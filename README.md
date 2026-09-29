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
- Includes a lightweight engineering web dashboard (Phase 10) that
  visualizes the real, live state of the running system (no fabricated
  data).

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

**Current status: Phases 0–10 complete** (Phase 8 was done before
Phase 7); Phase 11 (final docs/polish) is not started. The paragraphs below describe the most recent
phases in the order they were completed.

**Phase 6 (diagnostic analyzer) complete** — a Diagnostic Anomaly Analyzer now
sits on top of Phase 2's UDS diagnostics: deterministic rules detect
repeated negative UDS responses, repeated DTC queries, and repeated
overheating-related (P0217) activity from `DiagnosticEvent`s, and an
optional LLM layer can add a short advisory explanation to a finding the
rules already made (never the other way around — see
[docs/analyzer-spec.md](docs/analyzer-spec.md)). This was moved up from
its originally-planned Phase 9 slot; see ARCHITECTURE.md's "Phase
reordering" note. Nothing about Phase 5's cloud integration or Phase 4's
resilience story changed: a failed publish is still buffered to a
persistent local SQLite queue, the gateway still keeps ingesting and
validating CAN traffic through an outage, and it still reconnects
(exponential backoff, capped at 30s, no background thread) and replays
everything buffered, in order, once the broker — local or AWS — comes
back. Combined with Phase 1's simulated 3-ECU network, Phase 2's UDS
diagnostic server, and Phase 3's ingest/validate/normalize/publish
pipeline, the system now demonstrates a resilient edge-to-cloud pipeline
with advisory diagnostic analysis end to end. No dashboard code exists
yet.

**Phase 8 (testing & CI hardening) complete** — a full-scenario
integration test now drives seeded ECUs → virtual CAN → the gateway's
`run()` loop → a real Mosquitto broker → a real subscriber, CI can no
longer silently skip the broker-backed tests, and
[docs/assumptions-and-limitations.md](docs/assumptions-and-limitations.md)
records the current testing limits.

**Phase 7 (observability) complete** (done after Phase 8) —
`edge_gateway/metrics.py` gives the gateway one in-process metrics object
(processed / rejected / publish failures / buffered / replayed, plus the
buffer's dropped count), logged as a structured `"gateway stopped"`
summary tagged with the run's `session_id`. `session_id` is the run-level
correlation ID and `event_id` the per-event identity, both carried in
gateway logs and every payload — see
[docs/edge-gateway-spec.md](docs/edge-gateway-spec.md)'s "Observability"
section. No external metrics system (Prometheus, OpenTelemetry,
CloudWatch Metrics) is used.

**Phase 9 (resilience demo) complete** — `python -m scenarios.resilience_demo`
runs a self-verifying MQTT outage → SQLite buffering → reconnect → FIFO
replay → recovery scenario against local Mosquitto and prints PASS/FAIL
with real counts (see "Resilience demo" below).

**Phase 10 (Engineering Dashboard) complete** — `python -m dashboard.backend`
serves a read-only engineering dashboard at http://127.0.0.1:8080: live
telemetry, gateway metrics, the resilience lifecycle, vehicles/ECUs,
the event stream, diagnostics and activity, plus a few safe demo
controls (see "Engineering dashboard" below). No new dependencies.

On top of that, this repository has also been made reproducible and
CI/CD-ready: a `Dockerfile` and an extended `docker/docker-compose.yml`
run the whole demo in containers, and `.github/workflows/ci.yml` runs the
full test suite on every push/PR. See "Run with Docker" and "CI/CD"
below, and ARCHITECTURE.md's "Reproducibility, Docker & CI/CD" section
for the full reasoning. (This work is orthogonal to the phase-numbered
feature list above — it hardens everything built so far rather than
adding a new one.)

## Requirements

This list grows as each phase introduces a real dependency:

- Python 3.11 — standardized across local development, Docker, and CI
  (see `requirements.txt` for pinned package versions). If your machine's
  default `python`/`python3` is a different version, use Docker (below)
  or a `python3.11` interpreter directly instead of changing this
  project's target version.
- A local MQTT broker: either `docker compose -f docker/docker-compose.yml up`
  (Docker & Docker Compose), or install `mosquitto` directly
  (`apt-get install mosquitto` on Debian/Ubuntu) and run it with
  `docker/mosquitto/mosquitto.conf`
- An AWS account, only if you want to run against AWS IoT Core instead of
  local Mosquitto (Phase 5; all resources are provisioned via Terraform
  and designed to be torn down cleanly after use — see
  [docs/aws-setup.md](docs/aws-setup.md))

## Run locally

```bash
pip install -r requirements.txt   # add --break-system-packages on Debian/Ubuntu system Python

# Run the test suite (broker-backed integration tests skip if no broker is running)
pytest

# Include the real-broker integration tests, incl. the full ECU -> CAN ->
# gateway -> MQTT scenario test: start only the broker, then run pytest
docker compose -f docker/docker-compose.yml up -d mosquitto
pytest -v -rs

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

### Resilience demo (Phase 9)

A scripted, self-checking version of that outage story — no second
terminal, no timing guesswork, no AWS or Anthropic configuration:

```bash
docker compose -f docker/docker-compose.yml up -d mosquitto
python -m scenarios.resilience_demo
```

It runs the real ECUs, Edge Gateway, SQLite buffer and MQTT publisher in
one process (like `run_demo.py`), injects an MQTT connection outage with
`edge_gateway.fault_injection.simulated_connection_outage()`, and walks
through six stages: normal telemetry → outage injected → events buffered
in SQLite (reconnect attempts failing with exponential backoff) → outage
cleared and the gateway's own backoff-gated reconnect succeeds → the
buffer is replayed in FIFO order before any new live event → live
telemetry resumes. Every stage waits on an observed condition with a
bounded timeout. A real MQTT subscriber checks that every buffered
`event_id` arrives, in order, with its original identity; the run ends
with a PASS/FAIL summary (session_id, published/buffered/replayed counts,
final buffer depth, FIFO and recovery checks, the metrics snapshot) and
one structured JSON summary line. The exit code is 0 on PASS, 1 on FAIL.

Options: `--mqtt-host`, `--mqtt-port`, `--vehicle-id`, `--buffer-path`
(default: a temporary file, removed afterwards), `--outage-events`
(default 80), and `--show-logs` to also stream the gateway's JSON logs.
Delivery is at-least-once, not exactly-once — the summary reports any
duplicates rather than hiding them (none are expected in this controlled
run). See [docs/assumptions-and-limitations.md](docs/assumptions-and-limitations.md)
for what the simulated outage does and doesn't exercise.

## Engineering dashboard (Phase 10)

A read/visualization layer over the running system. It never publishes
telemetry, buffers, replays, or changes the gateway; see ARCHITECTURE.md
section 12 for the data flow.

```bash
docker compose -f docker/docker-compose.yml up -d mosquitto
python -m dashboard.backend          # then open http://127.0.0.1:8080
```

What it shows, all derived from real data:

- **System status:** gateway run state, the gateway `session_id`, the gateway's
  MQTT connection, telemetry flow (flowing / stale / none), last event
  time, vehicles and sessions seen, and the dashboard's own broker
  subscription state.
- **Gateway metrics:** the real `GatewayMetrics` snapshot (processed, rejected,
  publish failures, buffered, replayed, dropped). These are cumulative
  counters, shown separately from the *current* SQLite buffer depth and
  the receive rate.
- **Resilience lifecycle:** NORMAL → OUTAGE → BUFFERING → RECONNECTING →
  REPLAYING → RECOVERED, plus the latest replay batch.
- **Live telemetry:** charts of speed, RPM, SOC and pack current (60 s window,
  units on the axes), and the latest value of all 11 signals.
- **Vehicles / ECUs:** per-vehicle, per-ECU event counts and latest values.
- **Event stream:** the latest 50 events with `event_id`, `session_id`, topic,
  and status (live / replayed / late).
- **Diagnostics:** a UDS session run over the virtual CAN bus through the
  existing client/server, analyzed by the deterministic rules. No LLM
  call is made; findings say so.
- **Activity:** the gateway's own structured log lines (reconnects, buffering,
  replay, "gateway stopped" summary), with repeats coalesced.

**Demo controls** (fixed actions only; each drives existing components):
start/stop a live demo (the `run_demo.py` wiring, hosted in the
dashboard process), inject/clear the existing simulated MQTT outage, run
the scripted Phase 9 resilience check, and run a UDS diagnostic session.

Telemetry from gateways in *other* processes, such as `python run_demo.py` or
the Compose `app` container, appears through MQTT. Their `GatewayMetrics`
live in that other process, so the metrics and resilience panels only
cover runs hosted by the dashboard. `--external-buffer
edge_gateway/data/buffer.db` adds a read-only depth reading of
run_demo.py's buffer.

Options: `--host` (default 127.0.0.1), `--port` (8080), `--mqtt-host`,
`--mqtt-port`, `--external-buffer`, `--show-logs`. In Docker:
`docker compose -f docker/docker-compose.yml --profile dashboard up --build`
(the port is published on 127.0.0.1 only). The dashboard has no
authentication: keep it on localhost.

## Run with Docker

Everything above also runs in containers, with no local Python
installation needed at all:

```bash
docker compose -f docker/docker-compose.yml up --build
```

This builds `docker/Dockerfile` (Python 3.11, matching this project's
standardized runtime — see "Requirements" above) and starts two
containers: `mosquitto` (the same broker as above) and `app`, which runs
`run_demo.py` exactly as it runs locally. `app` waits for `mosquitto` to
actually be accepting connections (a Docker healthcheck) before it
starts, so there's no manual ordering to get right. Watch both
containers' logs with the command above, or `docker compose -f
docker/docker-compose.yml logs -f app` for just the demo. Stop everything
with Ctrl+C, or `docker compose -f docker/docker-compose.yml down`.

The same Phase 4 outage demo works here too — `docker compose -f
docker/docker-compose.yml stop mosquitto` / `start mosquitto` in another
terminal while `up` is running, exactly as described above.

**Why one `app` container, not separate simulator/gateway containers:**
`python-can`'s virtual CAN bus only shares frames *within one OS
process*, so the 3 simulated ECUs and the Edge Gateway run together, as
threads inside this single container — splitting them into two
containers would leave the gateway with no bus traffic to read. See
`docker/Dockerfile`'s own comments and
[docs/edge-gateway-spec.md](docs/edge-gateway-spec.md).

`app` needs no AWS or Anthropic credentials to run — see
`docker/docker-compose.yml`'s comments for how to optionally point it at
AWS IoT Core instead of local Mosquitto, or enable the analyzer's
advisory LLM layer, both purely via environment variables (never baked
into the image).

## CI/CD

Every push and pull request to `main` runs
[`.github/workflows/ci.yml`](.github/workflows/ci.yml) on GitHub-hosted
Ubuntu runners: checkout → Python 3.11 → a fast syntax check → install
`requirements.txt` → install and start a local Mosquitto broker (via
`apt-get`, since GitHub-hosted runners don't guarantee a working Docker
daemon), wait until it's listening → `pytest -v -rs`. It also validates
`docker/docker-compose.yml` with `docker compose config` (no image build).
CI sets `MQTT_BROKER_REQUIRED=1`, so the real-broker integration tests —
including the full-scenario ECU → CAN → gateway → MQTT test — *fail*
rather than silently skip if the broker isn't reachable. A failing test
fails the workflow run, visible directly on the commit/PR.

CI requires **no secrets or paid services**: no AWS credentials, no AWS
IoT Core resources, and no `ANTHROPIC_API_KEY`. Tests that need those
(the AWS IoT smoke test, and anything that would make a real Anthropic
API call) are written to skip safely when they're absent, and the
repo-root `conftest.py` strips `ANTHROPIC_API_KEY` from every test's
environment so no test can make a real API call — CI proves the
simulator, UDS diagnostics, Edge Gateway (including against a real local
Mosquitto broker), buffering/resilience, and the deterministic analyzer
rules all work; it does not prove connectivity to a real AWS account,
which is inherently something only a run with real credentials can show.
See ARCHITECTURE.md's "Reproducibility, Docker & CI/CD" section for
exactly which tests run in CI vs. which require real AWS credentials.

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

## Diagnostic Anomaly Analyzer

The analyzer consumes `DiagnosticEvent`s (the same objects
`simulation/uds/uds_server.py` already produces per UDS transaction) and
is completely independent of the live UDS server or bus — pass it any
list of events, e.g. from `simulation/uds/tests/test_uds_integration.py`'s
`events_received` collection pattern:

```python
from analyzer import DiagnosticAnalyzer

analyzer = DiagnosticAnalyzer()
reports = analyzer.analyze(events_received)  # a List[DiagnosticEvent]
for report in reports:
    print(report.severity.value, report.title)
```

This works with **no configuration at all** — the three deterministic
rules (repeated negative responses, repeated DTC queries, repeated
P0217/overheating activity) need nothing but the events themselves. An
optional advisory explanation can be added afterward, only if
`ANTHROPIC_API_KEY` is set:

```python
from analyzer.llm_explainer import explain_anomaly

for report in reports:
    report.llm_explanation = explain_anomaly(report, events_received)  # None if not configured
```

See [docs/analyzer-spec.md](docs/analyzer-spec.md) for the full rule
descriptions, configurable time windows, severity levels, known false
positive/negative patterns, and exactly what the LLM layer is and isn't
allowed to do.

See "Engineering dashboard" below for the Phase 10 visual view. See
[ARCHITECTURE.md](ARCHITECTURE.md) for the full phase plan,
[docs/can-signal-spec.md](docs/can-signal-spec.md) for exactly what the
simulated vehicle transmits, [docs/uds-spec.md](docs/uds-spec.md) for the
UDS diagnostic services the Powertrain ECU supports,
[docs/edge-gateway-spec.md](docs/edge-gateway-spec.md) for the Edge
Gateway's ingest/validate/normalize/publish pipeline, and
[docs/analyzer-spec.md](docs/analyzer-spec.md) for the Diagnostic Anomaly
Analyzer.
