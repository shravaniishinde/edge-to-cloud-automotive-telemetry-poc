"""
End-to-end EdgeGateway test: a real virtual CAN frame goes in, a real
Mosquitto broker (via the `mosquitto_broker` fixture) is on the other end,
and a real MQTT subscriber confirms what actually arrived -- proving the
whole ingest -> validate -> normalize -> publish pipeline works together,
not just each step's logic in isolation (that's what test_ingestion.py /
test_validation.py / test_normalization.py already cover).

Phase 4 adds resilience tests to this same file: they use
`force_publish_failures()` to simulate an MQTT outage deterministically
(no real broker stop/start needed for these), then let the connection
"recover" simply by the context manager exiting, and confirm the buffer/
replay/ordering behavior against the same real broker and real
subscriber every other test here already uses.

Phase 8 moved the test subscriber into conftest.py (`mqtt_subscriber`,
which waits for a real SUBACK instead of a fixed sleep), scoped every
test here to a per-run vehicle_id so unrelated traffic on the same
broker can't leak in, and added the restart/backlog-replay test at the
bottom. The full ECU -> CAN -> gateway -> MQTT scenario lives in
test_full_scenario_integration.py.
"""

import json
import threading
import time
import uuid

import can
import pytest

from common.can_signal_map import decode_to_event, encode_signal
from edge_gateway.buffer import TelemetryBuffer
from edge_gateway.fault_injection import force_publish_failures
from edge_gateway.gateway import EdgeGateway
from edge_gateway.mqtt_publisher import MqttPublisher
from edge_gateway.normalization import normalize
from simulation.can_bus import get_bus

# A vehicle_id unique to this pytest run, so these tests' subscriptions only
# ever see what THIS run's gateway published -- not, say, the Docker Compose
# `app` container's run_demo.py (which publishes as SIM-VEHICLE-01) if it
# happens to be running against the same broker.
TEST_VEHICLE_ID = f"IT-{uuid.uuid4().hex[:8]}"
TOPIC_FILTER = f"vehicle/{TEST_VEHICLE_ID}/telemetry/+/+"

# How long to keep listening when asserting that nothing was published.
NEGATIVE_WAIT_SECONDS = 0.3


@pytest.fixture
def gateway(mosquitto_broker, mosquitto_host, tmp_path):
    bus = get_bus()
    publisher = MqttPublisher(host=mosquitto_host, port=mosquitto_broker)
    publisher.connect()
    buffer = TelemetryBuffer(tmp_path / "buffer.db")
    gw = EdgeGateway(bus, publisher, buffer, vehicle_id=TEST_VEHICLE_ID, session_id="integration-test-session")

    yield gw

    publisher.disconnect()
    buffer.close()
    bus.shutdown()


def test_valid_telemetry_frame_is_published_over_real_mqtt(gateway, mqtt_subscriber):
    subscriber = mqtt_subscriber(TOPIC_FILTER)

    sender_bus = get_bus()
    frame = can.Message(arbitration_id=0x100, data=encode_signal(0x100, 62.3), is_extended_id=False)
    sender_bus.send(frame)

    gateway.run_once()  # processes exactly the frame just sent
    subscriber.wait_for_count(1)
    time.sleep(NEGATIVE_WAIT_SECONDS)  # ...and nothing beyond it arrives

    sender_bus.shutdown()

    assert len(subscriber.messages) == 1
    msg = subscriber.messages[0]
    assert msg.topic == f"vehicle/{TEST_VEHICLE_ID}/telemetry/powertrain/vehicle_speed_kph"
    payload = json.loads(msg.payload)
    assert payload["value"] == pytest.approx(62.3, abs=0.1)
    assert gateway.processed_count == 1
    assert gateway.rejected_count == 0


def test_out_of_range_frame_is_rejected_and_never_published(gateway, mqtt_subscriber):
    subscriber = mqtt_subscriber(TOPIC_FILTER)

    sender_bus = get_bus()
    # battery_soc_pct's valid_range is (0.0, 100.0), but encode_signal only
    # enforces that the raw value fits its data type (uint16 here) -- it
    # happily encodes 250% (a corrupted/faulty sensor reading), exactly the
    # "structurally valid but physically wrong" case Phase 1 left for the
    # gateway to catch.
    frame = can.Message(arbitration_id=0x200, data=encode_signal(0x200, 250.0), is_extended_id=False)
    sender_bus.send(frame)

    gateway.run_once()
    time.sleep(NEGATIVE_WAIT_SECONDS)

    sender_bus.shutdown()

    assert len(subscriber.messages) == 0  # never forwarded
    assert gateway.rejected_count == 1
    assert gateway.processed_count == 0
    assert gateway._buffer.count() == 0  # rejected before ever reaching publish -- nothing to buffer


def test_uds_frame_sharing_the_bus_is_ignored_by_the_gateway(gateway, mqtt_subscriber):
    subscriber = mqtt_subscriber(TOPIC_FILTER)

    sender_bus = get_bus()
    uds_frame = can.Message(arbitration_id=0x7E0, data=bytes(8), is_extended_id=False)
    sender_bus.send(uds_frame)

    gateway.run_once()
    time.sleep(NEGATIVE_WAIT_SECONDS)

    sender_bus.shutdown()

    assert len(subscriber.messages) == 0
    assert gateway.processed_count == 0
    assert gateway.rejected_count == 0
    assert gateway._buffer.count() == 0  # never even ingested as telemetry -- nothing to buffer


# --- Phase 4: resilience (buffering, replay, ordering, continued operation) ---


def test_publish_failure_buffers_the_event_instead_of_dropping_it(gateway):
    sender_bus = get_bus()

    with force_publish_failures(gateway._publisher, count=1):
        frame = can.Message(arbitration_id=0x100, data=encode_signal(0x100, 62.3), is_extended_id=False)
        sender_bus.send(frame)
        gateway.run_once()

    sender_bus.shutdown()

    assert gateway.processed_count == 0
    assert gateway.publish_failure_count == 1
    assert gateway.buffered_count == 1
    assert gateway._buffer.count() == 1


def test_gateway_keeps_ingesting_and_validating_during_an_outage(gateway):
    sender_bus = get_bus()

    with force_publish_failures(gateway._publisher, count=10):
        good_frame = can.Message(arbitration_id=0x100, data=encode_signal(0x100, 50.0), is_extended_id=False)
        sender_bus.send(good_frame)
        gateway.run_once()

        # battery_soc_pct's valid_range is (0.0, 100.0) -- still rejected by
        # validation.py exactly as before, never even reaching publish.
        bad_frame = can.Message(arbitration_id=0x200, data=encode_signal(0x200, 250.0), is_extended_id=False)
        sender_bus.send(bad_frame)
        gateway.run_once()

    sender_bus.shutdown()

    assert gateway.buffered_count == 1   # the valid frame: publish failed -> buffered
    assert gateway.rejected_count == 1   # the invalid frame: rejected, as always
    assert gateway.processed_count == 0


def test_buffered_events_replay_in_order_and_drain_the_buffer_on_recovery(gateway, mqtt_subscriber):
    subscriber = mqtt_subscriber(TOPIC_FILTER)
    sender_bus = get_bus()
    speeds = (10.0, 20.0, 30.0)

    with force_publish_failures(gateway._publisher, count=len(speeds)):
        for speed in speeds:
            frame = can.Message(arbitration_id=0x100, data=encode_signal(0x100, speed), is_extended_id=False)
            sender_bus.send(frame)
            gateway.run_once()

    assert gateway._buffer.count() == len(speeds)
    assert len(subscriber.messages) == 0  # nothing reached the broker yet -- all buffered

    # "MQTT recovery" from the gateway's point of view is exactly this:
    # publish() starts succeeding again. _replay_buffered() is the same
    # method run_once() calls automatically once try_reconnect() reports
    # success (see edge_gateway/tests/test_gateway_unit.py for that
    # wiring, verified there without needing a real broker).
    gateway._replay_buffered()
    subscriber.wait_for_count(len(speeds))

    sender_bus.shutdown()

    assert gateway._buffer.count() == 0
    assert gateway.replayed_count == len(speeds)
    assert len(subscriber.messages) == len(speeds)
    received_speeds = [json.loads(msg.payload)["value"] for msg in subscriber.messages]
    assert received_speeds == pytest.approx(list(speeds), abs=0.1)


def test_backlog_left_by_a_previous_run_is_replayed_in_order_when_run_starts(
    mosquitto_broker, mosquitto_host, mqtt_subscriber, tmp_path,
):
    """The restart half of the Phase 4 story, through the real `run()`
    loop rather than calling `_replay_buffered()` directly: a previous
    gateway process left events in the SQLite buffer file; a brand-new
    gateway opened against that same file replays them, in FIFO order,
    as soon as `run()` starts -- with no reconnect event and no new CAN
    traffic needed to trigger it. Replayed payloads are byte-for-byte what
    was buffered, so they keep the ORIGINAL session_id/event_id: the new
    run's session_id never rewrites a previous run's events."""
    db_path = tmp_path / "buffer.db"
    previous_session_id = "previous-run-session"
    buffered_events = [
        decode_to_event(0x100, encode_signal(0x100, speed), session_id=previous_session_id, vehicle_id=TEST_VEHICLE_ID)
        for speed in (11.0, 22.0, 33.0)
    ]

    previous_run_buffer = TelemetryBuffer(db_path)
    for event in buffered_events:
        topic, payload = normalize(event)
        previous_run_buffer.enqueue(event.event_id, topic, payload)
    previous_run_buffer.close()

    subscriber = mqtt_subscriber(TOPIC_FILTER)
    bus = get_bus()
    publisher = MqttPublisher(host=mosquitto_host, port=mosquitto_broker)
    publisher.connect()
    buffer = TelemetryBuffer(db_path)
    gw = EdgeGateway(bus, publisher, buffer, vehicle_id=TEST_VEHICLE_ID, session_id="restarted-run-session")
    stop_event = threading.Event()
    gateway_thread = threading.Thread(target=gw.run, args=(stop_event,), daemon=True)
    try:
        gateway_thread.start()
        subscriber.wait_for_count(len(buffered_events))
    finally:
        stop_event.set()
        gateway_thread.join(timeout=5)
        publisher.disconnect()
        bus.shutdown()

    assert not gateway_thread.is_alive()
    received = [json.loads(msg.payload) for msg in subscriber.messages]
    assert [p["event_id"] for p in received] == [e.event_id for e in buffered_events]
    assert {p["session_id"] for p in received} == {previous_session_id}
    assert gw.replayed_count == len(buffered_events)
    assert gw.processed_count == 0  # replay is counted separately from live publishes
    assert buffer.count() == 0
    buffer.close()
