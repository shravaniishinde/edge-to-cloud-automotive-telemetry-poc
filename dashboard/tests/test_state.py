"""Unit tests for the dashboard's state store, log tap, buffer probe, and
resilience-state derivation. No broker, no network."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from common.can_signal_map import decode_to_event, encode_signal
from dashboard.backend import state as state_module
from dashboard.backend.sources import LogTap, read_buffer_depth
from dashboard.backend.state import DashboardState, derive_resilience_state
from edge_gateway.buffer import TelemetryBuffer
from edge_gateway.gateway import EdgeGateway
from edge_gateway.normalization import normalize


class _Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _message(can_id=0x100, value=50.0, vehicle="VEH-1", session="sess-1", ts=None):
    event = decode_to_event(can_id, encode_signal(can_id, value), session_id=session, vehicle_id=vehicle)
    if ts is not None:
        event = event.model_copy(update={"timestamp": ts})
    topic, payload = normalize(event)  # the gateway's own normalization -> exact wire format
    return event, topic, payload


def test_empty_state_is_explicit_not_invented():
    snap = DashboardState().snapshot()

    assert snap["telemetry"]["flow"] == "none"
    assert snap["telemetry"]["total_received"] == 0
    assert snap["telemetry"]["last_event_timestamp"] is None
    assert snap["vehicles"] == [] and snap["events"] == [] and snap["series"] == {}
    assert snap["selected_vehicle"] is None
    assert snap["diagnostics"]["status"] == "not run"
    json.dumps(snap)  # always serializable


def test_valid_payload_is_parsed_with_its_identity_intact():
    clock = _Clock()
    state = DashboardState(clock=clock)
    event, topic, payload = _message(ts=datetime.fromtimestamp(clock.t, tz=timezone.utc))

    assert state.ingest_telemetry(topic, payload) is True

    snap = state.snapshot()
    (row,) = snap["events"]
    assert (row["event_id"], row["session_id"], row["vehicle_id"], row["topic"]) == (
        event.event_id, "sess-1", "VEH-1", topic)
    assert row["signal_name"] == "vehicle_speed_kph" and row["source_ecu"] == "powertrain"
    assert row["value"] == pytest.approx(50.0) and row["status"] == "live"
    assert snap["telemetry"]["flow"] == "flowing"
    assert snap["telemetry"]["sessions_seen"] == ["sess-1"]
    assert snap["vehicles"][0]["ecus"]["powertrain"]["signals"]["vehicle_speed_kph"]["event_id"] == event.event_id


@pytest.mark.parametrize("topic_payload", [
    ("vehicle/VEH-1/telemetry/powertrain/vehicle_speed_kph", b"not json"),
    ("vehicle/VEH-1/telemetry/powertrain/vehicle_speed_kph", b'{"value": 1}'),
    ("vehicle/VEH-1/telemetry/powertrain/vehicle_speed_kph", b"x" * (state_module.MAX_PAYLOAD_BYTES + 1)),
])
def test_malformed_payloads_are_counted_not_raised(topic_payload):
    state = DashboardState()
    assert state.ingest_telemetry(*topic_payload) is False
    snap = state.snapshot()
    assert snap["telemetry"]["malformed"] == 1 and snap["telemetry"]["total_received"] == 0
    assert "ignored malformed telemetry" in snap["activity"][0]["message"]


def test_payload_that_contradicts_its_topic_or_is_non_finite_is_rejected():
    state = DashboardState()
    _event, topic, payload = _message()
    assert state.ingest_telemetry(topic.replace("VEH-1", "OTHER"), payload) is False

    nan_payload = json.loads(payload)
    nan_payload["value"] = "NaN"
    assert state.ingest_telemetry(topic, json.dumps(nan_payload).encode()) is False
    assert state.snapshot()["telemetry"]["malformed"] == 2


def test_duplicates_are_counted_by_event_id():
    state = DashboardState()
    _event, topic, payload = _message()
    state.ingest_telemetry(topic, payload)
    state.ingest_telemetry(topic, payload)
    assert state.snapshot()["telemetry"]["duplicates"] == 1


def test_all_history_is_bounded(monkeypatch):
    monkeypatch.setattr(state_module, "MAX_VEHICLES", 3)
    clock = _Clock()
    state = DashboardState(clock=clock)
    base = datetime.fromtimestamp(clock.t, tz=timezone.utc)
    for i in range(1500):
        clock.t += 0.01
        _e, topic, payload = _message(value=float(i % 200), vehicle=f"VEH-{i % 5}",
                                      ts=base + timedelta(seconds=i))
        state.ingest_telemetry(topic, payload)
        state.add_activity("test", "INFO", f"message {i}")

    snap = state.snapshot(max_events=10_000)
    assert len(snap["events"]) == state_module.MAX_RECENT_EVENTS
    assert len(snap["activity"]) == state_module.MAX_ACTIVITY
    assert len(snap["vehicles"]) == 3  # least-recently-seen evicted
    assert all(len(points) <= state_module.MAX_SERIES_POINTS for points in snap["series"].values())


def test_activity_coalesces_interleaved_repeats():
    state = DashboardState()
    for _ in range(50):
        state.add_activity("edge_gateway.buffer", "WARNING", "publish failed -- buffered for replay")
        state.add_activity("edge_gateway.mqtt_publisher", "ERROR", "publish attempted while not connected")
    activity = state.snapshot()["activity"]
    assert len(activity) == 2
    assert {a["count"] for a in activity} == {50}


def test_flow_goes_stale_when_telemetry_stops():
    clock = _Clock()
    state = DashboardState(clock=clock)
    _e, topic, payload = _message()
    state.ingest_telemetry(topic, payload)
    clock.t += state_module.FLOW_FRESH_SECONDS + 1
    assert state.snapshot()["telemetry"]["flow"] == "stale"


def test_log_tap_reads_real_gateway_records_and_marks_replayed_events(tmp_path):
    """Drives a real EdgeGateway (mock bus/publisher, real buffer) so the tap
    sees the gateway's actual structured records, not hand-made ones."""
    from unittest.mock import MagicMock

    state = DashboardState()
    tap = LogTap(state)
    tap.attach("edge_gateway")
    try:
        buffer = TelemetryBuffer(tmp_path / "b.db")
        event, topic, payload = _message(vehicle="VEH-R")
        buffer.enqueue(event.event_id, topic, payload)
        publisher = MagicMock()
        publisher.is_connected.return_value = True
        publisher.publish.return_value = True
        gateway = EdgeGateway(MagicMock(), publisher, buffer, session_id="tap-session")
        gateway._replay_buffered()
        state.ingest_telemetry(topic, payload)  # the replayed message arriving via MQTT
    finally:
        tap.detach()

    snap = state.snapshot()
    assert snap["events"][0]["status"] == "replayed"
    assert snap["last_replay"]["replayed"] == 1 and snap["last_replay"]["session_id"] == "tap-session"
    replay_entry = next(a for a in snap["activity"] if a["message"] == "replayed buffered events")
    assert replay_entry["fields"]["session_id"] == "tap-session"
    assert replay_entry["fields"]["first_event_id"] == event.event_id
    buffer.close()


def test_log_tap_only_forwards_allowlisted_fields():
    state = DashboardState()
    tap = LogTap(state)
    tap.attach("edge_gateway")
    try:
        logging.getLogger("edge_gateway.test").warning("something odd", extra={"session_id": "s", "api_key": "SECRET"})
    finally:
        tap.detach()
    entry = state.snapshot()["activity"][0]
    assert entry["fields"] == {"session_id": "s"}
    assert "SECRET" not in json.dumps(state.snapshot())


def test_log_tap_detach_restores_logger_state():
    logger = logging.getLogger("edge_gateway")
    before = (logger.level, list(logger.handlers))
    tap = LogTap(DashboardState())
    tap.attach("edge_gateway")
    tap.detach()
    assert (logger.level, list(logger.handlers)) == before


def test_gateway_stopped_summary_is_captured():
    state = DashboardState()
    state.ingest_log_record({"message": "gateway stopped", "session_id": "s-9", "processed": 5, "rejected": 0,
                             "publish_failures": 1, "buffered": 1, "replayed": 1, "dropped": 0, "buffer_pending": 0})
    summary = state.snapshot()["last_stopped_summary"]
    assert summary["session_id"] == "s-9" and summary["processed"] == 5 and summary["buffer_pending"] == 0


def test_read_buffer_depth_is_read_only_and_tolerates_missing_files(tmp_path):
    assert read_buffer_depth(None) is None
    assert read_buffer_depth(tmp_path / "missing.db") is None
    buffer = TelemetryBuffer(tmp_path / "b.db")
    for i in range(3):
        buffer.enqueue(f"e{i}", "t", b"p")
    assert read_buffer_depth(tmp_path / "b.db") == 3
    buffer.close()


@pytest.mark.parametrize("kwargs,expected", [
    (dict(running=False, mqtt_connected=None, outage="none", buffer_depth=None), "IDLE"),
    (dict(running=True, mqtt_connected=None, outage="none", buffer_depth=None), "UNKNOWN"),
    (dict(running=True, mqtt_connected=True, outage="none", buffer_depth=0), "NORMAL"),
    (dict(running=True, mqtt_connected=False, outage="active", buffer_depth=0), "OUTAGE"),
    (dict(running=True, mqtt_connected=False, outage="active", buffer_depth=12), "BUFFERING"),
    (dict(running=True, mqtt_connected=False, outage="none", buffer_depth=12), "BUFFERING"),  # real broker outage
    (dict(running=True, mqtt_connected=False, outage="cleared", buffer_depth=12), "RECONNECTING"),
    # Connected with rows buffered but no replay activity (found in manual testing with a real
    # `docker compose stop mosquitto`: the publisher reported connected while the buffer grew)
    # must NOT be called REPLAYING.
    (dict(running=True, mqtt_connected=True, outage="none", buffer_depth=12), "BUFFERING"),
])
def test_resilience_state_derivation(kwargs, expected):
    assert derive_resilience_state(replayed_total=0, seconds_since_replay=None, **kwargs)["state"] == expected


def test_replaying_requires_a_recent_replay_batch():
    base = dict(running=True, mqtt_connected=True, outage="none", buffer_depth=12, replayed_total=100)
    assert derive_resilience_state(seconds_since_replay=1.0, **base)["state"] == "REPLAYING"
    assert derive_resilience_state(seconds_since_replay=60.0, **base)["state"] == "BUFFERING"


def test_recovered_only_after_a_replay_drained_the_buffer():
    base = dict(running=True, mqtt_connected=True, outage="none", buffer_depth=0, seconds_since_replay=1.0)
    assert derive_resilience_state(replayed_total=0, **base)["state"] == "NORMAL"
    assert derive_resilience_state(replayed_total=7, **base)["state"] == "RECOVERED"
