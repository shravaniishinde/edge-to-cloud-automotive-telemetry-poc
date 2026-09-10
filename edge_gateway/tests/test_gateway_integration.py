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
"""

import json
import time

import can
import paho.mqtt.client as mqtt
import pytest

from common.can_signal_map import encode_signal
from edge_gateway.buffer import TelemetryBuffer
from edge_gateway.fault_injection import force_publish_failures
from edge_gateway.gateway import EdgeGateway
from edge_gateway.mqtt_publisher import MqttPublisher
from simulation.can_bus import get_bus


class _Subscriber:
    """Small real MQTT subscriber used only by these tests to observe what
    the gateway actually published."""

    def __init__(self, port: int, topic: str) -> None:
        self.messages = []
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = lambda c, u, f, rc, p: c.subscribe(topic)
        self._client.on_message = lambda c, u, msg: self.messages.append(msg)
        self._client.connect("localhost", port, keepalive=5)
        self._client.loop_start()
        time.sleep(0.3)  # let the subscribe complete before the publisher sends anything

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


@pytest.fixture
def gateway(mosquitto_broker, tmp_path):
    bus = get_bus()
    publisher = MqttPublisher(host="localhost", port=mosquitto_broker)
    publisher.connect()
    buffer = TelemetryBuffer(tmp_path / "buffer.db")
    gw = EdgeGateway(bus, publisher, buffer, session_id="integration-test-session")

    yield gw

    publisher.disconnect()
    buffer.close()
    bus.shutdown()


def test_valid_telemetry_frame_is_published_over_real_mqtt(gateway, mosquitto_broker):
    subscriber = _Subscriber(mosquitto_broker, "vehicle/+/telemetry/+/+")

    sender_bus = get_bus()
    frame = can.Message(arbitration_id=0x100, data=encode_signal(0x100, 62.3), is_extended_id=False)
    sender_bus.send(frame)

    gateway.run_once()  # processes exactly the frame just sent
    time.sleep(0.3)      # let the subscriber's callback fire

    subscriber.close()
    sender_bus.shutdown()

    assert len(subscriber.messages) == 1
    msg = subscriber.messages[0]
    assert msg.topic == "vehicle/SIM-VEHICLE-01/telemetry/powertrain/vehicle_speed_kph"
    payload = json.loads(msg.payload)
    assert payload["value"] == pytest.approx(62.3, abs=0.1)
    assert gateway.processed_count == 1
    assert gateway.rejected_count == 0


def test_out_of_range_frame_is_rejected_and_never_published(gateway, mosquitto_broker):
    subscriber = _Subscriber(mosquitto_broker, "vehicle/+/telemetry/+/+")

    sender_bus = get_bus()
    # battery_soc_pct's valid_range is (0.0, 100.0), but encode_signal only
    # enforces that the raw value fits its data type (uint16 here) -- it
    # happily encodes 250% (a corrupted/faulty sensor reading), exactly the
    # "structurally valid but physically wrong" case Phase 1 left for the
    # gateway to catch.
    frame = can.Message(arbitration_id=0x200, data=encode_signal(0x200, 250.0), is_extended_id=False)
    sender_bus.send(frame)

    gateway.run_once()
    time.sleep(0.3)

    subscriber.close()
    sender_bus.shutdown()

    assert len(subscriber.messages) == 0  # never forwarded
    assert gateway.rejected_count == 1
    assert gateway.processed_count == 0
    assert gateway._buffer.count() == 0  # rejected before ever reaching publish -- nothing to buffer


def test_uds_frame_sharing_the_bus_is_ignored_by_the_gateway(gateway, mosquitto_broker):
    subscriber = _Subscriber(mosquitto_broker, "vehicle/+/telemetry/+/+")

    sender_bus = get_bus()
    uds_frame = can.Message(arbitration_id=0x7E0, data=bytes(8), is_extended_id=False)
    sender_bus.send(uds_frame)

    gateway.run_once()
    time.sleep(0.3)

    subscriber.close()
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


def test_buffered_events_replay_in_order_and_drain_the_buffer_on_recovery(gateway, mosquitto_broker):
    subscriber = _Subscriber(mosquitto_broker, "vehicle/+/telemetry/+/+")
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
    time.sleep(0.3)

    subscriber.close()
    sender_bus.shutdown()

    assert gateway._buffer.count() == 0
    assert gateway.replayed_count == len(speeds)
    assert len(subscriber.messages) == len(speeds)
    received_speeds = [json.loads(msg.payload)["value"] for msg in subscriber.messages]
    assert received_speeds == pytest.approx(list(speeds), abs=0.1)
