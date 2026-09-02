"""Unit tests for normalization.py -- topic naming and payload shape."""

import json

from common.telemetry_schema import ECUSource, SignalName, TelemetryEvent
from edge_gateway.normalization import build_payload, build_topic, normalize


def _event(**overrides):
    kwargs = dict(
        session_id="s1", vehicle_id="SIM-VEHICLE-01", source_ecu=ECUSource.POWERTRAIN,
        can_id=0x100, signal_name=SignalName.VEHICLE_SPEED_KPH, value=62.3, unit="km/h",
    )
    kwargs.update(overrides)
    return TelemetryEvent(**kwargs)


def test_topic_follows_vehicle_telemetry_ecu_signal_scheme():
    topic = build_topic(_event())
    assert topic == "vehicle/SIM-VEHICLE-01/telemetry/powertrain/vehicle_speed_kph"


def test_topic_reflects_a_different_ecu_and_signal():
    event = _event(
        source_ecu=ECUSource.BATTERY, can_id=0x200,
        signal_name=SignalName.BATTERY_SOC_PCT, value=80.0, unit="%",
    )
    assert build_topic(event) == "vehicle/SIM-VEHICLE-01/telemetry/battery/battery_soc_pct"


def test_payload_is_valid_json_matching_the_event():
    event = _event()
    payload = build_payload(event)
    decoded = json.loads(payload)
    assert decoded["value"] == 62.3
    assert decoded["can_id"] == 0x100
    assert decoded["event_id"] == event.event_id


def test_normalize_returns_topic_and_payload_together():
    event = _event()
    topic, payload = normalize(event)
    assert topic == build_topic(event)
    assert payload == build_payload(event)
