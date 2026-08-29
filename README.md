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

**Current status: Phase 0 complete** — repository scaffolding only. No
simulation, gateway, or cloud code exists yet.

## Requirements

This list grows as each phase introduces a real dependency:

- Python 3.x
- Docker & Docker Compose (introduced in Phase 3)
- An AWS account (introduced in Phase 5; all resources are provisioned via
  Terraform and designed to be torn down cleanly after use)

## Getting started

There is nothing runnable yet. Setup instructions will be added here as
each phase introduces something to run.
