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

Further entries are added as each phase is implemented.
