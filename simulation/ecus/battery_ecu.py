"""
Simulated Battery/Energy ECU.

Owns 4 CAN messages (see docs/can-signal-spec.md): state of charge, pack
voltage, pack current, and pack temperature. All four are generated and
sent together, at this ECU's single rate of 2 Hz -- slower than powertrain
signals, matching how energy-system readings evolve more gradually than
driveline signals in a real vehicle.
"""

from __future__ import annotations

import random
from typing import List, Optional

from common.telemetry_schema import ECUSource, SignalName, TelemetryEvent

CAN_ID_SOC = 0x200
CAN_ID_VOLTAGE = 0x201
CAN_ID_CURRENT = 0x202
CAN_ID_TEMP = 0x203


class BatteryECU:
    """
    Generates plausible battery telemetry. State of charge drains slowly
    over time (a small negative bias rather than a symmetric random walk);
    pack current swings both positive (drawing power) and negative
    (regenerative braking / charging), which is exactly why it's the one
    signal on this ECU encoded as signed (see can-signal-spec.md).
    """

    TICK_INTERVAL_SECONDS = 0.5  # 2 Hz

    def __init__(self, session_id: str, vehicle_id: str, rng_seed: Optional[int] = None) -> None:
        self._session_id = session_id
        self._vehicle_id = vehicle_id
        self._rng = random.Random(rng_seed)
        self._soc_pct = 80.0
        self._voltage_v = 400.0
        self._current_a = 0.0
        self._temp_c = 25.0

    def tick(self) -> List[TelemetryEvent]:
        """Advance the simulated state by one step and return the 4 readings."""
        self._soc_pct = self._drift(self._soc_pct, step=0.3, low=0.0, high=100.0, bias=-0.05)
        self._voltage_v = self._drift(self._voltage_v, step=2.0, low=300.0, high=420.0)
        self._current_a = self._drift(self._current_a, step=15.0, low=-500.0, high=500.0)
        self._temp_c = self._drift(self._temp_c, step=0.5, low=-40.0, high=120.0)

        return [
            self._make_event(CAN_ID_SOC, SignalName.BATTERY_SOC_PCT, self._soc_pct, "%"),
            self._make_event(CAN_ID_VOLTAGE, SignalName.BATTERY_VOLTAGE_V, self._voltage_v, "V"),
            self._make_event(CAN_ID_CURRENT, SignalName.BATTERY_CURRENT_A, self._current_a, "A"),
            self._make_event(CAN_ID_TEMP, SignalName.BATTERY_TEMP_C, self._temp_c, "degC"),
        ]

    def _drift(self, current: float, step: float, low: float, high: float, bias: float = 0.0) -> float:
        """Move `current` by a random amount in [-step, step] plus a fixed bias,
        clamped to [low, high]."""
        delta = self._rng.uniform(-step, step) + bias
        return max(low, min(high, current + delta))

    def _make_event(self, can_id: int, signal_name: SignalName, value: float, unit: str) -> TelemetryEvent:
        return TelemetryEvent(
            session_id=self._session_id,
            vehicle_id=self._vehicle_id,
            source_ecu=ECUSource.BATTERY,
            can_id=can_id,
            signal_name=signal_name,
            value=round(value, 2),
            unit=unit,
        )
