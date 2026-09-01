# UDS Diagnostic Specification (Phase 2)

This is the human-readable mirror of `common/diagnostic_schema.py` and
`simulation/uds/uds_server.py`, the same relationship
`docs/can-signal-spec.md` has to `common/can_signal_map.py`. If this file
and the code ever disagree, the code is correct and this file is stale.

## Concept: UDS vs. telemetry

Phase 1 telemetry is unsolicited/broadcast -- every ECU just publishes its
signals on a timer, and nobody has to ask. UDS (ISO 14229) is
request/response and one-to-one: a diagnostic tester sends a specific
request to a specific ECU and gets back exactly one response. Phase 2 adds
this second traffic pattern onto the same virtual CAN bus, without
touching Phase 1's telemetry traffic at all.

## CAN IDs used

| CAN ID | Direction | Purpose |
|---|---|---|
| `0x7E0` | tester -> ECU | UDS request |
| `0x7E8` | ECU -> tester | UDS response |

This is the standard OBD-II/UDS physical-addressing pair, chosen
deliberately because it's immediately recognisable and is guaranteed
distinct from the 11 telemetry CAN IDs (`0x100`-`0x303`) already in use.

Because a UDS response can exceed a single CAN frame's 8-byte payload
(e.g. the VIN response below), both directions are carried over ISO-TP
(ISO 15765-2, via the `isotp` library's `CanStack`), which transparently
segments/reassembles First Frame / Consecutive Frame / Flow Control
frames on top of the same virtual bus python-can already provides.

## Who hosts the server

Only the Powertrain ECU hosts a UDS server (`PowertrainUDSServer` in
`simulation/uds/uds_server.py`). A real vehicle would have one UDS server
per ECU; simulating a second one on Battery or Body would add no new
teaching value at this phase, so this project simulates exactly one.
`udsoncan` is a **client-only** library -- it has no server
implementation, so the server here is hand-rolled on top of
`udsoncan.Request`/`udsoncan.Response` for byte-level parsing/building.

## Supported services

| SID | Service | Notes |
|---|---|---|
| `0x10` | DiagnosticSessionControl | `defaultSession` and `extendedDiagnosticSession` only |
| `0x22` | ReadDataByIdentifier | 3 DIDs supported, see table below |
| `0x19` | ReadDTCInformation | `reportDTCByStatusMask` subfunction only |

Any other service, or an unsupported subfunction/DID within a supported
service, gets a standard UDS negative response (`0x7F <SID> <NRC>`).

## Session handling

`0x10` DiagnosticSessionControl is supported and the server does track
which session (`defaultSession` / `extendedDiagnosticSession`) is
currently active. **It does not yet enforce anything based on that
state** -- `0x22` and `0x19` respond the same way regardless of which
session is active. A real ECU often gates certain DIDs/services behind an
extended session or security access; this is a deliberate Phase 2
limitation, not an oversight, and may be revisited in a later phase.

## Data Identifiers (DIDs)

| DID | Name | Codec | Source of value |
|---|---|---|---|
| `0xF190` | VIN | `AsciiCodec(17)` | `DEFAULT_SIMULATED_VIN` constant (synthetic, see below) |
| `0x1001` | live vehicle speed | `ScaledUint16Codec(scale=0.1)` | `PowertrainECU.speed_kph` (same live state Phase 1 telemetry publishes) |
| `0x1002` | live engine RPM | `ScaledUint16Codec(scale=1.0)` | `PowertrainECU.rpm` |

`0xF190` is the real, standardized UDS DID for VIN. `0x1001`/`0x1002` are
**project-invented** DIDs, outside the standardized range -- a real OEM
would assign its own manufacturer-specific DIDs for exposing live sensor
state this way.

### `vehicle_id` vs. VIN -- kept deliberately separate

This project already has a `vehicle_id` field (`DEFAULT_VEHICLE_ID` in
`common/telemetry_schema.py`, e.g. `"SIM-VEHICLE-01"`) identifying which
simulated vehicle produced an event -- the same role it plays on every
`TelemetryEvent`. That is **not** a UDS concept and is never returned by a
UDS service.

DID `0xF190` returns a completely separate, project-invented constant:

```python
DEFAULT_SIMULATED_VIN = "SIMVIN00000000001"
```

This value is **synthetic**: it is not a real vehicle VIN, does not
encode any real WMI/manufacturer/model information, and has no derived
relationship to `DEFAULT_VEHICLE_ID` -- reading one tells you nothing
about the other. They are kept apart on purpose, the same way a real
vehicle's VIN and its fleet-management ID are two independent
identifiers that happen to both refer to the same vehicle.

## Diagnostic Trouble Codes (DTCs)

`0x19` subfunction `reportDTCByStatusMask` always returns this static
list, regardless of the status mask the client actually requested:

| DTC | Meaning (illustrative) | 3-byte encoding |
|---|---|---|
| P0217 | Engine overtemperature | `0x000217` |
| P0420 | Catalyst efficiency below threshold | `0x000420` |

Two POC simplifications, both deliberate:

1. **The status mask is accepted but not enforced.** A real server ANDs
   the requested mask against each DTC's actual status bits and only
   returns matches; this server always returns both DTCs no matter what
   mask is sent.
2. **The 3-byte encoding is illustrative, not certified.** Real UDS DTC
   encoding per SAE J2012 involves specific bit-level meaning (e.g. the
   first two bits of the high byte indicate the P/B/C/U prefix category).
   This project maps a real-format label straight to a plausible-looking
   3-byte hex value without reproducing that bit layout exactly.

## Message flow (example: reading the VIN)

```
Tester                                    Powertrain ECU (server)
  |--- 0x7E0: [0x22, 0xF1, 0x90] --------------->|   (ReadDataByIdentifier, DID=0xF190)
  |                                               |   looks up DEFAULT_SIMULATED_VIN
  |<-- 0x7E8: [0x62, 0xF1, 0x90, "SIMVIN...1"] ---|   (positive response, ISO-TP segmented:
  |                                               |    20 bytes > 8-byte single-frame limit)
```

Every request/response pair like this produces exactly one
`DiagnosticEvent` (see `common/diagnostic_schema.py`) -- one event per
*transaction*, not one for the request and a separate one for the
response.

## Where this fits in the data flow

```
Client call (e.g. UDSTester.read_did) --[udsoncan.Client]--> Raw UDS request bytes
Raw UDS request bytes --[isotp.CanStack]--> Segmented CAN frames on 0x7E0
Segmented CAN frames --[isotp.CanStack, server side]--> Reassembled raw request bytes
Reassembled raw request bytes --[PowertrainUDSServer.handle_request]--> Raw response bytes + DiagnosticEvent
Raw response bytes --[isotp.CanStack]--> Segmented CAN frames on 0x7E8
Segmented CAN frames --[isotp.CanStack, client side]--> Reassembled response --[udsoncan.Client]--> decoded value
```

## Library versions (confirmed via smoke test before implementation)

- `udsoncan==1.26.1`
- `can-isotp==2.0.7` (PyPI package name; the importable module is `isotp`)

Two API behaviors confirmed by the smoke test, not assumed from
documentation:

- `isotp.CanStack.start()` spawns its own background threads for CAN I/O
  and ISO-TP timing. Once `.start()` has been called, the older
  poll-driven `.process()` method must **not** be called manually -- the
  stack pumps itself, and code should use blocking `.recv()`/`.send()`
  instead.
- `udsoncan.connections.PythonIsoTpConnection.open()` calls `.start()` on
  the `isotp.CanStack` it wraps internally -- calling `.start()` on that
  stack beforehand raises `RuntimeError: Transport Layer is already
  started`.
