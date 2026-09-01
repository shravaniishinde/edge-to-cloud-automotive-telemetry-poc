"""
Simulated Powertrain ECU.

Owns 3 CAN messages (see docs/can-signal-spec.md): vehicle speed, engine
RPM, and throttle position. All three are generated and sent together, at
this ECU's single rate of 10 Hz -- the fastest-changing, most time-critical
signals in the simulated vehicle.
"""

from __future__ import annotations

import random
from typing import List, Optional

from common.telemetry_schema import ECUSource, SignalName, TelemetryEvent

CAN_ID_VEHICLE_SPEED = 0x100
CAN_ID_ENGINE_RPM = 0x101
CAN_ID_THROTTLE_POSITION = 0x102


class PowertrainECU:
    """
    Generates plausible, smoothly-drifting powertrain telemetry rather than
    pure random noise -- a real vehicle's speed/RPM/throttle move gradually
    tick to tick, not erratically. Speed and RPM are also nudged by the
    current throttle position, so the three signals move together the way
    a real drivetrain would.
    """

    TICK_INTERVAL_SECONDS = 0.1  # 10 Hz

    def __init__(self, session_id: str, vehicle_id: str, rng_seed: Optional[int] = None) -> None:
        self._session_id = session_id
        self._vehicle_id = vehicle_id
        self._rng = random.Random(rng_seed)
        # Starting state: vehicle stationary, engine idling, foot off the pedal.
        self._speed_kph = 0.0
        self._rpm = 800.0
        self._throttle_pct = 0.0

    @property
    def speed_kph(self) -> float:
        """Current live speed, for the Phase 2 UDS server to expose over
        DID 0x1001. Read-only: only `tick()` may change ECU state."""
        return self._speed_kph

    @property
    def rpm(self) -> float:
        """Current live RPM, for the Phase 2 UDS server to expose over
        DID 0x1002."""
        return self._rpm

    def tick(self) -> List[TelemetryEvent]:
        """Advance the simulated state by one step and return the 3 readings."""
        self._throttle_pct = self._drift(self._throttle_pct, step=5.0, low=0.0, high=100.0)
        self._speed_kph = self._drift(
            self._speed_kph, step=2.0 + self._throttle_pct * 0.05, low=0.0, high=250.0
        )
        self._rpm = self._drift(
            self._rpm, step=100.0 + self._throttle_pct * 5.0, low=800.0, high=8000.0
        )

        return [
            self._make_event(CAN_ID_VEHICLE_SPEED, SignalName.VEHICLE_SPEED_KPH, self._speed_kph, "km/h"),
            self._make_event(CAN_ID_ENGINE_RPM, SignalName.ENGINE_RPM, self._rpm, "rpm"),
            self._make_event(CAN_ID_THROTTLE_POSITION, SignalName.THROTTLE_POSITION_PCT, self._throttle_pct, "%"),
        ]

    def _drift(self, current: float, step: float, low: float, high: float) -> float:
        """Move `current` by a random amount in [-step, step], clamped to [low, high]."""
        delta = self._rng.uniform(-step, step)
        return max(low, min(high, current + delta))

    def _make_event(self, can_id: int, signal_name: SignalName, value: float, unit: str) -> TelemetryEvent:
        return TelemetryEvent(
            session_id=self._session_id,
            vehicle_id=self._vehicle_id,
            source_ecu=ECUSource.POWERTRAIN,
            can_id=can_id,
            signal_name=signal_name,
            value=round(value, 2),
            unit=unit,
        )
