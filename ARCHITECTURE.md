# Architecture & Phase Plan

Status: Phase 3 complete. Implementation proceeds phase by phase; each
phase is reviewed before the next begins. This document is the single
source of truth for *why* the system is shaped the way it is — update it
whenever a phase changes or adds a decision.

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
[Powertrain ECU] [Battery ECU] [Body ECU]   (3 logical ECUs, each its own
        \             |             /        thread within one process —
         \            |            /         see the correction below)
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

**Correction found during Phase 1 planning:** `python-can`'s virtual
interface shares frames between `Bus` objects only *within the same OS
process* (confirmed with a smoke test) — it's an in-memory registry, not a
real inter-process transport. So the 3 ECUs are 3 separate Python classes
running as threads inside one process, not 3 separate OS processes as an
earlier draft of this diagram implied. This doesn't change any decision
above; it's simply the accurate mechanics of the tool we chose.

**Confirmed during Phase 2 planning (smoke test before implementation):**
`udsoncan` (1.26.1) is a client-side-only library with no server
implementation, so `simulation/uds/uds_server.py` is hand-rolled on top of
its `Request`/`Response` byte-level classes. The ISO-TP library's PyPI
package is named `can-isotp` (2.0.7) even though the importable module is
`isotp`. Once `isotp.CanStack.start()` is called it runs its own
background I/O threads; the older poll-driven `.process()` API must not
be called afterward. See `docs/uds-spec.md` for the full write-up.

**Confirmed during Phase 3 planning (smoke test before implementation):**
`paho-mqtt` 2.1.0 deprecated its old default callback signatures;
`mqtt.Client(...)` must be constructed with
`callback_api_version=CallbackAPIVersion.VERSION2` explicitly. Verified
live against a real local Mosquitto broker (2.0.18, installed via
`apt-get` since this project's sandbox has no usable Docker daemon —
`docker/docker-compose.yml` remains the deployment artifact for a
developer's own machine or CI). See `docs/edge-gateway-spec.md`.

### Component responsibilities

- **Simulated ECUs** — 3 logical ECUs (Powertrain, Battery/Energy,
  Body/Status), each its own thread sharing the virtual bus. A CAN ID
  identifies one *message*, not an ECU — each ECU owns several CAN IDs
  (11 messages total; see `docs/can-signal-spec.md`). Each ECU generates
  realistic, slowly-drifting telemetry values and publishes them as CAN
  frames at that ECU's own rate (10 Hz / 2 Hz / 1 Hz).
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
| Dependency management via plain `requirements.txt` | Kept intentionally simple (no Poetry/pyproject) and starts empty, gaining entries only as each phase introduces a real dependency. Versions are pinned exactly to what was installed and tested against. |
| CI introduced early, grown incrementally | A minimal GitHub Actions workflow appears at Phase 1 and runs whatever test suite exists at the time, rather than being bolted on at the end. |
| `TelemetryEvent` schemas built with Pydantic | Decided at Phase 1: validation (types, required fields, known enum values) and serialization come built-in, which is directly useful since "validate" is a named Edge Gateway responsibility. The trade-off (one more dependency, some implicit behavior) was accepted over hand-written dataclass validation. |
| A CAN ID identifies a message, not an ECU | Corrected during Phase 1 planning: one logical ECU can own several CAN IDs. This vehicle's 3 ECUs own 11 CAN messages between them (3 + 4 + 4) — see `docs/can-signal-spec.md`. `TelemetryEvent.can_id` is a required field, not optional, so every event can always be traced back to the exact message it came from. |
| `TelemetryEvent` validates shape, not physical plausibility | E.g. a `battery_soc_pct` of 500 is rejected by nothing in Phase 1 — it's shaped correctly. Range/plausibility checks are deliberately left to the Edge Gateway (Phase 3), so Phase 4's out-of-range fault injection has real "structurally valid but physically wrong" data to test the gateway's defenses against. |
| `vehicle_id` and the UDS VIN are two separate constants | Decided during Phase 2 approval: `DEFAULT_VEHICLE_ID` (this project's own simulated-vehicle identifier, used on every event) and `DEFAULT_SIMULATED_VIN` (a synthetic value returned only by UDS DID `0xF190`) are unrelated by design. Prevents conflating this project's internal ID with a UDS-standard field, and makes clear the VIN is fabricated for the POC, not a real vehicle VIN. |
| UDS server is hand-rolled, not from a library | `udsoncan` only implements the client side of UDS; there is no server counterpart to depend on. `simulation/uds/uds_server.py` builds directly on `udsoncan.Request`/`Response` for parsing/building UDS byte payloads rather than reimplementing that framing from scratch. |
| UDS session tracking without access-control enforcement | Phase 2 tracks which diagnostic session is active but does not yet gate any service behind it. Enforcing this now would add a rule with no corresponding test scenario; deferred to a later phase if needed. |
| Edge Gateway runs in the same process as the ECUs | `python-can`'s virtual bus is process-local (Phase 1 finding), so `run_demo.py` starts the gateway and the 3 ECU threads together. The gateway remains a logically distinct component (own package/thread/session_id) reachable only via CAN frames on the shared bus, never direct object access — a simulation-environment constraint, not an architectural merge. |
| `session_id` moved from simulator-owned to gateway-owned | Raw CAN frames never carried a session concept — only `can_id` and 8 data bytes exist on the wire. Now that a real listener (the gateway) exists, it mints its own `session_id` per run and attaches it during decode, matching how a real edge component owns correlation context rather than the sensors themselves. |
| Gateway validation rejects, not flags | An out-of-range decoded value (e.g. `battery_soc_pct=500`) is dropped and logged, not forwarded with a warning marker — gives Phase 4's fault injection a clean pass/fail signal, and keeps "what got published" trustworthy by construction. |
| MQTT topic scheme: `vehicle/{vehicle_id}/telemetry/{source_ecu}/{signal_name}` | Standard MQTT/IoT topic-hierarchy practice — lets a subscriber filter by vehicle, ECU, or specific signal without inspecting every payload. Payload is `TelemetryEvent`'s own JSON, no new schema introduced. |
| Real-broker integration tests spawn `mosquitto` via subprocess, not Docker | This sandbox (and possibly some CI runners) can't rely on a working Docker daemon; `apt-get install mosquitto` reliably provides the binary. `docker/docker-compose.yml` remains the setup for manual/demo use on a developer's own machine. |

## 4. Target repository structure

Built incrementally — each item is tagged with the phase that creates it.
Items marked **done** exist in the repo today; everything else is still
just the target destination.

```
edge-to-cloud-automotive-telemetry-poc/
├── README.md, .gitignore, requirements.txt, .env.example, ARCHITECTURE.md   [Phase 0 — done]
├── pytest.ini                                        [Phase 1 — done]
├── docs/
│   ├── assumptions-and-limitations.md   [started Phase 0, appended every phase — done]
│   ├── can-signal-spec.md               [Phase 1 — done]
│   ├── uds-spec.md                      [Phase 2 — done]
│   ├── edge-gateway-spec.md             [Phase 3 — done]
│   └── aws-setup.md                     [Phase 5]
├── common/                              [Phase 1 — shared schema package — done]
│   ├── telemetry_schema.py              [Phase 1 — TelemetryEvent — done]
│   ├── can_signal_map.py                [Phase 1 — 11-message CAN registry + encode/decode — done]
│   ├── diagnostic_schema.py             [Phase 2 — DiagnosticEvent — done]
│   ├── tests/                           [Phase 1, extended Phase 2 — done]
│   └── operational_schema.py            [Phase 4/6 — buffer/metric events]
├── simulation/                          [Phase 1 — done]
│   ├── can_bus.py                       [Phase 1 — virtual bus + send/run helpers — done]
│   ├── ecus/                            [Phase 1 — powertrain/battery/body — done]
│   ├── run_simulation.py                [Phase 1 — live demo entry point — done]
│   ├── tests/                           [Phase 1 — done]
│   └── uds/                             [Phase 2 — uds_server.py, uds_client.py, tests/ — done]
├── edge_gateway/                        [Phase 3 — done]
│   ├── ingestion.py, validation.py, normalization.py, mqtt_publisher.py, logging_config.py, gateway.py [Phase 3 — done]
│   ├── tests/                           [Phase 3 — unit + real-broker integration — done]
│   ├── buffer.py, fault_injection.py    [Phase 4]
│   ├── cloud_publisher.py (TLS/IoT Core)[Phase 5]
│   └── metrics.py                       [Phase 6]
├── infra/                               [Phase 5 — Terraform: IoT Core, IAM, CloudWatch]
├── analyzer/                            [Phase 9: rules_engine.py, llm_analyzer.py, prompts/]
├── dashboard/                           [Phase 10: backend/ (REST+WebSocket), frontend/]
├── scenarios/resilience_demo.py         [Phase 8]
├── run_demo.py                          [Phase 3 — ECUs + gateway together — done]
├── docker/docker-compose.yml            [Phase 3 — local Mosquitto — done; grows each phase]
└── .github/workflows/ci.yml             [Phase 1 — minimal, grows every phase — done]
```

## 5. Phase plan

| Phase | Scope | New files/dirs created |
|---|---|---|
| 0 | Minimal scaffolding — **done** | `README.md`, `.gitignore`, `requirements.txt`, `.env.example`, `ARCHITECTURE.md` |
| 1 | Vehicle simulation + shared schema foundation — **done** | `common/telemetry_schema.py`, `common/can_signal_map.py`, `common/tests/`, `simulation/can_bus.py`, `simulation/ecus/` (3 ECUs), `simulation/run_simulation.py`, `simulation/tests/`, `docs/can-signal-spec.md`, `pytest.ini`, `.github/workflows/ci.yml` |
| 2 | UDS diagnostics — **done** | `simulation/uds/`, `common/diagnostic_schema.py`, `docs/uds-spec.md` |
| 3 | Edge Gateway core — **done** | `edge_gateway/` (ingestion, validation, normalization, mqtt_publisher, logging, gateway), `run_demo.py`, `docker/docker-compose.yml` (local Mosquitto), `docs/edge-gateway-spec.md` |
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
