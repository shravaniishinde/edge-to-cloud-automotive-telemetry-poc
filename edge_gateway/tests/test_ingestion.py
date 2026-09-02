"""Unit tests for ingestion.py -- no CAN bus involved, just real
can.Message objects built directly (the same objects a bus.recv() would
hand back)."""

import can
import pytest

from common.can_signal_map import encode_signal
from edge_gateway.ingestion import ingest_frame


def _telemetry_frame(can_id: int, physical_value: float) -> can.Message:
    return can.Message(arbitration_id=can_id, data=encode_signal(can_id, physical_value), is_extended_id=False)


def test_ingest_known_telemetry_frame_returns_an_event():
    frame = _telemetry_frame(0x100, 62.3)
    event = ingest_frame(frame, session_id="s1", vehicle_id="SIM-VEHICLE-01")

    assert event is not None
    assert event.can_id == 0x100
    assert event.value == pytest.approx(62.3, abs=0.1)
    assert event.session_id == "s1"
    assert event.vehicle_id == "SIM-VEHICLE-01"


def test_ingest_ignores_uds_request_frame():
    # 0x7E0 is the Phase 2 UDS tester->ECU CAN ID, sharing the same bus.
    # The gateway must not try to decode it as telemetry.
    frame = can.Message(arbitration_id=0x7E0, data=bytes(8), is_extended_id=False)
    event = ingest_frame(frame, session_id="s1", vehicle_id="SIM-VEHICLE-01")
    assert event is None


def test_ingest_ignores_uds_response_frame():
    frame = can.Message(arbitration_id=0x7E8, data=bytes(8), is_extended_id=False)
    event = ingest_frame(frame, session_id="s1", vehicle_id="SIM-VEHICLE-01")
    assert event is None


def test_ingest_drops_malformed_payload_for_known_can_id():
    # 0x100 (vehicle_speed_kph) requires 8 bytes; this frame has only 2.
    frame = can.Message(arbitration_id=0x100, data=bytes(2), is_extended_id=False)
    event = ingest_frame(frame, session_id="s1", vehicle_id="SIM-VEHICLE-01")
    assert event is None
