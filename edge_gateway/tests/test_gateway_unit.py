"""
Unit-level tests for EdgeGateway.run_once()'s Phase 4 reconnect/replay
wiring: does it call publisher.try_reconnect() when disconnected, and
does a successful reconnect trigger a replay attempt? These use mocked
bus/publisher/buffer so the control flow is verified deterministically
and fast, without a real CAN bus or MQTT broker.

The real, real-broker version of the buffering/replay/ordering behavior
itself (not just this wiring) is covered by test_gateway_integration.py.
"""

from unittest.mock import MagicMock

from edge_gateway.gateway import EdgeGateway


def _make_gateway(connected: bool):
    bus = MagicMock()
    bus.recv.return_value = None  # no CAN frame this tick -- isolates the reconnect/replay check
    publisher = MagicMock()
    publisher.is_connected.return_value = connected
    buffer = MagicMock()
    buffer.peek_batch.return_value = []
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
