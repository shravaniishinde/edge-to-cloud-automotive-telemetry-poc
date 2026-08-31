"""
Simulated Body/Status ECU.

Owns 4 CAN messages (see docs/can-signal-spec.md): door status bitmask,
turn indicator state, odometer, and ambient temperature. All four are
generated and sent together, at this ECU's single rate of 1 Hz -- the
slowest-changing signals in the simulated vehicle. Two of them (doors,
indicator) behave like discrete real-world events rather than a smooth
drift, so they only change occasionally instead of every tick.
"""

from __future__ import annotations

import random
from typing import List, Optional

from common.telemetry_schema import ECUSource, SignalName, TelemetryEvent

CAN_ID_DOOR_STATUS = 0x300
CAN_ID_INDICATOR = 0x301
CAN_ID_ODOMETER = 0x302
CAN_ID_AMBIENT_TEMP = 0x303

# Bit positions within door_status_bitmask (see can-signal-spec.md).
DOOR_BIT_DRIVER = 0
DOOR_BIT_PASSENGER = 1
DOOR_BIT_REAR_LEFT = 2
DOOR_BIT_REAR_RIGHT = 3


class BodyECU:
    TICK_INTERVAL_SECONDS = 1.0  # 1 Hz

    def __init__(self, session_id: str, vehicle_id: str, rng_seed: Optional[int] = None) -> None:
        self._session_id = session_id
        self._vehicle_id = vehicle_id
        self._rng = random.Random(rng_seed)
        self._door_status = 0  # all doors closed
        self._indicator_state = 0  # off
        self._odometer_km = 12500.0
        self._ambient_temp_c = 22.0

    def tick(self) -> List[TelemetryEvent]:
        """Advance the simulated state by one step and return the 4 readings."""
        # Doors and the indicator behave like discrete events: they only
        # change on an occasional coin flip, not a continuous drift.
        if self._rng.random() < 0.05:
            bit = self._rng.randint(DOOR_BIT_DRIVER, DOOR_BIT_REAR_RIGHT)
            self._door_status ^= 1 << bit
        if self._rng.random() < 0.1:
            self._indicator_state = self._rng.randint(0, 3)
        # The odometer only ever counts up.
        self._odometer_km += self._rng.uniform(0.0, 0.02)
        self._ambient_temp_c = self._drift(self._ambient_temp_c, step=0.2, low=-40.0, high=60.0)

        return [
            self._make_event(CAN_ID_DOOR_STATUS, SignalName.DOOR_STATUS_BITMASK, float(self._door_status), "bitmask"),
            self._make_event(CAN_ID_INDICATOR, SignalName.INDICATOR_STATE, float(self._indicator_state), "enum"),
            self._make_event(CAN_ID_ODOMETER, SignalName.ODOMETER_KM, self._odometer_km, "km"),
            self._make_event(CAN_ID_AMBIENT_TEMP, SignalName.AMBIENT_TEMP_C, self._ambient_temp_c, "degC"),
        ]

    def _drift(self, current: float, step: float, low: float, high: float) -> float:
        delta = self._rng.uniform(-step, step)
        return max(low, min(high, current + delta))

    def _make_event(self, can_id: int, signal_name: SignalName, value: float, unit: str) -> TelemetryEvent:
        return TelemetryEvent(
            session_id=self._session_id,
            vehicle_id=self._vehicle_id,
            source_ecu=ECUSource.BODY,
            can_id=can_id,
            signal_name=signal_name,
            value=round(value, 2),
            unit=unit,
        )
