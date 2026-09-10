"""Unit tests for TelemetryBuffer -- no CAN bus or MQTT broker involved,
just the SQLite-backed queue itself."""

from pathlib import Path

from edge_gateway.buffer import TelemetryBuffer


def test_enqueue_and_count(tmp_path: Path):
    buffer = TelemetryBuffer(tmp_path / "buffer.db")
    assert buffer.count() == 0

    buffer.enqueue("evt-1", "vehicle/x/telemetry/powertrain/vehicle_speed_kph", b"{}")
    buffer.enqueue("evt-2", "vehicle/x/telemetry/powertrain/vehicle_speed_kph", b"{}")

    assert buffer.count() == 2


def test_peek_batch_preserves_fifo_order(tmp_path: Path):
    buffer = TelemetryBuffer(tmp_path / "buffer.db")
    for i in range(5):
        buffer.enqueue(f"evt-{i}", "some/topic", f"payload-{i}".encode())

    batch = buffer.peek_batch(limit=10)

    assert [row.event_id for row in batch] == [f"evt-{i}" for i in range(5)]
    # peek must not remove anything
    assert buffer.count() == 5


def test_remove_deletes_only_the_given_rows(tmp_path: Path):
    buffer = TelemetryBuffer(tmp_path / "buffer.db")
    for i in range(3):
        buffer.enqueue(f"evt-{i}", "some/topic", b"payload")
    batch = buffer.peek_batch(limit=10)

    buffer.remove([batch[0].id, batch[2].id])

    remaining = buffer.peek_batch(limit=10)
    assert [row.event_id for row in remaining] == ["evt-1"]
    assert buffer.count() == 1


def test_remove_with_empty_list_is_a_safe_no_op(tmp_path: Path):
    buffer = TelemetryBuffer(tmp_path / "buffer.db")
    buffer.enqueue("evt-0", "some/topic", b"payload")

    buffer.remove([])

    assert buffer.count() == 1


def test_buffer_persists_across_a_simulated_restart(tmp_path: Path):
    db_path = tmp_path / "buffer.db"

    first_run = TelemetryBuffer(db_path)
    first_run.enqueue("evt-restart", "some/topic", b"payload")
    first_run.close()

    # A brand new instance opened against the same file -- simulating the
    # gateway process restarting -- must see what was left unsent.
    second_run = TelemetryBuffer(db_path)
    try:
        assert second_run.count() == 1
        assert second_run.peek_batch(limit=10)[0].event_id == "evt-restart"
    finally:
        second_run.close()


def test_full_buffer_drops_oldest_to_make_room_for_newest(tmp_path: Path):
    buffer = TelemetryBuffer(tmp_path / "buffer.db", max_size=3)

    for i in range(5):  # 2 over the cap
        buffer.enqueue(f"evt-{i}", "some/topic", b"payload")

    remaining = buffer.peek_batch(limit=10)
    assert buffer.count() == 3
    # The oldest two (evt-0, evt-1) were dropped; the newest three remain,
    # still in FIFO order.
    assert [row.event_id for row in remaining] == ["evt-2", "evt-3", "evt-4"]
    assert buffer.dropped_count == 2


def test_drop_logs_a_warning_with_a_logger(tmp_path: Path):
    import logging

    class _RecordingHandler(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record)

    logger = logging.getLogger("test_buffer_drop_logging")
    handler = _RecordingHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)

    buffer = TelemetryBuffer(tmp_path / "buffer.db", max_size=1, logger=logger)
    buffer.enqueue("evt-0", "some/topic", b"payload")
    buffer.enqueue("evt-1", "some/topic", b"payload")  # pushes evt-0 out

    assert len(handler.records) == 1
    assert "dropped" in handler.records[0].getMessage()
