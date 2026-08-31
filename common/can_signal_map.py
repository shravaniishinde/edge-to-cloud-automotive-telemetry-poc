"""
The CAN signal registry: the single source of truth for how each of this
vehicle's 11 CAN messages is laid out on the wire, and the encode/decode
functions that translate between raw CAN bytes and decoded engineering
values.

Both the simulator (Phase 1, encoding values to send) and the future Edge
Gateway (Phase 3, decoding frames it receives) import this module rather
than each re-implementing the byte layout -- that's what keeps them
"speaking the same schema" as required.

See docs/can-signal-spec.md for the human-readable version of this same
table, including *why* each data type/scale/offset was chosen.

Data flow this module implements, matching ARCHITECTURE.md:

    Decoded engineering value --[encode_signal]--> Raw CAN frame (send side)
    Raw CAN frame --[decode_signal]--> Decoded engineering value (receive side)
    Raw CAN frame --[decode_to_event]--> TelemetryEvent (full receive pipeline)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Dict, NamedTuple, Tuple

from common.telemetry_schema import ECUSource, SignalName, TelemetryEvent

# Every CAN message in this simulation uses a classic CAN 2.0A frame: an
# 11-bit standard arbitration ID and an 8-byte payload (DLC = 8), with any
# bytes the signal doesn't use left as zero-filled reserved space.
CAN_PAYLOAD_LENGTH = 8

# struct format codes for each data type used below. All multi-byte values
# are little-endian ("<") -- a project convention documented here and in
# can-signal-spec.md, not something borrowed from a real vehicle's actual
# DBC file (real OEMs vary on this).
_STRUCT_FORMATS = {
    "uint8": "B",
    "int16": "<h",
    "uint16": "<H",
    "uint32": "<I",
}


class UnknownCanIdError(ValueError):
    """Raised when a CAN ID has no entry in SIGNAL_REGISTRY."""


class DecodedSignal(NamedTuple):
    """
    The result of decoding one raw CAN frame -- everything that can
    genuinely be recovered from the bytes on the wire. Notably, this does
    NOT include event_id/session_id/vehicle_id/timestamp: raw CAN frames
    don't carry those, so they must be supplied by whatever receives the
    frame (see decode_to_event below).
    """

    source_ecu: ECUSource
    signal_name: SignalName
    can_id: int
    value: float
    unit: str


@dataclass(frozen=True)
class SignalDefinition:
    """One row of the CAN signal map: everything needed to encode/decode
    one signal, plus the documentation fields (unit, valid range,
    frequency) that describe it."""

    can_id: int
    ecu: ECUSource
    signal_name: SignalName
    dtype: str  # key into _STRUCT_FORMATS
    scale: float
    offset: float
    unit: str
    valid_range: Tuple[float, float]
    frequency_hz: float

    @property
    def struct_format(self) -> str:
        return _STRUCT_FORMATS[self.dtype]

    @property
    def byte_length(self) -> int:
        return struct.calcsize(self.struct_format)


# The 11-message signal map. See docs/can-signal-spec.md for the full
# explanation of each choice (why temperature uses an offset, why current
# is signed, why rates differ by ECU, etc.).
SIGNAL_REGISTRY: Dict[int, SignalDefinition] = {
    # --- Powertrain ECU: 10 Hz ---
    0x100: SignalDefinition(0x100, ECUSource.POWERTRAIN, SignalName.VEHICLE_SPEED_KPH,
                             "uint16", scale=0.1, offset=0.0, unit="km/h",
                             valid_range=(0.0, 250.0), frequency_hz=10.0),
    0x101: SignalDefinition(0x101, ECUSource.POWERTRAIN, SignalName.ENGINE_RPM,
                             "uint16", scale=1.0, offset=0.0, unit="rpm",
                             valid_range=(0.0, 8000.0), frequency_hz=10.0),
    0x102: SignalDefinition(0x102, ECUSource.POWERTRAIN, SignalName.THROTTLE_POSITION_PCT,
                             "uint8", scale=1.0, offset=0.0, unit="%",
                             valid_range=(0.0, 100.0), frequency_hz=10.0),
    # --- Battery/Energy ECU: 2 Hz ---
    0x200: SignalDefinition(0x200, ECUSource.BATTERY, SignalName.BATTERY_SOC_PCT,
                             "uint16", scale=0.1, offset=0.0, unit="%",
                             valid_range=(0.0, 100.0), frequency_hz=2.0),
    0x201: SignalDefinition(0x201, ECUSource.BATTERY, SignalName.BATTERY_VOLTAGE_V,
                             "uint16", scale=0.1, offset=0.0, unit="V",
                             valid_range=(0.0, 500.0), frequency_hz=2.0),
    0x202: SignalDefinition(0x202, ECUSource.BATTERY, SignalName.BATTERY_CURRENT_A,
                             "int16", scale=0.1, offset=0.0, unit="A",
                             valid_range=(-500.0, 500.0), frequency_hz=2.0),
    0x203: SignalDefinition(0x203, ECUSource.BATTERY, SignalName.BATTERY_TEMP_C,
                             "uint8", scale=1.0, offset=-40.0, unit="degC",
                             valid_range=(-40.0, 120.0), frequency_hz=2.0),
    # --- Body/Status ECU: 1 Hz ---
    0x300: SignalDefinition(0x300, ECUSource.BODY, SignalName.DOOR_STATUS_BITMASK,
                             "uint8", scale=1.0, offset=0.0, unit="bitmask",
                             valid_range=(0.0, 15.0), frequency_hz=1.0),
    0x301: SignalDefinition(0x301, ECUSource.BODY, SignalName.INDICATOR_STATE,
                             "uint8", scale=1.0, offset=0.0, unit="enum",
                             valid_range=(0.0, 3.0), frequency_hz=1.0),
    0x302: SignalDefinition(0x302, ECUSource.BODY, SignalName.ODOMETER_KM,
                             "uint32", scale=1.0, offset=0.0, unit="km",
                             valid_range=(0.0, 999_999.0), frequency_hz=1.0),
    0x303: SignalDefinition(0x303, ECUSource.BODY, SignalName.AMBIENT_TEMP_C,
                             "uint8", scale=1.0, offset=-40.0, unit="degC",
                             valid_range=(-40.0, 60.0), frequency_hz=1.0),
}


def get_signal_definition(can_id: int) -> SignalDefinition:
    """Look up a CAN ID's definition, or raise a clear error if it's not
    one of ours."""
    try:
        return SIGNAL_REGISTRY[can_id]
    except KeyError as exc:
        raise UnknownCanIdError(f"No signal definition registered for CAN ID 0x{can_id:03X}") from exc


def encode_signal(can_id: int, physical_value: float) -> bytes:
    """
    Encode a decoded engineering value (e.g. 62.3 km/h) into an 8-byte CAN
    payload, per that CAN ID's definition. Unused bytes are zero-filled.
    """
    definition = get_signal_definition(can_id)
    raw = round((physical_value - definition.offset) / definition.scale)
    try:
        packed = struct.pack(definition.struct_format, raw)
    except struct.error as exc:
        raise ValueError(
            f"Cannot encode {physical_value} for CAN ID 0x{can_id:03X} "
            f"({definition.signal_name.value}): raw value {raw} does not fit "
            f"a {definition.dtype}"
        ) from exc

    payload = bytearray(CAN_PAYLOAD_LENGTH)
    payload[: len(packed)] = packed
    return bytes(payload)


def decode_signal(can_id: int, data: bytes) -> DecodedSignal:
    """
    Decode a raw 8-byte CAN payload back into its engineering value. This
    is the "CAN signal decoding" step in the data flow -- it recovers only
    what the wire actually carries (no session/event identifiers, since
    those aren't part of a real CAN frame).
    """
    definition = get_signal_definition(can_id)
    if len(data) < CAN_PAYLOAD_LENGTH:
        raise ValueError(
            f"CAN payload for 0x{can_id:03X} must be {CAN_PAYLOAD_LENGTH} bytes, got {len(data)}"
        )

    raw = struct.unpack(definition.struct_format, data[: definition.byte_length])[0]
    physical_value = raw * definition.scale + definition.offset

    return DecodedSignal(
        source_ecu=definition.ecu,
        signal_name=definition.signal_name,
        can_id=can_id,
        value=physical_value,
        unit=definition.unit,
    )


def decode_to_event(can_id: int, data: bytes, *, session_id: str, vehicle_id: str) -> TelemetryEvent:
    """
    The full receive-side pipeline in one call: raw CAN frame -> CAN signal
    decoding -> decoded engineering value -> TelemetryEvent. This is the
    function the Edge Gateway (Phase 3) will call for every frame it
    ingests; Phase 1's tests call it directly to prove the pipeline works
    correctly even though no gateway exists yet.
    """
    decoded = decode_signal(can_id, data)
    return TelemetryEvent(
        session_id=session_id,
        vehicle_id=vehicle_id,
        source_ecu=decoded.source_ecu,
        can_id=decoded.can_id,
        signal_name=decoded.signal_name,
        value=decoded.value,
        unit=decoded.unit,
    )
