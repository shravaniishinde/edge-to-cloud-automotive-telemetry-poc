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
)
from edge_gateway.ingestion import ingest_frame
from edge_gateway.mqtt_publisher import MqttPublisher
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
