"""Unit tests for the fault-injection helpers -- no CAN bus or MQTT
broker involved. force_publish_failures() is tested against a real
MqttPublisher instance that's simply never connected (publish() would
already return False on its own for that reason, so these tests connect
it conceptually by checking the *forced-failure* path specifically via
the publisher's own injected-failure counter, not real connectivity)."""

import pytest

from common.can_signal_map import UnknownCanIdError
from edge_gateway.fault_injection import (
    force_publish_failures,
    malformed_frame,
    out_of_range_frame,
    simulated_connection_outage,
)
from edge_gateway.ingestion import ingest_frame
from edge_gateway.mqtt_publisher import INITIAL_RECONNECT_BACKOFF_SECONDS, MqttPublisher
from edge_gateway.validation import validate_event
from common.can_signal_map import decode_to_event


def test_force_publish_failures_makes_publish_return_false_n_times():
    publisher = MqttPublisher(host="localhost", port=1883)
    publisher._connected = True  # bypass real connect() -- we're only testing the fault-injection seam

    with force_publish_failures(publisher, count=2):
        assert publisher.publish("some/topic", b"payload") is False
        assert publisher.publish("some/topic", b"payload") is False
        # the forced-failure count is exhausted after 2 calls; a 3rd call
        # falls through to the real client and would only fail because
        # there's no real broker connection underneath this test.

    # after the context manager exits, no failures are left "queued"
    assert publisher._forced_failures_remaining == 0


def test_force_publish_failures_restores_on_exception():
    publisher = MqttPublisher(host="localhost", port=1883)

    with pytest.raises(RuntimeError):
        with force_publish_failures(publisher, count=5):
            raise RuntimeError("boom")

    assert publisher._forced_failures_remaining == 0


def test_malformed_frame_is_dropped_by_ingestion():
    frame = malformed_frame(0x100)
    event = ingest_frame(frame, session_id="s1", vehicle_id="SIM-VEHICLE-01")
    assert event is None


def test_malformed_frame_rejects_unknown_can_id():
    with pytest.raises(UnknownCanIdError):
        malformed_frame(0x999)


def test_out_of_range_frame_is_rejected_by_validation():
    frame = out_of_range_frame(0x200, 250.0)  # battery_soc_pct valid_range is (0.0, 100.0)
    event = decode_to_event(frame.arbitration_id, bytes(frame.data), session_id="s1", vehicle_id="SIM-VEHICLE-01")

    result = validate_event(event)

    assert result.is_valid is False


# --- Phase 9: simulated connection outage ---


def _connected_publisher(monkeypatch):
    """A publisher that believes it is connected, with paho's network calls
    replaced by recorders -- no broker needed."""
    publisher = MqttPublisher(host="localhost", port=1883)
    publisher._connected = True  # simulate a prior successful connect()
    calls = {"reconnect": 0, "disconnect": 0, "loop_stop": 0}
    for name in calls:
        monkeypatch.setattr(publisher._client, name, lambda *a, _n=name, **k: calls.__setitem__(_n, calls[_n] + 1))
    return publisher, calls


def test_simulated_outage_makes_the_gateway_see_a_disconnect(monkeypatch):
    publisher, _calls = _connected_publisher(monkeypatch)

    with simulated_connection_outage(publisher):
        assert publisher.is_connected() is False
        assert publisher.publish("some/topic", b"payload") is False
        assert publisher.try_reconnect(now=0.0) is False  # fails through the real backoff path
        assert publisher.try_reconnect(now=0.5) is False  # still inside the 1s backoff window
        assert publisher.try_reconnect(now=1.0) is False  # attempted again, still down
        assert publisher._reconnect_backoff_seconds == INITIAL_RECONNECT_BACKOFF_SECONDS * 4


def test_recovery_is_only_observed_by_the_next_backoff_gated_reconnect(monkeypatch):
    publisher, calls = _connected_publisher(monkeypatch)

    with simulated_connection_outage(publisher):
        publisher.try_reconnect(now=0.0)  # fails; next attempt allowed at t=1.0

    assert publisher.is_connected() is False  # clearing the fault alone doesn't reconnect
    assert publisher.try_reconnect(now=0.5) is False  # backoff still applies
    assert publisher.try_reconnect(now=1.0) is True
    assert publisher.is_connected() is True
    assert calls["reconnect"] == 0  # the real socket was never closed, so it isn't re-opened


def test_simulated_outage_clears_even_if_the_block_raises(monkeypatch):
    publisher, _calls = _connected_publisher(monkeypatch)

    with pytest.raises(RuntimeError):
        with simulated_connection_outage(publisher):
            raise RuntimeError("boom")

    assert publisher.try_reconnect(now=10.0) is True


def test_disconnect_during_a_simulated_outage_still_closes_the_real_client(monkeypatch):
    publisher, calls = _connected_publisher(monkeypatch)
    publisher.inject_connection_outage()

    publisher.disconnect()

    assert calls["loop_stop"] == 1 and calls["disconnect"] == 1
