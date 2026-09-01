# Edge-to-Cloud Automotive Telemetry POC

A production-style, no-hardware Proof of Concept demonstrating an edge-to-cloud
telemetry pipeline for a simulated small vehicle network.

## What this project is

- Simulates a small in-vehicle network: multiple logical ECUs (powertrain,
  battery/energy, body/status) communicating over a **virtual** CAN bus — no
  physical hardware or real vehicle involved.
- Performs basic UDS (ISO 14229) diagnostic interactions against the
  simulated ECUs.
- Uses an **Edge Gateway** to ingest, validate, normalize, buffer, and
  reliably forward telemetry to the cloud — including graceful handling of
  connectivity loss (local buffering + replay on reconnect).
- Streams telemetry to **AWS IoT Core** over MQTT, with structured JSON
  logging and operational metrics for observability.
- Includes a deterministic anomaly-detection rule engine plus an
  **LLM-based advisory log analyzer** — the LLM only explains/summarizes
  what the deterministic rules already flagged; it never makes the
  anomaly call itself.
- Will later add a lightweight web dashboard that visualizes the real,
  live state of the running system (no fabricated/demo data).

## What this project is NOT

This is a personal portfolio engineering exercise. It is **not** a
certified automotive system and is **not** connected to any real vehicle
or hardware. It should be described as a "production-style" or
"real-world-inspired" edge-to-cloud POC — never as production automotive
software.

## Status

This repository is being built incrementally, phase by phase, with each
phase reviewed before the next begins. See [ARCHITECTURE.md](ARCHITECTURE.md)
for the full architecture, the phase plan, and the reasoning behind every
major technical decision.

**Current status: Phase 2 complete** — in addition to Phase 1's simulated
3-ECU vehicle network, the Powertrain ECU now also hosts a basic UDS
(ISO 14229) diagnostic server, reachable by a UDS client/tester over the
same virtual CAN bus via ISO-TP. A shared `DiagnosticEvent` schema
(`common/`) captures every diagnostic request/response transaction. No
Edge Gateway, cloud, or dashboard code exists yet.

## Requirements

This list grows as each phase introduces a real dependency:

- Python 3.11 (see `requirements.txt` for pinned package versions)
- Docker & Docker Compose (introduced in Phase 3)
- An AWS account (introduced in Phase 5; all resources are provisioned via
  Terraform and designed to be torn down cleanly after use)

## Getting started

```bash
pip install -r requirements.txt   # add --break-system-packages on Debian/Ubuntu system Python

# Run the test suite
pytest

# Run the live simulation for 5 seconds, with a fixed seed for reproducible output
python -m simulation.run_simulation --duration 5 --seed 42

# Same, but log every CAN frame sent (otherwise only start/stop are logged)
python -m simulation.run_simulation --duration 5 --seed 42 --verbose
```

There is no cloud, gateway, or dashboard to run yet — those arrive in
later phases. See [ARCHITECTURE.md](ARCHITECTURE.md) for the full phase
plan, [docs/can-signal-spec.md](docs/can-signal-spec.md) for exactly what
the simulated vehicle transmits, and
[docs/uds-spec.md](docs/uds-spec.md) for the UDS diagnostic services the
Powertrain ECU now supports (`pytest simulation/uds/` runs a full
client/server UDS exchange over the virtual CAN bus).
