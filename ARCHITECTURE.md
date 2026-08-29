# Architecture & Phase Plan

Status: plan approved. Implementation proceeds phase by phase; each phase
is reviewed before the next begins. This document is the single source of
truth for *why* the system is shaped the way it is — update it whenever a
phase changes or adds a decision.

## 1. Scope & framing

This is a no-hardware, portfolio-grade Proof of Concept. It is described
accurately as a "production-style engineering POC" or "real-world-inspired
edge-to-cloud POC" — **never** as a certified or production automotive
system. It simulates a small vehicle network end-to-end: telemetry
generation, diagnostics, edge processing, and cloud delivery, with the
same reliability concerns (validation, buffering, retries, observability)
that a real edge-to-cloud pipeline would need, at a scale and cost that
stays reasonable for a personal project.

## 2. System architecture

```
[Powertrain ECU] [Battery ECU] [Body ECU]   (3 simulated processes)
        \             |             /
         \            |            /
          virtual CAN bus (python-can "virtual" interface)
                      |
         UDS diagnostic client <-> one ECU acting as UDS server
                      |
              Edge Gateway
   ingest -> validate -> normalize -> structured log (session/correlation ID)
   -> publish attempt -> [success: MQTT] / [failure: SQLite buffer]
   -> retry/backoff -> replay buffer on reconnect
   -> local metrics (processed, failed, buffered, replayed)
                      |
        MQTT broker: Mosquitto (dev/test) or AWS IoT Core (cloud demo)
                      |
        IoT Rule -> CloudWatch Logs + Metrics
                      |
   (later) Dashboard backend (REST + WebSocket) reads gateway state directly
```

### Component responsibilities

- **Simulated ECUs** — independent processes that generate realistic
  telemetry values (e.g. vehicle speed, RPM, battery state of charge) and
  publish them as CAN frames on the virtual bus, at defined rates.
- **UDS diagnostic layer** — a small client/server pair implementing a
  deliberately limited slice of ISO 14229 (UDS): `DiagnosticSessionControl`,
  `ReadDataByIdentifier`, and `ReadDTCInformation`. This is enough to
  demonstrate the request/response diagnostic protocol without building
  out the entire UDS service catalog.
- **Edge Gateway** — the architectural boundary between the vehicle network
  and the cloud. Responsible for ingesting raw CAN frames, validating and
  normalizing them into the shared telemetry schema, structured logging
  with session/correlation IDs, buffering locally when the cloud is
  unreachable, retrying with backoff, replaying buffered data on recovery,
  and exposing operational metrics.
- **Cloud pipeline** — AWS IoT Core as the MQTT entry point, with an IoT
  Rule routing accepted telemetry to CloudWatch Logs/Metrics.
- **Analyzer** — a deterministic rule engine that performs the actual
  anomaly detection, plus an LLM pass that only explains/summarizes what
  the rules already flagged.
- **Dashboard** (added last) — reads the Edge Gateway's own live state
  (not AWS, not fabricated data) over REST/WebSocket.

## 3. Key technical decisions and rationale

| Decision | Rationale |
|---|---|
| Kinesis excluded from v1 | IoT Core's rule engine already routes to CloudWatch/DynamoDB/S3/Lambda. No genuine high-throughput or multi-consumer fan-out need at this scale. Documented here as an evaluated-and-rejected option rather than silently omitted. |
| OpenTelemetry excluded from v1 | Structured JSON logs plus a correlation/session ID threaded end-to-end already provide the tracing this POC needs, without running a collector/exporter stack. |
| UDS scope limited to 3 services | `DiagnosticSessionControl`, `ReadDataByIdentifier`, `ReadDTCInformation` demonstrate the protocol thoroughly without implementing the full ISO 14229 service catalog, which would be scope creep for a POC. |
| Virtual CAN via `python-can`'s "virtual" backend, not real SocketCAN `vcan0` | SocketCAN requires a Linux kernel module and usually a privileged container — it doesn't work portably on macOS/Windows Docker Desktop or in CI. The `python-can` virtual backend behaves the same at the application level with no OS dependency. |
| Local buffer uses SQLite | Standard library, zero extra service, and — unlike an in-memory list — survives a gateway process restart. An external broker (Redis/RabbitMQ) would be unjustified complexity for a local buffer. |
| MQTT broker is configurable (local Mosquitto vs. AWS IoT Core) | Same Edge Gateway code talks to a local Mosquitto container for development/tests and to AWS IoT Core (TLS + X.509 cert auth) for the real cloud demo, switched by config. This keeps most of the system, and all of CI, free of AWS cost and flakiness. |
| AWS provisioning via Terraform | Chosen over ad hoc boto3/CLI scripts to reinforce real Infrastructure-as-Code practice and produce a stronger portfolio artifact. |
| Cloud persistence limited to IoT Core + CloudWatch (no DynamoDB/S3 yet) | The resilience story is fully provable from Edge Gateway metrics plus CloudWatch logs alone. Added persistence is deferred until there's a genuine need (e.g. the dashboard needing to read telemetry back from the cloud side). |
| Dashboard reads from the Edge Gateway, not from AWS | Keeps the dashboard honest ("no independently-simulated data") and responsive, since the gateway already holds all the state the dashboard needs to show. |
| LLM analysis is batch/on-demand, never real-time per-message | Avoids latency, cost, and CI flakiness. The deterministic rule engine is authoritative for anomaly detection; the LLM's output is always labeled advisory and is mocked in automated tests. |
| Shared telemetry data model (`common/`) | A single canonical schema (analogous to a DBC file's role in a real vehicle network) used by the simulator, Edge Gateway, cloud integration, analyzer, and dashboard, so all components agree on one event shape instead of drifting JSON conventions. Introduced incrementally: `TelemetryEvent` (Phase 1), `DiagnosticEvent` (Phase 2), an operational/metrics event shape (Phase 4/6). |
| Repository scaffolding is incremental | Directories and files are created only when the phase that needs them begins — the structure below is the destination, not something built upfront. |
| Dependency management via plain `requirements.txt` | Kept intentionally simple (no Poetry/pyproject) and starts empty, gaining entries only as each phase introduces a real dependency. |
| CI introduced early, grown incrementally | A minimal GitHub Actions workflow appears at Phase 1 and runs whatever test suite exists at the time, rather than being bolted on at the end. |

**Open decision (to be finalized at Phase 1):** whether `common/` schema
classes are implemented as **Pydantic models** (built-in validation and
JSON serialization — directly useful for the Edge Gateway's validation
responsibility, at the cost of one more dependency) or **plain dataclasses
with hand-written validation** (more verbose, fully transparent, one fewer
dependency). Default recommendation is Pydantic; will be confirmed before
Phase 1 code is written.

## 4. Target repository structure

Built incrementally — each item is tagged with the phase that creates it:

```
edge-to-cloud-automotive-telemetry-poc/
├── README.md, .gitignore, requirements.txt, .env.example, ARCHITECTURE.md   [Phase 0]
├── docs/
│   ├── assumptions-and-limitations.md   [started Phase 0, appended every phase]
│   ├── can-signal-spec.md               [Phase 1]
│   ├── uds-spec.md                      [Phase 2]
│   └── aws-setup.md                     [Phase 5]
├── common/                              [Phase 1 — shared schema package]
│   ├── telemetry_schema.py              [Phase 1 — TelemetryEvent]
│   ├── diagnostic_schema.py             [Phase 2 — DiagnosticEvent]
│   └── operational_schema.py            [Phase 4/6 — buffer/metric events]
├── simulation/                          [Phase 1: ecus/, can_bus.py, tests/]
│   └── uds/                             [Phase 2: uds_server.py, uds_client.py]
├── edge_gateway/                        [Phase 3: ingestion.py, validation.py, normalization.py, logging_config.py]
│   ├── buffer.py, fault_injection.py    [Phase 4]
│   ├── cloud_publisher.py (TLS/IoT Core)[Phase 5]
│   └── metrics.py                       [Phase 6]
├── infra/                               [Phase 5 — Terraform: IoT Core, IAM, CloudWatch]
├── analyzer/                            [Phase 9: rules_engine.py, llm_analyzer.py, prompts/]
├── dashboard/                           [Phase 10: backend/ (REST+WebSocket), frontend/]
├── scenarios/resilience_demo.py         [Phase 8]
├── docker/docker-compose.yml            [Phase 3 — adds mosquitto; grows each phase]
└── .github/workflows/ci.yml             [Phase 1 — minimal, grows every phase]
```

## 5. Phase plan

| Phase | Scope | New files/dirs created |
|---|---|---|
| 0 | Minimal scaffolding | `README.md`, `.gitignore`, `requirements.txt`, `.env.example`, `ARCHITECTURE.md` |
| 1 | Vehicle simulation + shared schema foundation | `common/telemetry_schema.py`, `simulation/` (ecus, can_bus, tests), `docs/can-signal-spec.md`, first `.github/workflows/ci.yml` |
| 2 | UDS diagnostics | `simulation/uds/`, `common/diagnostic_schema.py`, `docs/uds-spec.md` |
| 3 | Edge Gateway core | `edge_gateway/` (ingestion, validation, normalization, logging), `docker/docker-compose.yml` (local Mosquitto) |
| 4 | Buffering, retry, fault injection | `edge_gateway/buffer.py`, `fault_injection.py`, `common/operational_schema.py` if needed |
| 5 | AWS integration | `infra/` (Terraform), `edge_gateway/cloud_publisher.py`, `docs/aws-setup.md` |
| 6 | Observability | `edge_gateway/metrics.py`, correlation IDs finalized end-to-end |
| 7 | Testing & CI hardening | full pytest suite, one full-scenario integration test, expanded CI, finalized `assumptions-and-limitations.md` |
| 8 | Resilience demo | `scenarios/resilience_demo.py` |
| 9 | AI-assisted analyzer | `analyzer/` (rules engine, LLM analyzer, prompts) |
| 10 | Engineering Dashboard | `dashboard/backend/`, `dashboard/frontend/` |
| 11 | Final docs & polish | architecture diagram, interview talking-points doc |

Each phase follows: **PLAN → EXPLAIN → IMPLEMENT → VERIFY → DOCUMENT**, and
implementation does not begin on a phase until it has been explicitly
approved.

## 6. Final end-to-end demonstration (target)

The 3 ECUs and the virtual CAN bus start up. A UDS diagnostic session reads
a simulated fault code from the powertrain ECU. The Edge Gateway ingests,
validates, normalizes, and publishes telemetry to AWS IoT Core over MQTT,
with structured logs carrying a session ID from ECU through gateway to
cloud. Fault injection then cuts cloud connectivity; the gateway keeps
running and buffers incoming telemetry to SQLite instead of crashing, and
its metrics show the buffered count climbing. Connectivity is restored;
the gateway detects it, replays the buffer in order, and metrics show the
replayed count matching what was buffered, with the buffer draining to
zero and no data loss. CloudWatch logs and the gateway's own logs together
tell the whole story from a single correlation ID. Optionally, the
analyzer then runs over that session's logs and returns deterministic
rule findings plus an LLM summary explicitly labeled as advisory.

## 7. AWS services: essential vs. optional

**Essential:** AWS IoT Core (MQTT endpoint, device certificate, IoT
policy — the primary cloud entry point), IAM (least-privilege policy
scoped to the one IoT "thing"), CloudWatch (Logs + basic Metrics via an
IoT Rule action).

**Deferred/rejected, with rationale documented rather than silently
dropped:** Kinesis (no genuine throughput need at POC scale), DynamoDB/S3
(not needed yet — the resilience story is provable from gateway metrics
and CloudWatch logs alone; revisit only if the dashboard needs to read
telemetry back from the cloud side), Lambda (only needed if an IoT Rule
action requires custom transformation, which none of the current rule
actions do).

## 8. Assumptions & limitations

Tracked in detail in `docs/assumptions-and-limitations.md`, started in
Phase 0 and appended as each phase introduces new assumptions. At minimum
this project assumes: no real vehicle hardware or CAN bus is involved;
AWS resources are provisioned and torn down by the developer, not
continuously running; and the LLM analyzer is advisory only and is never
the sole basis for a safety- or correctness-relevant decision.
