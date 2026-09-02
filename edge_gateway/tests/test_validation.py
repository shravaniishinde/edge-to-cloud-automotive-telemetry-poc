"""Unit tests for validation.py's range-checking logic."""

from common.telemetry_schema import ECUSource, SignalName, TelemetryEvent
from edge_gateway.validation import validate_event


def _event(**overrides):
    kwargs = dict(
        session_id="s1", vehicle_id="SIM-VEHICLE-01", source_ecu=ECUSource.BATTERY,
        can_id=0x200, signal_name=SignalName.BATTERY_SOC_PCT, value=50.0, unit="%",
    )
    kwargs.update(overrides)
    return TelemetryEvent(**kwargs)


def test_value_within_range_is_valid():
    result = validate_event(_event(value=50.0))
    assert result.is_valid is True
    assert result.reason is None


def test_value_above_range_is_rejected():
    # battery_soc_pct's valid_range is (0.0, 100.0) -- 500% is nonsense,
    # exactly the case TelemetryEvent's schema deliberately does not catch.
    result = validate_event(_event(value=500.0))
    assert result.is_valid is False
    assert "500" in result.reason
    assert "battery_soc_pct" in result.reason


def test_value_below_range_is_rejected():
    result = validate_event(_event(value=-10.0))
    assert result.is_valid is False


def test_boundary_values_are_valid():
    assert validate_event(_event(value=0.0)).is_valid is True
    assert validate_event(_event(value=100.0)).is_valid is True


def test_negative_current_is_valid_for_that_signal():
    # battery_current_a's valid_range is (-500.0, 500.0) -- negative is a
    # legitimate value (regen/charging), not something to reject.
    event = _event(
        source_ecu=ECUSource.BATTERY, can_id=0x202,
        signal_name=SignalName.BATTERY_CURRENT_A, value=-120.5, unit="A",
    )
    assert validate_event(event).is_valid is True
