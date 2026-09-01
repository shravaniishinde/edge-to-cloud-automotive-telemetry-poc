"""
Shared UDS diagnostic data model (Phase 2).

Mirrors the philosophy of `common/telemetry_schema.py`: one definition of
"what a diagnostic interaction looks like," reused by the UDS server, the
UDS client/tester, and their tests, instead of each component inventing
its own shape.

See docs/uds-spec.md for the full, human-readable UDS specification this
file implements (CAN IDs, supported services, DID/DTC tables).
"""

from __future__ import annotations

import struct
import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field
from udsoncan import AsciiCodec, DidCodec

from common.telemetry_schema import DEFAULT_VEHICLE_ID, ECUSource

SCHEMA_VERSION = "1.0"

# --- UDS CAN IDs (physical addressing, the standard OBD-II/UDS pair) ---
# Deliberately distinct from the 11 telemetry CAN IDs (0x100-0x303) so the
# two traffic patterns (broadcast telemetry vs. request/response
# diagnostics) never collide on the bus.
UDS_REQUEST_CAN_ID = 0x7E0   # tester -> ECU
UDS_RESPONSE_CAN_ID = 0x7E8  # ECU -> tester

# --- Identity: vehicle_id vs. VIN, kept deliberately separate ---
#
# `vehicle_id` (DEFAULT_VEHICLE_ID, imported from common.telemetry_schema)
# is this project's OWN identifier for a simulated vehicle -- the same
# field TelemetryEvent already carries. It is not a UDS concept.
#
# DEFAULT_SIMULATED_VIN is a completely separate, project-invented value
# returned only when a UDS client reads DID 0xF190 (the real, standardized
# VIN data identifier). It is a SYNTHETIC value for this POC:
#   - it is NOT a real vehicle VIN
#   - it does not encode any real WMI / manufacturer / model data
#   - it has no relationship to DEFAULT_VEHICLE_ID
# The two identifiers are kept apart on purpose: vehicle_id is how this
# project's own components (simulator, gateway, cloud, dashboard) refer to
# "which simulated vehicle," while VIN is a standard UDS field being
# demonstrated for realism. A real vehicle's VIN and its fleet-management
# ID are similarly two independent identifiers.
DEFAULT_SIMULATED_VIN = "SIMVIN00000000001"  # 17 chars, matching real VIN length

# --- Data Identifiers (DIDs) this project's UDS server supports ---
DID_VIN = 0xF190  # standardized DID
# The following two are project-invented DIDs (outside the standardized
# range) used to expose live Powertrain ECU state over UDS, purely for
# demonstration -- a real OEM would assign its own DIDs for this.
DID_VEHICLE_SPEED_KPH = 0x1001
DID_ENGINE_RPM = 0x1002


class ScaledUint16Codec(DidCodec):
    """
    DidCodec for a UDS DID whose payload is a raw uint16 with a fixed
    scale factor -- the same `physical = raw * scale` convention already
    used for CAN signals in common/can_signal_map.py, reused here instead
    of inventing a second encoding scheme for the same kind of value.
    """

    def __init__(self, scale: float) -> None:
        super().__init__(packstr="<H")
        self._scale = scale

    def encode(self, physical_value: float) -> bytes:
        raw = round(physical_value / self._scale)
        raw = max(0, min(0xFFFF, raw))
        return struct.pack(self.packstr, raw)

    def decode(self, payload: bytes):
        (raw,) = struct.unpack(self.packstr, payload)
        return raw * self._scale


# One codec per supported DID, shared by both the server (to encode
# responses) and the client (to decode them) -- so there is exactly one
# place that knows how each DID's value is packed into bytes.
DID_CODECS = {
    DID_VIN: AsciiCodec(len(DEFAULT_SIMULATED_VIN)),
    DID_VEHICLE_SPEED_KPH: ScaledUint16Codec(scale=0.1),  # matches CAN 0x100's scale
    DID_ENGINE_RPM: ScaledUint16Codec(scale=1.0),          # matches CAN 0x101's scale
}

# --- Diagnostic Trouble Codes (DTCs) this project's UDS server reports ---
# Real-format DTC labels, mapped to a simplified 3-byte hex encoding for
# the UDS payload. See docs/uds-spec.md for why this encoding is NOT
# guaranteed to be byte-accurate against the real SAE J2012 bit layout --
# it is illustrative, not a certified DTC encoder.
STATIC_DTCS = [
    (0x000217, "P0217"),  # engine overtemperature (illustrative)
    (0x000420, "P0420"),  # catalyst efficiency below threshold (illustrative)
]


class DiagnosticEvent(BaseModel):
    """
    One event per UDS request/response *transaction* (not one event for
    the request and a second for the response) -- the unit of "something
    happened diagnostically" that later phases (logging, dashboard) will
    want to consume, mirroring how TelemetryEvent is the unit for
    telemetry. Validates structure only, same philosophy as
    TelemetryEvent: this does not judge whether the diagnostic outcome
    itself was "correct," only that the event is well-formed.
    """

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    vehicle_id: str = DEFAULT_VEHICLE_ID
    source_ecu: ECUSource
    service_id: int
    service_name: str
    request_summary: str
    response_summary: str
    is_positive_response: bool
    negative_response_code: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: str = SCHEMA_VERSION
