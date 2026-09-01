"""Tests for DiagnosticEvent and the shared UDS constants/codecs (structural
validation only, same philosophy as test_telemetry_schema.py)."""

import pytest
from pydantic import ValidationError

from common.diagnostic_schema import (
    DEFAULT_SIMULATED_VIN,
    DID_CODECS,
    DID_ENGINE_RPM,
    DID_VEHICLE_SPEED_KPH,
    DID_VIN,
    ScaledUint16Codec,
    DiagnosticEvent,
)
from common.telemetry_schema import DEFAULT_VEHICLE_ID, ECUSource


def _valid_kwargs(**overrides):
    kwargs = dict(
        session_id="11111111-1111-1111-1111-111111111111",
        vehicle_id=DEFAULT_VEHICLE_ID,
        source_ecu=ECUSource.POWERTRAIN,
        service_id=0x22,
        service_name="ReadDataByIdentifier",
        request_summary="DID=0xF190",
        response_summary=f"VIN={DEFAULT_SIMULATED_VIN}",
        is_positive_response=True,
    )
    kwargs.update(overrides)
    return kwargs


def test_valid_event_constructs_successfully():
    event = DiagnosticEvent(**_valid_kwargs())
    assert event.is_positive_response is True
    assert event.negative_response_code is None
    assert event.schema_version == "1.0"
    assert event.event_id


def test_negative_response_carries_a_code():
    event = DiagnosticEvent(**_valid_kwargs(
        is_positive_response=False, negative_response_code="RequestOutOfRange",
    ))
    assert event.negative_response_code == "RequestOutOfRange"


def test_missing_required_field_raises():
    kwargs = _valid_kwargs()
    del kwargs["service_id"]
    with pytest.raises(ValidationError):
        DiagnosticEvent(**kwargs)


def test_unknown_source_ecu_raises():
    with pytest.raises(ValidationError):
        DiagnosticEvent(**_valid_kwargs(source_ecu="engine_control_unit_9000"))


def test_each_event_gets_a_unique_event_id():
    event_a = DiagnosticEvent(**_valid_kwargs())
    event_b = DiagnosticEvent(**_valid_kwargs())
    assert event_a.event_id != event_b.event_id


def test_vehicle_id_and_vin_are_independent_values():
    """The specific adjustment the user required before approving Phase 2:
    vehicle_id and the synthetic VIN must be two unrelated constants, and
    the VIN must not simply echo vehicle_id."""
    assert DEFAULT_SIMULATED_VIN != DEFAULT_VEHICLE_ID
    assert len(DEFAULT_SIMULATED_VIN) == 17  # matches real VIN length


class TestScaledUint16Codec:
    def test_round_trip_matches_can_signal_map_scale(self):
        # Same scale as CAN 0x100 (vehicle_speed_kph) in can_signal_map.py --
        # deliberately reusing that convention rather than a new one.
        codec = ScaledUint16Codec(scale=0.1)
        encoded = codec.encode(62.3)
        assert codec.decode(encoded) == pytest.approx(62.3, abs=0.1)

    def test_clamps_out_of_range_values(self):
        codec = ScaledUint16Codec(scale=0.1)
        encoded = codec.encode(999999.0)  # would overflow uint16 * scale
        assert len(encoded) == 2  # never raises, always produces a valid uint16 payload


def test_did_codecs_registry_covers_all_supported_dids():
    assert set(DID_CODECS.keys()) == {DID_VIN, DID_VEHICLE_SPEED_KPH, DID_ENGINE_RPM}
    assert DID_CODECS[DID_VIN].decode(DID_CODECS[DID_VIN].encode(DEFAULT_SIMULATED_VIN)) == DEFAULT_SIMULATED_VIN
