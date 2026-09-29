"""
Phase 8's one full-scenario integration test: the real pipeline, wired
the same way run_demo.py wires it, with nothing mocked --

    3 seeded simulated ECUs (their own threads, via simulation.can_bus.run_ecu)
      -> python-can virtual CAN bus
      -> EdgeGateway.run() on its own thread
         (ingest -> decode -> validate -> normalize -> publish)
      -> real local Mosquitto broker
      -> real MQTT subscriber (this test)

Everything stays in this one process, exactly like run_demo.py, because
python-can's virtual bus only shares frames within a process (see
simulation/can_bus.py) -- this test does not, and must not, split the
ECUs and the gateway apart.

Determinism: the ECUs are seeded, so each ECU's FIRST tick is fully
predictable. Replica ECUs built with the same seed tell us exactly what
value every signal's first message must carry (after the real CAN
encode/decode quantization), and per-topic MQTT ordering guarantees the
first message received on each topic is that first tick. How many later
ticks happen before shutdown depends on thread timing, so later messages
are checked for identity/consistency, never for specific values.

Skipped (or failed, under MQTT_BROKER_REQUIRED=1) when no broker is
reachable -- see conftest.py.
"""

from __future__ import annotations

import logging
import threading
import uuid

from common.can_signal_map import SIGNAL_REGISTRY, decode_signal, encode_signal
from common.telemetry_schema import SCHEMA_VERSION, TelemetryEvent
from edge_gateway.buffer import TelemetryBuffer
from edge_gateway.gateway import EdgeGateway
from edge_gateway.mqtt_publisher import MqttPublisher
from simulation.can_bus import get_bus, run_ecu
from simulation.ecus.battery_ecu import BatteryECU
from simulation.ecus.body_ecu import BodyECU
from simulation.ecus.powertrain_ecu import PowertrainECU

SEED = 42
# The simulator's own session_id is in-memory bookkeeping only; it never
# reaches the CAN wire. The gateway's session_id is the one that must show
# up downstream. Distinct values make that checkable.
ECU_SESSION_ID = "full-scenario-ecu-session"
VEHICLE_ID = f"FS-{uuid.uuid4().hex[:8]}"  # isolates this test from other broker traffic

ALL_SIGNALS_TIMEOUT_SECONDS = 10.0
THREAD_JOIN_TIMEOUT_SECONDS = 5.0


def _make_ecus():
    return [
        PowertrainECU(ECU_SESSION_ID, VEHICLE_ID, rng_seed=SEED),
        BatteryECU(ECU_SESSION_ID, VEHICLE_ID, rng_seed=SEED),
        BodyECU(ECU_SESSION_ID, VEHICLE_ID, rng_seed=SEED),
    ]


def _expected_first_tick_values():
    """can_id -> the exact value the gateway should decode from each
    ECU's first frame: a same-seed replica's first tick, passed through
    the same encode/decode the wire applies."""
    expected = {}
    for ecu in _make_ecus():
        for event in ecu.tick():
            expected[event.can_id] = decode_signal(event.can_id, encode_signal(event.can_id, event.value)).value
    return expected


def _expected_topics():
    return {
        f"vehicle/{VEHICLE_ID}/telemetry/{definition.ecu.value}/{definition.signal_name.value}"
        for definition in SIGNAL_REGISTRY.values()
    }


def test_seeded_ecu_telemetry_flows_end_to_end_to_a_real_mqtt_subscriber(
    mosquitto_broker, mosquitto_host, mqtt_subscriber, tmp_path, caplog,
):
    caplog.set_level(logging.DEBUG, logger="edge_gateway")
    expected_topics = _expected_topics()
    expected_values = _expected_first_tick_values()
    subscriber = mqtt_subscriber(f"vehicle/{VEHICLE_ID}/telemetry/#")

    # --- wiring: same shape as run_demo.py ---
    gateway_bus = get_bus()  # opened before any ECU sends, so no frame is missed
    publisher = MqttPublisher(host=mosquitto_host, port=mosquitto_broker)
    publisher.connect()
    buffer = TelemetryBuffer(tmp_path / "buffer.db")
    gateway = EdgeGateway(gateway_bus, publisher, buffer, vehicle_id=VEHICLE_ID)

    ecus = _make_ecus()
    ecu_buses = [get_bus() for _ in ecus]
    stop_event = threading.Event()
    gateway_thread = threading.Thread(target=gateway.run, args=(stop_event,), daemon=True)
    ecu_threads = [
        threading.Thread(target=run_ecu, args=(ecu, bus, stop_event, ecu.TICK_INTERVAL_SECONDS), daemon=True)
        for ecu, bus in zip(ecus, ecu_buses)
    ]

    try:
        gateway_thread.start()
        for thread in ecu_threads:
            thread.start()
        all_signals_seen = subscriber.wait_until(
            lambda msgs: {m.topic for m in msgs} >= expected_topics, timeout=ALL_SIGNALS_TIMEOUT_SECONDS,
        )
    finally:
        # --- shutdown: same order as run_demo.py ---
        stop_event.set()
        for thread in ecu_threads:
            thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
        gateway_thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
        for bus in ecu_buses:
            bus.shutdown()
        gateway_bus.shutdown()
        publisher.disconnect()

    # Clean shutdown: every thread actually exited on stop_event.
    assert not any(t.is_alive() for t in ecu_threads), "an ECU thread did not stop"
    assert not gateway_thread.is_alive(), "EdgeGateway.run() did not stop"

    # Every signal from all 3 ECUs made it through, on the normalized topic scheme.
    assert all_signals_seen, f"missing topics: {expected_topics - {m.topic for m in subscriber.messages}}"

    # Nothing lost, nothing duplicated, nothing extra: every event the
    # gateway counted as published (broker-acknowledged) reached the
    # subscriber exactly once, and no event was rejected or buffered.
    subscriber.wait_for_count(gateway.processed_count)
    messages = subscriber.messages
    assert len(messages) == gateway.processed_count
    assert gateway.rejected_count == 0
    assert gateway.publish_failure_count == 0
    assert gateway.buffered_count == 0
    assert buffer.count() == 0
    buffer.close()

    events = [TelemetryEvent.model_validate_json(m.payload) for m in messages]  # full schema round-trip
    assert len({e.event_id for e in events}) == len(events)
    assert all(uuid.UUID(e.event_id) for e in events)

    for msg, event in zip(messages, events):
        definition = SIGNAL_REGISTRY[event.can_id]
        # The payload agrees with its own topic and with the signal registry.
        assert msg.topic == f"vehicle/{VEHICLE_ID}/telemetry/{event.source_ecu.value}/{event.signal_name.value}"
        assert (event.source_ecu, event.signal_name, event.unit) == (definition.ecu, definition.signal_name, definition.unit)
        low, high = definition.valid_range
        assert low <= event.value <= high
        # Identity: the gateway's session_id, never the simulator's.
        assert event.session_id == gateway.session_id
        assert event.session_id != ECU_SESSION_ID
        assert event.vehicle_id == VEHICLE_ID
        assert event.schema_version == SCHEMA_VERSION

    # Deterministic content: the first message on each topic is that
    # signal's seeded first-tick value.
    first_by_can_id = {}
    for event in events:
        first_by_can_id.setdefault(event.can_id, event)
    assert set(first_by_can_id) == set(expected_values)
    for can_id, event in first_by_can_id.items():
        assert event.value == expected_values[can_id], f"CAN ID 0x{can_id:03X}"

    # Traceability: every received event_id can be found in the gateway's
    # structured logs -- ingested and published -- under the same session_id.
    ingested = {
        r.event_id: r for r in caplog.records
        if r.getMessage() == "ingested frame" and hasattr(r, "event_id")
    }
    published = {
        r.event_id: r for r in caplog.records
        if r.getMessage() == "published telemetry event" and hasattr(r, "event_id")
    }
    for msg, event in zip(messages, events):
        assert event.event_id in ingested, f"no ingestion log for {event.event_id}"
        assert ingested[event.event_id].session_id == gateway.session_id
        publish_record = published[event.event_id]
        assert publish_record.session_id == gateway.session_id
        assert publish_record.topic == msg.topic

    # Phase 7: the run ends with exactly one "gateway stopped" metrics
    # summary under the same session_id, agreeing with what was observed.
    summaries = [r for r in caplog.records if r.getMessage() == "gateway stopped"]
    assert len(summaries) == 1
    assert summaries[0].session_id == gateway.session_id
    assert summaries[0].processed == len(messages)
    assert (summaries[0].rejected, summaries[0].buffered, summaries[0].dropped, summaries[0].buffer_pending) == (0, 0, 0, 0)
