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

Further entries are added as each phase is implemented.
