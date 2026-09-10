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

**Current status: Phase 5 complete** — the Edge Gateway can now publish
the exact same telemetry to a real AWS IoT Core endpoint over MQTT/TLS,
in addition to local Mosquitto. Nothing about Phase 4's resilience story
changed to make this possible: a failed publish is still buffered to a
persistent local SQLite queue, the gateway still keeps ingesting and
validating CAN traffic through an outage, and it still reconnects
(exponential backoff, capped at 30s, no background thread) and replays
everything buffered, in order, once the broker — local or AWS — comes
back. Combined with Phase 1's simulated 3-ECU network, Phase 2's UDS
diagnostic server, and Phase 3's ingest/validate/normalize/publish
pipeline, the system now demonstrates a resilient edge-to-cloud pipeline
end to end. No dashboard code exists yet.

## Requirements

This list grows as each phase introduces a real dependency:

- Python 3.11 (see `requirements.txt` for pinned package versions)
- A local MQTT broker: either `docker compose -f docker/docker-compose.yml up`
  (Docker & Docker Compose), or install `mosquitto` directly
  (`apt-get install mosquitto` on Debian/Ubuntu) and run it with
  `docker/mosquitto/mosquitto.conf`
- An AWS account, only if you want to run against AWS IoT Core instead of
  local Mosquitto (Phase 5; all resources are provisioned via Terraform
  and designed to be torn down cleanly after use — see
  [docs/aws-setup.md](docs/aws-setup.md))

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

To see the Edge Gateway itself running against a real broker (rather than
just its tests), start a local Mosquitto broker (see above), then:

```bash
python run_demo.py --duration 10
```

This starts all 3 ECUs and the Edge Gateway together (they must share one
Python process — see [docs/edge-gateway-spec.md](docs/edge-gateway-spec.md)
for why) and publishes validated telemetry to
`vehicle/SIM-VEHICLE-01/telemetry/{ecu}/{signal}` topics on the broker.
Subscribe with `mosquitto_sub -t 'vehicle/#'` in another terminal to watch
it live.

To see Phase 4's resilience story — buffering through an outage, then
replaying on recovery — while the demo above is running, in a second
terminal:

```bash
docker compose -f docker/docker-compose.yml stop mosquitto
# watch the demo's logs shift from "published telemetry event" to
# "publish failed -- buffered for replay"

docker compose -f docker/docker-compose.yml start mosquitto
# watch the logs show "MQTT reconnected" followed by a burst of
# "replayed buffered events", and mosquitto_sub receive that same burst
# in original order
```

## Cloud (AWS IoT Core)

Local Mosquitto is still the default — nothing above changes. To publish
the same telemetry to a real AWS IoT Core endpoint instead:

1. Provision AWS IoT Core with Terraform and set four environment
   variables it prints (`AWS_IOT_ENDPOINT`, `AWS_IOT_CA_PATH`,
   `AWS_IOT_CERT_PATH`, `AWS_IOT_KEY_PATH`) — see
   [docs/aws-setup.md](docs/aws-setup.md) for the exact steps.
2. Run `python run_demo.py` exactly as before. With all four variables
   set, it connects to AWS IoT Core over MQTT/TLS instead of local
   Mosquitto; with none set, it's unchanged. Setting only *some* of them
   is treated as a configuration error and the gateway refuses to
   start — it never silently falls back to local Mosquitto.

The same topic scheme and JSON payload are used either way; only the
broker and its TLS/authentication differ. See
[docs/edge-gateway-spec.md](docs/edge-gateway-spec.md)'s "Publishing to
AWS IoT Core" section for how the gateway decides which broker to use,
and [ARCHITECTURE.md](ARCHITECTURE.md) for why Kinesis is deliberately
not part of this phase.

There is no dashboard to run yet — that arrives in a later phase. See
[ARCHITECTURE.md](ARCHITECTURE.md) for the full phase plan,
[docs/can-signal-spec.md](docs/can-signal-spec.md) for exactly what the
simulated vehicle transmits, [docs/uds-spec.md](docs/uds-spec.md) for the
UDS diagnostic services the Powertrain ECU supports, and
[docs/edge-gateway-spec.md](docs/edge-gateway-spec.md) for the Edge
Gateway's ingest/validate/normalize/publish pipeline.
