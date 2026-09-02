"""
Validation: the physical-plausibility check Phase 1 deliberately left out
of TelemetryEvent itself (see the design note at the top of
common/telemetry_schema.py). A battery at 500% charge is shaped correctly
-- Pydantic already confirmed that -- but it's nonsense, and this is where
that finally gets caught.

Reuses `common/can_signal_map.SIGNAL_REGISTRY`'s `valid_range` per signal
rather than inventing a second range table -- the same shared-model
principle behind every other component in this project.
"""

from __future__ import annotations

from typing import NamedTuple, Optional

from common.can_signal_map import SIGNAL_REGISTRY
from common.telemetry_schema import TelemetryEvent


class ValidationResult(NamedTuple):
    is_valid: bool
    reason: Optional[str]  # None when is_valid is True


def validate_event(event: TelemetryEvent) -> ValidationResult:
    """
    Checks `event.value` against its signal's registered valid_range.
    Assumes `event.can_id` is a known telemetry ID -- ingestion.py already
    filters to those before validation ever sees an event, so a KeyError
    here would indicate a real bug, not bad input data, and is allowed to
    propagate rather than being silently swallowed.
    """
    definition = SIGNAL_REGISTRY[event.can_id]
    low, high = definition.valid_range
    if not (low <= event.value <= high):
        return ValidationResult(
            is_valid=False,
            reason=(
                f"{event.signal_name.value}={event.value} is outside the valid "
                f"range [{low}, {high}] {definition.unit} for CAN ID 0x{event.can_id:03X}"
            ),
        )
    return ValidationResult(is_valid=True, reason=None)
