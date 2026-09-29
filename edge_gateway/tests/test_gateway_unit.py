"""
Unit-level tests for EdgeGateway.run_once()'s Phase 4 reconnect/replay
wiring: does it call publisher.try_reconnect() when disconnected, and
does a successful reconnect trigger a replay attempt? These use mocked
bus/publisher/buffer so the control flow is verified deterministically
and fast, without a real CAN bus or MQTT broker.

The real, real-broker version of the buffering/replay/ordering behavior
itself (not just this wiring) is covered by test_gateway_integration.py.
"""

import threading
from unittest.mock import MagicMock

from edge_gateway.buffer import BufferedEvent
from edge_gateway.gateway import EdgeGateway


def _make_gateway(connected: bool):
    bus = MagicMock()
    bus.recv.return_value = None  # no CAN frame this tick -- isolates the reconnect/replay check
    publisher = MagicMock()
    publisher.is_connected.return_value = connected
    buffer = MagicMock()
    buffer.peek_batch.return_value = []
    buffer.dropped_count = 0  # read by EdgeGateway.metrics_snapshot() (Phase 7)
    gateway = EdgeGateway(bus, publisher, buffer, session_id="unit-test-session")
    return gateway, bus, publisher, buffer


def test_run_once_does_not_check_reconnect_when_already_connected():
    gateway, _bus, publisher, _buffer = _make_gateway(connected=True)

    gateway.run_once()

    publisher.try_reconnect.assert_not_called()


def test_run_once_attempts_reconnect_when_disconnected():
    gateway, _bus, publisher, _buffer = _make_gateway(connected=False)
    publisher.try_reconnect.return_value = False

    gateway.run_once()

    publisher.try_reconnect.assert_called_once()


def test_run_once_replays_the_buffer_immediately_after_a_successful_reconnect():
    gateway, _bus, publisher, buffer = _make_gateway(connected=False)

    publisher.try_reconnect.return_value = True
    publisher.is_connected.side_effect = [False, True]

    gateway.run_once()

    buffer.peek_batch.assert_called()

def test_run_once_does_not_replay_when_reconnect_fails():
    gateway, _bus, publisher, buffer = _make_gateway(connected=False)
    publisher.try_reconnect.return_value = False

    gateway.run_once()

    buffer.peek_batch.assert_not_called()


# --- Phase 8: run() lifecycle and strict-FIFO partial replay ---


def test_run_returns_immediately_when_stop_event_is_already_set():
    gateway, bus, _publisher, buffer = _make_gateway(connected=True)
    buffer.count.return_value = 0
    stop_event = threading.Event()
    stop_event.set()

    gateway.run(stop_event)

    bus.recv.assert_not_called()


def test_run_replays_a_persisted_backlog_on_startup_without_a_reconnect():
    gateway, bus, publisher, buffer = _make_gateway(connected=True)
    buffer.count.return_value = 3  # left behind by a previous run
    stop_event = threading.Event()
    bus.recv.side_effect = lambda timeout: stop_event.set()  # exactly one loop iteration

    gateway.run(stop_event)

    publisher.try_reconnect.assert_not_called()
    buffer.peek_batch.assert_called()


def test_replay_stops_at_the_first_unconfirmed_row_and_removes_only_what_was_confirmed():
    gateway, _bus, publisher, buffer = _make_gateway(connected=True)
    rows = [BufferedEvent(id=i, event_id=f"evt-{i}", topic="t", payload=b"p", enqueued_at=0.0) for i in (1, 2, 3)]
    buffer.peek_batch.return_value = rows
    publisher.publish.side_effect = [True, False, True]  # row 2 fails; row 3 must NOT be sent ahead of it

    drained = gateway._replay_buffered()

    assert drained is False
    assert publisher.publish.call_count == 2
    buffer.remove.assert_called_once_with([1])
    assert gateway.replayed_count == 1
