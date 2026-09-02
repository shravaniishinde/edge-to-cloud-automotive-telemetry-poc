# Assumptions & Limitations

This document tracks assumptions and known limitations as the project
grows. It starts small in Phase 0 and gets a new entry whenever a phase
introduces a simplification worth being explicit about.

## Assumptions (established in Phase 0)

- No real vehicle, ECU hardware, or physical CAN bus is used anywhere in
  this project. All "CAN traffic" is generated in software.
- This is a personal portfolio project, developed and run by one person.
  It is not intended to run continuously or serve real users/vehicles.
- AWS resources (introduced from Phase 5 onward) are expected to be
  created for a demo/test session and torn down afterward, not left
  running indefinitely.
- Any AI/LLM-based analysis (introduced in Phase 9) is advisory only. It
  is never the sole basis for a safety- or correctness-relevant decision;
  deterministic rules are always the authority for objective anomaly
  detection.

## Limitations (established in Phase 0)

- This project does not implement or claim compliance with any
  automotive safety standard (e.g. ISO 26262) or any certified UDS/CAN
  stack. It borrows concepts from those domains for educational and
  demonstration purposes only.
- Only a small, deliberately chosen slice of the UDS (ISO 14229) service
  catalog is implemented (see `ARCHITECTURE.md`), not the full
  specification.

## Assumptions and limitations added in Phase 1

- The 3 logical ECUs run as threads inside one Python process, not as
  separate OS processes. This is a direct consequence of how
  `python-can`'s virtual interface shares CAN frames (an in-memory
  registry scoped to one process, confirmed with a smoke test) — a real
  vehicle has genuinely separate ECU hardware.
- `TelemetryEvent` validates structure only (correct types, required
  fields, recognised enum values) — it does **not** reject physically
  implausible values (e.g. a battery state of charge over 100%).
  Plausibility checks are deliberately deferred to the Edge Gateway
  (Phase 3), so later fault-injection work (Phase 4) has real
  "structurally valid but physically wrong" data to test against.
- Each ECU publishes all of its own CAN messages together, on one shared
  interval (10 Hz / 2 Hz / 1 Hz), rather than each signal having its own
  independently-scheduled rate.
- `session_id` is generated once per simulation run and currently owned by
  `simulation/run_simulation.py`, since there is no Edge Gateway yet to
  own session lifecycle. This may move to being gateway-owned starting
  Phase 3.
- `vehicle_id` is a fixed constant (`SIM-VEHICLE-01`) since only one
  vehicle is simulated; the field exists on the schema so the data model
  is already honest that a real deployment would have many vehicles.
- Telemetry values are generated with a seedable random-walk model
  (bounded drift within each signal's valid range), not sampled from any
  real vehicle dataset — realistic-looking, not real.

## Assumptions and limitations added in Phase 2

- Only the Powertrain ECU hosts a UDS server. A real vehicle would have
  one per ECU; this project simulates one to demonstrate the pattern
  without triplicating the same server logic for no added teaching value.
- `udsoncan` provides UDS vocabulary/encoding and a client only -- it has
  no server implementation. `simulation/uds/uds_server.py` is a hand-rolled
  server built on `udsoncan.Request`/`udsoncan.Response` for byte-level
  parsing/building, not a library-provided server.
- Diagnostic sessions (`0x10`) are tracked but not enforced: no service is
  gated behind having entered `extendedDiagnosticSession`. A real ECU
  commonly restricts certain DIDs/services to non-default sessions.
- `0x19 reportDTCByStatusMask` always returns the same static 2-DTC list;
  the client's requested status mask is accepted but not used to filter.
- DTC encoding is illustrative, not certified: the 3-byte hex values are
  not guaranteed to match the real SAE J2012 bit-level layout for the
  corresponding P-codes.
- `vehicle_id` (`DEFAULT_VEHICLE_ID`) and the UDS VIN
  (`DEFAULT_SIMULATED_VIN`, DID `0xF190`) are two separate, unrelated
  constants by design -- see docs/uds-spec.md. The VIN is entirely
  synthetic and not derived from any real vehicle or from `vehicle_id`.
- The two project-invented DIDs (`0x1001` speed, `0x1002` RPM) read live
  state directly from the running `PowertrainECU` instance's read-only
  properties, not by decoding the ECU's own CAN telemetry output -- this
  mirrors how a real ECU's diagnostic layer has direct access to its own
  internal state rather than sniffing its own bus traffic.

## Assumptions and limitations added in Phase 3

- The Edge Gateway runs as a thread in the same OS process as the 3 ECU
  threads (via `run_demo.py`), not as a genuinely separate process --
  the same `python-can` virtual-bus constraint noted in Phase 1. It is
  still a logically separate component (own package, own thread, own
  `session_id`) that only interacts with the ECUs through CAN frames on
  the shared bus, never through direct object references. See
  `docs/edge-gateway-spec.md`.
- `session_id` moved from being simulator-owned (Phase 1) to
  gateway-owned. Raw CAN frames never carried a session concept; the
  gateway now mints one `session_id` per run and attaches it when
  decoding frames. The simulator's own `session_id` (still generated by
  `simulation/run_simulation.py`) remains simulator-side bookkeeping
  that never reaches the wire.
- Validation rejects (drops) out-of-range telemetry rather than
  forwarding it with a warning flag. This means an invalid reading is
  simply absent from what the gateway publishes, not present-but-marked
  -- a design choice made so Phase 4's fault injection has a clean
  pass/fail signal to test against.
- The local Mosquitto broker runs with `allow_anonymous true` and no
  TLS (`docker/mosquitto/mosquitto.conf`) -- acceptable for a
  developer's own machine, not something to expose on a network. Real
  authentication (TLS + X.509 client certs) arrives in Phase 5 when the
  gateway talks to AWS IoT Core instead.
- Publish failures are logged and dropped; there is no retry, backoff,
  or local buffering yet. That is Phase 4's explicit scope.
- No metrics are persisted or exported yet -- `EdgeGateway` only tracks
  per-run counters (`processed_count`, `rejected_count`,
  `publish_failure_count`) as plain attributes for a summary log line.
  Phase 6 adds a real metrics system.
- Phase 2's UDS diagnostic events are not ingested, validated, or
  published by the gateway. UDS remains a standalone client/server demo.
- The automated test suite's real-broker integration test launches its
  own `mosquitto` subprocess (via `apt-get install mosquitto`) rather
  than depending on Docker, since this project's sandbox (and possibly
  some CI runners) cannot rely on a working Docker daemon.
  `docker/docker-compose.yml` remains the setup for manual/demo use on a
  developer's own machine.

Further entries are added as each phase is implemented.
