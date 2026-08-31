# CAN Signal Specification

This is the human-readable mirror of `common/can_signal_map.py`, which is
the machine-readable, authoritative version used by the code. If the two
ever disagree, the code is correct and this file is stale — please fix it.

## Concept: a CAN ID identifies a message, not an ECU

One logical ECU can transmit several distinct CAN messages, each with its
own arbitration ID. This vehicle simulation has 3 logical ECUs and 11 CAN
messages between them — the table below documents, for each message, which
ECU owns it and exactly how its 8-byte payload is laid out.

## Frame format

Every message in this simulation is a classic CAN 2.0A frame: an 11-bit
standard arbitration ID and an 8-byte payload (DLC = 8). Each message
carries exactly one signal, occupying the payload starting at byte 0; any
remaining bytes are **reserved and zero-filled**. All multi-byte values are
**little-endian**. Both choices (one signal per frame, little-endian) are
project conventions chosen for readability — real automotive DBC files
vary on both points between OEMs.

Decoded engineering value = `raw_integer * scale + offset`. Encoding
reverses this: `raw_integer = round((physical_value - offset) / scale)`.

## Powertrain ECU — 10 Hz (all 3 messages sent together)

| CAN ID | Signal | Bytes | Type | Endianness | Scale | Offset | Unit | Valid range |
|---|---|---|---|---|---|---|---|---|
| `0x100` | `vehicle_speed_kph` | 0–1 | uint16 | LE | 0.1 | 0 | km/h | 0.0–250.0 |
| `0x101` | `engine_rpm` | 0–1 | uint16 | LE | 1 | 0 | rpm | 0–8000 |
| `0x102` | `throttle_position_pct` | 0 | uint8 | N/A | 1 | 0 | % | 0–100 |

## Battery/Energy ECU — 2 Hz (all 4 messages sent together)

| CAN ID | Signal | Bytes | Type | Endianness | Scale | Offset | Unit | Valid range |
|---|---|---|---|---|---|---|---|---|
| `0x200` | `battery_soc_pct` | 0–1 | uint16 | LE | 0.1 | 0 | % | 0.0–100.0 |
| `0x201` | `battery_voltage_v` | 0–1 | uint16 | LE | 0.1 | 0 | V | 0.0–500.0 |
| `0x202` | `battery_current_a` | 0–1 | int16 (signed) | LE | 0.1 | 0 | A | −500.0–500.0 |
| `0x203` | `battery_temp_c` | 0 | uint8 | N/A | 1 | **−40** | degC | −40–120 |

## Body/Status ECU — 1 Hz (all 4 messages sent together)

| CAN ID | Signal | Bytes | Type | Endianness | Scale | Offset | Unit | Valid range |
|---|---|---|---|---|---|---|---|---|
| `0x300` | `door_status_bitmask` | 0 | uint8 bitmask | N/A | — | — | — | 0–15 (bit0=driver, bit1=passenger, bit2=rear-left, bit3=rear-right) |
| `0x301` | `indicator_state` | 0 | uint8 enum | N/A | — | — | — | 0=off, 1=left, 2=right, 3=hazard |
| `0x302` | `odometer_km` | 0–3 | uint32 | LE | 1 | 0 | km | 0–999999 |
| `0x303` | `ambient_temp_c` | 0 | uint8 | N/A | 1 | **−40** | degC | −40–60 |

## Why offset is used for temperature but not current

`battery_temp_c` and `ambient_temp_c` use a common real-world DBC pattern:
store the value as a single **unsigned** byte with a fixed negative offset
(`physical = raw − 40`). One byte covers the entire realistic automotive
temperature range this way, instead of needing a signed 16-bit field.

`battery_current_a`, in contrast, is a genuinely **signed** 16-bit field
with no offset, because current needs to represent both directions
symmetrically around zero — positive while drawing power, negative during
regenerative braking or charging. These are the two techniques real signal
databases actually use for "a value that needs to go negative," shown here
side by side deliberately.

## Why rates are per-ECU, not per-signal

Each ECU sends all of its own messages together, on one interval. A
per-signal schedule (e.g. battery temperature updating slower than state
of charge) would be more realistic, but adds a second timer per signal for
little demonstration value at this phase. This can be revisited later
without changing the signal map or the schema.

## Where this fits in the data flow

```
Decoded engineering value --[encode_signal]--> Raw CAN frame      (send side, the simulator)
Raw CAN frame --[decode_signal]--> Decoded engineering value      (receive side)
Raw CAN frame --[decode_to_event]--> TelemetryEvent               (full receive pipeline)
```

`common/can_signal_map.py` implements all three of the above. It is
imported by the simulator today, and will be imported by the Edge Gateway
starting Phase 3 — neither component re-implements this table.
