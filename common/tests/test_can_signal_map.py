"""Tests for the CAN signal registry and the encode/decode round trip that
implements the "raw CAN frame <-> decoded engineering value" half of the
data flow described in ARCHITECTURE.md."""

import pytest

from common.can_signal_map import (
    CAN_PAYLOAD_LENGTH,
    SIGNAL_REGISTRY,
    UnknownCanIdError,
    decode_signal,
    decode_to_event,
    encode_signal,
)

# One representative physical value per registered signal, safely inside
# its documented valid range (see docs/can-signal-spec.md).
_SAMPLE_VALUES = {
    0x100: 62.3,     # vehicle_speed_kph
    0x101: 3200.0,   # engine_rpm
    0x102: 45.0,     # throttle_position_pct
    0x200: 78.5,     # battery_soc_pct
    0x201: 401.2,    # battery_voltage_v
    0x202: -120.5,   # battery_current_a (negative = regen/charging)
    0x203: 31.0,     # battery_temp_c
    0x300: 0b0101,   # door_status_bitmask (driver + rear-left open)
    0x301: 2.0,      # indicator_state (right)
    0x302: 45210.0,  # odometer_km
    0x303: 18.0,     # ambient_temp_c
}


def test_sample_values_cover_every_registered_signal():
    """Guards against silently forgetting to test a signal if the registry
    ever grows."""
    assert set(_SAMPLE_VALUES.keys()) == set(SIGNAL_REGISTRY.keys())


@pytest.mark.parametrize("can_id, physical_value", list(_SAMPLE_VALUES.items()))
def test_encode_decode_round_trip(can_id, physical_value):
    definition = SIGNAL_REGISTRY[can_id]

    frame = encode_signal(can_id, physical_value)
    assert len(frame) == CAN_PAYLOAD_LENGTH

    decoded = decode_signal(can_id, frame)
    assert decoded.can_id == can_id
    assert decoded.source_ecu == definition.ecu
    assert decoded.signal_name == definition.signal_name
    assert decoded.unit == definition.unit
    # The round trip is only as precise as the signal's own scale (e.g. a
    # scale of 0.1 can't perfectly preserve more than one decimal place).
    assert decoded.value == pytest.approx(physical_value, abs=definition.scale)


@pytest.mark.parametrize("can_id", list(SIGNAL_REGISTRY.keys()))
def test_unused_bytes_are_zero_filled(can_id):
    definition = SIGNAL_REGISTRY[can_id]
    frame = encode_signal(can_id, _SAMPLE_VALUES[can_id])
    assert frame[definition.byte_length:] == b"\x00" * (CAN_PAYLOAD_LENGTH - definition.byte_length)


def test_decode_unknown_can_id_raises():
    with pytest.raises(UnknownCanIdError):
        decode_signal(0x7FF, b"\x00" * CAN_PAYLOAD_LENGTH)


def test_encode_unknown_can_id_raises():
    with pytest.raises(UnknownCanIdError):
        encode_signal(0x7FF, 1.0)


def test_decode_to_event_builds_a_valid_telemetry_event():
    frame = encode_signal(0x203, 31.0)  # battery_temp_c
    event = decode_to_event(0x203, frame, session_id="session-1", vehicle_id="SIM-VEHICLE-01")
    assert event.signal_name.value == "battery_temp_c"
    assert event.can_id == 0x203
    assert event.value == pytest.approx(31.0, abs=1.0)
    assert event.session_id == "session-1"
