"""Phase 7 tests for edge_gateway/metrics.py and its EdgeGateway wiring.
No CAN bus or MQTT broker needed: the gateway-level tests use a mocked
bus/publisher and a real (tmp_path) TelemetryBuffer, so buffer drops are
the buffer's own real accounting, not a mock's."""

from __future__ import annotations

import json
import logging
import threading
from unittest.mock import MagicMock

import can
import pytest

from common.can_signal_map import encode_signal
from edge_gateway.buffer import TelemetryBuffer
from edge_gateway.gateway import EdgeGateway
from edge_gateway.metrics import GatewayMetrics, MetricsSnapshot

SNAPSHOT_KEYS = ["session_id", "processed", "rejected", "publish_failures", "buffered", "replayed", "dropped"]


# --- GatewayMetrics on its own ---


def test_new_metrics_start_at_zero():
    snapshot = GatewayMetrics(session_id="s-1").snapshot(dropped=0)

    assert snapshot == MetricsSnapshot("s-1", 0, 0, 0, 0, 0, 0)


def test_each_record_method_increments_only_its_own_counter():
    metrics = GatewayMetrics(session_id="s-1")

    metrics.record_processed()
    metrics.record_processed()
    metrics.record_rejected()
    metrics.record_publish_failure()
    metrics.record_buffered()
    metrics.record_replayed()
    metrics.record_replayed(3)

    assert (metrics.processed, metrics.rejected, metrics.publish_failures, metrics.buffered, metrics.replayed) == (
        2, 1, 1, 1, 4,
    )


def test_record_replayed_rejects_a_negative_count():
    with pytest.raises(ValueError):
        GatewayMetrics(session_id="s-1").record_replayed(-1)


def test_snapshot_is_an_immutable_point_in_time_copy():
    metrics = GatewayMetrics(session_id="s-1")
    metrics.record_processed()

    snapshot = metrics.snapshot(dropped=2)
    metrics.record_processed()  # later changes must not leak into an earlier snapshot

    assert snapshot.processed == 1
    assert snapshot.dropped == 2
    with pytest.raises(AttributeError):
        snapshot.processed = 99  # frozen


def test_snapshot_log_fields_are_deterministic_and_json_serializable():
    metrics = GatewayMetrics(session_id="s-1")
    metrics.record_rejected()

    fields = metrics.snapshot(dropped=0).as_log_fields()

    assert list(fields) == SNAPSHOT_KEYS  # stable key order
    assert json.loads(json.dumps(fields)) == {
        "session_id": "s-1", "processed": 0, "rejected": 1, "publish_failures": 0,
        "buffered": 0, "replayed": 0, "dropped": 0,
    }
    assert fields == metrics.snapshot(dropped=0).as_log_fields()  # same state -> same output


def test_concurrent_increments_are_not_lost():
    metrics = GatewayMetrics(session_id="s-1")
    threads_count, per_thread = 8, 5000

    def _hammer():
        for _ in range(per_thread):
            metrics.record_processed()
            metrics.record_replayed()

    threads = [threading.Thread(target=_hammer) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert metrics.processed == threads_count * per_thread
    assert metrics.replayed == threads_count * per_thread


# --- EdgeGateway integration ---


def _frame(can_id: int, value: float) -> can.Message:
    return can.Message(arbitration_id=can_id, data=encode_signal(can_id, value), is_extended_id=False)


def _gateway_with_frames(frames, publish_results, buffer):
    bus = MagicMock()
    bus.recv.side_effect = list(frames)
    publisher = MagicMock()
    publisher.is_connected.return_value = True
    publisher.publish.side_effect = list(publish_results)
    return EdgeGateway(bus, publisher, buffer, vehicle_id="METRICS-TEST", session_id="metrics-session")


def test_gateway_records_each_outcome_at_its_semantic_point(tmp_path):
    buffer = TelemetryBuffer(tmp_path / "buffer.db")
    frames = [
        _frame(0x100, 50.0),   # published -> processed
        _frame(0x200, 250.0),  # out of range -> rejected, never published
        _frame(0x100, 60.0),   # publish fails -> publish_failure + buffered
        can.Message(arbitration_id=0x7E0, data=bytes(8), is_extended_id=False),  # UDS: not telemetry -> no counter
    ]
    gateway = _gateway_with_frames(frames, publish_results=[True, False], buffer=buffer)

    for _ in frames:
        gateway.run_once()

    assert gateway.metrics_snapshot() == MetricsSnapshot(
        session_id="metrics-session", processed=1, rejected=1, publish_failures=1, buffered=1, replayed=0, dropped=0,
    )
    # Phase 3/4 attribute names still work and read the same object.
    assert (gateway.processed_count, gateway.rejected_count, gateway.publish_failure_count,
            gateway.buffered_count, gateway.replayed_count) == (1, 1, 1, 1, 0)
    buffer.close()


def test_replay_counts_only_confirmed_rows_and_not_as_processed(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="edge_gateway")
    buffer = TelemetryBuffer(tmp_path / "buffer.db")
    for i in range(3):
        buffer.enqueue(f"evt-{i}", "t", b"p")
    gateway = _gateway_with_frames([], publish_results=[True, True, False], buffer=buffer)

    gateway._replay_buffered()

    snapshot = gateway.metrics_snapshot()
    assert snapshot.replayed == 2
    assert snapshot.processed == 0
    assert snapshot.publish_failures == 0  # a replay that stops early is not a new publish failure
    assert buffer.count() == 1
    # The replay log line names exactly the replayed event_ids, under THIS
    # run's session_id -- the per-event link between a session and the
    # (possibly older) events it replayed.
    (replay_record,) = [r for r in caplog.records if r.getMessage() == "replayed buffered events"]
    assert replay_record.event_ids == ["evt-0", "evt-1"]
    assert replay_record.session_id == "metrics-session"
    buffer.close()


def test_buffer_drops_appear_in_the_snapshot_exactly_once(tmp_path):
    buffer = TelemetryBuffer(tmp_path / "buffer.db", max_size=2)
    frames = [_frame(0x100, float(speed)) for speed in (10, 20, 30, 40, 50)]
    gateway = _gateway_with_frames(frames, publish_results=[False] * 5, buffer=buffer)

    for _ in frames:
        gateway.run_once()

    snapshot = gateway.metrics_snapshot()
    assert snapshot.buffered == 5
    assert snapshot.dropped == 3 == buffer.dropped_count  # the buffer stays the one authority
    assert buffer.count() == 2
    buffer.close()


def test_run_logs_a_gateway_stopped_summary_carrying_the_session_id(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="edge_gateway")
    buffer = TelemetryBuffer(tmp_path / "buffer.db")
    stop_event = threading.Event()
    gateway = _gateway_with_frames([], publish_results=[True], buffer=buffer)
    frames = [_frame(0x100, 42.0)]

    def _recv(timeout):
        if frames:
            return frames.pop(0)
        stop_event.set()  # no more traffic: end the run after this iteration
        return None

    gateway._bus.recv.side_effect = _recv

    gateway.run(stop_event)

    summaries = [r for r in caplog.records if r.getMessage() == "gateway stopped"]
    assert len(summaries) == 1
    record = summaries[0]
    assert record.session_id == gateway.session_id == "metrics-session"
    assert record.component == "metrics"
    assert {key: getattr(record, key) for key in SNAPSHOT_KEYS} == gateway.metrics_snapshot().as_log_fields()
    assert record.processed == 1
    assert record.buffer_pending == 0
    buffer.close()


def test_gateway_stopped_summary_never_masks_the_loops_own_error_even_with_a_closed_buffer(tmp_path, caplog):
    """Audit regression: a caller that gives up on join() and closes the
    buffer while an iteration is still in flight must not turn the loop's
    real error into a sqlite3.ProgrammingError, or lose the summary."""
    caplog.set_level(logging.INFO, logger="edge_gateway")
    buffer = TelemetryBuffer(tmp_path / "buffer.db")
    stop_event = threading.Event()
    gateway = _gateway_with_frames([], publish_results=[], buffer=buffer)

    def _recv(timeout):
        buffer.close()
        stop_event.set()
        raise RuntimeError("original loop error")

    gateway._bus.recv.side_effect = _recv

    with pytest.raises(RuntimeError, match="original loop error"):
        gateway.run(stop_event)

    (summary,) = [r for r in caplog.records if r.getMessage() == "gateway stopped"]
    assert summary.session_id == "metrics-session"
    assert summary.buffer_pending is None


def test_each_gateway_instance_is_a_fresh_run_with_its_own_session_and_counters(tmp_path):
    buffer = TelemetryBuffer(tmp_path / "buffer.db")
    first = _gateway_with_frames([_frame(0x100, 1.0)], publish_results=[True], buffer=buffer)
    first.run_once()
    second = EdgeGateway(MagicMock(), MagicMock(), buffer)  # no session_id given -> a new UUID

    assert first.metrics_snapshot().processed == 1
    assert second.metrics_snapshot().processed == 0
    assert second.metrics_snapshot().session_id == second.session_id != first.session_id
    buffer.close()
