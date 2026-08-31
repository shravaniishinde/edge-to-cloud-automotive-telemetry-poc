"""
Canonical telemetry data model, shared by every component in this project.

This file is deliberately dependency-free of any single component (the
simulator, and from Phase 3 onward the Edge Gateway, analyzer, and
dashboard). Every part of the system imports TelemetryEvent from here
rather than inventing its own idea of "what a telemetry reading looks
like" -- that's the whole point of a shared schema: one contract, many
consumers.

Design note (read this before adding range checks here):
TelemetryEvent validates *shape* -- correct types, required fields present,
source_ecu/signal_name are values we actually recognise. It deliberately
does NOT validate physical plausibility (e.g. rejecting battery_soc_pct
= 500). That kind of check belongs to the Edge Gateway's validation logic,
introduced in Phase 3. If this schema refused implausible values outright,
Phase 4's fault-injection scenarios (out-of-range sensor values) would have
no way to even construct the bad data needed to test the gateway's
defenses. So: Pydantic enforces the contract's shape here; a later,
separate component enforces whether the data makes physical sense.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

# Schema version for this event shape. Bump this if TelemetryEvent's fields
# ever change in a way that could break an older consumer (e.g. a stored
# log file, or a component that hasn't been updated yet).
SCHEMA_VERSION = "1.0"

# Only one simulated vehicle exists in this POC, so this is a constant for
# now rather than something passed around at runtime. Keeping vehicle_id on
# the schema (instead of leaving it out) means the data model is already
# honest about the fact that a real deployment would have many vehicles --
# at zero extra implementation cost today.
DEFAULT_VEHICLE_ID = "SIM-VEHICLE-01"


class ECUSource(str, Enum):
    """The logical ECU that produced a telemetry reading."""

    POWERTRAIN = "powertrain"
    BATTERY = "battery"
    BODY = "body"


class SignalName(str, Enum):
    """
    Every signal this vehicle simulation can produce -- see
    docs/can-signal-spec.md for the full CAN ID / byte-layout definition
    of each one. Kept as a closed set (an Enum) rather than a free string
    so a typo like "vehcile_speed_kph" is rejected immediately by Pydantic
    instead of silently becoming a new, unrecognised signal.
    """

    VEHICLE_SPEED_KPH = "vehicle_speed_kph"
    ENGINE_RPM = "engine_rpm"
    THROTTLE_POSITION_PCT = "throttle_position_pct"
    BATTERY_SOC_PCT = "battery_soc_pct"
    BATTERY_VOLTAGE_V = "battery_voltage_v"
    BATTERY_CURRENT_A = "battery_current_a"
    BATTERY_TEMP_C = "battery_temp_c"
    DOOR_STATUS_BITMASK = "door_status_bitmask"
    INDICATOR_STATE = "indicator_state"
    ODOMETER_KM = "odometer_km"
    AMBIENT_TEMP_C = "ambient_temp_c"


class TelemetryEvent(BaseModel):
    """
    A single, decoded telemetry reading -- the canonical unit of data that
    flows through this whole system from Phase 1 onward.

    Note what "decoded" means here: `value` is always the physical
    engineering value (e.g. 62.3 km/h), never the raw integer that was
    actually packed into the CAN frame's bytes (e.g. 623). The raw-bytes
    representation is a wire-format detail that common/can_signal_map.py
    knows how to produce and consume; TelemetryEvent only ever holds the
    human-meaningful result.
    """

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    vehicle_id: str = DEFAULT_VEHICLE_ID
    source_ecu: ECUSource
    can_id: int
    signal_name: SignalName
    value: float
    unit: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: str = SCHEMA_VERSION
