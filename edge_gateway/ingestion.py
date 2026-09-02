"""
Ingestion: raw CAN frame -> TelemetryEvent (or nothing, if the frame isn't
ours to interpret).

The gateway listens on the same virtual CAN bus the ECUs publish to *and*
the UDS client/server (Phase 2) exchanges frames on -- there is only one
shared bus in this simulation, exactly as there'd be only one physical bus
in a real vehicle. A real gateway's CAN interface has to filter for the
message IDs it actually cares about rather than assuming every frame it
sees is telemetry; this module does the same thing by checking each
frame's arbitration ID against the known telemetry registry
(`common/can_signal_map.SIGNAL_REGISTRY`) before attempting to decode it.
"""

from __future__ import annotations

import logging
from typing import Optional

import can

from common.can_signal_map import SIGNAL_REGISTRY, decode_to_event
from common.telemetry_schema import TelemetryEvent

# The set of CAN IDs this gateway knows how to decode as telemetry. Anything
# else on the bus (e.g. the Phase 2 UDS request/response IDs 0x7E0/0x7E8)
# is deliberately ignored here, not an error -- a real gateway's CAN
# interface would apply a hardware or software filter the same way instead
# of trying to interpret traffic it has no schema for.
TELEMETRY_CAN_IDS = frozenset(SIGNAL_REGISTRY.keys())


def ingest_frame(
    message: can.Message,
    *,
    session_id: str,
    vehicle_id: str,
    logger: Optional[logging.Logger] = None,
) -> Optional[TelemetryEvent]:
    """
    Turn one raw CAN frame into a TelemetryEvent, or return None if the
    frame isn't recognised telemetry (wrong CAN ID) or is malformed
    (wrong payload length for its CAN ID). `session_id`/`vehicle_id` are
    supplied by the caller (the gateway) because raw CAN frames carry
    neither -- see docs/edge-gateway-spec.md for why session_id is now
    gateway-owned rather than simulator-owned.
    """
    if message.arbitration_id not in TELEMETRY_CAN_IDS:
        if logger is not None:
            logger.debug(
                "ignoring non-telemetry frame", extra={
                    "component": "ingestion", "can_id": message.arbitration_id,
                },
            )
        return None

    try:
        event = decode_to_event(
            message.arbitration_id, bytes(message.data),
            session_id=session_id, vehicle_id=vehicle_id,
        )
    except ValueError as exc:
        # Malformed payload (wrong length) for an otherwise-known CAN ID.
        # Dropped and logged rather than raised -- one bad frame shouldn't
        # crash the ingestion loop.
        if logger is not None:
            logger.warning(
                "dropping malformed frame: %s", exc, extra={
                    "component": "ingestion", "can_id": message.arbitration_id,
                },
            )
        return None

    if logger is not None:
        logger.debug(
            "ingested frame", extra={
                "component": "ingestion", "can_id": event.can_id,
                "signal_name": event.signal_name.value, "event_id": event.event_id,
            },
        )
    return event
