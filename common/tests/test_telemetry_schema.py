"""Tests for the canonical TelemetryEvent schema (structural validation only
-- see the design note at the top of common/telemetry_schema.py for why
physical-plausibility checks are deliberately NOT here)."""

import pytest
from pydantic import ValidationError

from common.telemetry_schema import ECUSource, SignalName, TelemetryEvent


def _valid_kwargs(**overrides):
    kwargs = dict(
        session_id="11111111-1111-1111-1111-111111111111",
        vehicle_id="SIM-VEHICLE-01",
        source_ecu=ECUSource.POWERTRAIN,
        can_id=0x100,
        signal_name=SignalName.VEHICLE_SPEED_KPH,
        value=62.3,
        unit="km/h",
    )
    kwargs.update(overrides)
    return kwargs


def test_valid_event_constructs_successfully():
    event = TelemetryEvent(**_valid_kwargs())
    assert event.value == 62.3
    assert event.source_ecu == ECUSource.POWERTRAIN
    assert event.signal_name == SignalName.VEHICLE_SPEED_KPH
    assert event.schema_version == "1.0"
    # event_id and timestamp are auto-generated, not supplied by the caller.
    assert event.event_id
    assert event.timestamp is not None


def test_missing_required_field_raises():
    kwargs = _valid_kwargs()
    del kwargs["value"]
    with pytest.raises(ValidationError):
        TelemetryEvent(**kwargs)


def test_wrong_type_for_value_raises():
    with pytest.raises(ValidationError):
        TelemetryEvent(**_valid_kwargs(value="fast"))


def test_unknown_source_ecu_raises():
    with pytest.raises(ValidationError):
        TelemetryEvent(**_valid_kwargs(source_ecu="engine_control_unit_9000"))


def test_unknown_signal_name_raises():
    with pytest.raises(ValidationError):
        TelemetryEvent(**_valid_kwargs(signal_name="warp_core_temperature"))


def test_implausible_value_is_NOT_rejected_by_the_schema():
    """
    Deliberate: TelemetryEvent only validates shape, not physical
    plausibility. A battery at 500% charge is nonsense, but it's shaped
    correctly, so the schema accepts it -- catching this is the Edge
    Gateway's job starting Phase 3.
    """
    event = TelemetryEvent(**_valid_kwargs(
        source_ecu=ECUSource.BATTERY, can_id=0x200,
        signal_name=SignalName.BATTERY_SOC_PCT, value=500.0, unit="%",
    ))
    assert event.value == 500.0


def test_each_event_gets_a_unique_event_id():
    event_a = TelemetryEvent(**_valid_kwargs())
    event_b = TelemetryEvent(**_valid_kwargs())
    assert event_a.event_id != event_b.event_id
