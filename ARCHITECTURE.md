# Architecture & Phase Plan

Status: Phase 6 complete. Implementation proceeds phase by phase; each
phase is reviewed before the next begins. This document is the single
source of truth for *why* the system is shaped the way it is — update it
whenever a phase changes or adds a decision.

**Phase reordering (Phase 6):** the original phase plan (see section 5)
had the AI-assisted analyzer at Phase 9, after Observability, Testing/CI
hardening, and the Resilience demo. It was moved up to Phase 6 by
explicit decision, since it's the next feature that meaningfully
differentiates this portfolio project, and nothing about it depends on
those three phases existing first (it consumes `DiagnosticEvent`s that
have existed since Phase 2). Observability, Testing/CI hardening, and the
Resilience demo shift to Phases 7-9 accordingly; nothing about their
scope changed, only their order.

**Naming note (reproducibility/Docker/CI pass):** a later work session
was itself referred to as "Phase 7," but its actual content — Docker,
Docker Compose, GitHub Actions/CI hardening, and Python-version
standardization across the whole repo — is a cross-cutting
reproducibility pass over everything built in Phases 1-6, not the
feature-numbered Phase 7 ("Observability," `edge_gateway/metrics.py`)
this section's table still lists below. To avoid silently renumbering an
approved phase plan, that work is documented in its own section (9,
below) rather than folded into "Phase 7" in the table. The feature-phase
numbering in section 5 is otherwise unchanged and still applies going
forward — resolve this naming overlap explicitly (e.g. rename the
table's Phase 7, or keep both, whichever you prefer) before it causes
confusion later.

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
| Real-broker integration tests connect to an already-running broker, not spawn one themselves | `edge_gateway/tests/conftest.py`'s fixture just checks `localhost:1883` (overridable via `MQTT_BROKER_HOST`/`MQTT_BROKER_PORT`) is reachable and skips if not, rather than owning a broker's lifecycle. `docker/docker-compose.yml` is the one broker mechanism for local dev, manual demos, and CI alike -- CI provides its own equivalent broker by installing the `mosquitto` package, which starts it as a systemd service, since GitHub-hosted runners don't guarantee a working Docker daemon. |
| Reconnect is paced by the gateway's own loop, not a dedicated thread | `try_reconnect()` is a cheap timestamp check most of the time, gated by exponential backoff (1s, doubling, capped at 30s); `EdgeGateway.run_once()` calls it once per iteration. Keeps retry timing simple and explainable without adding a second thread's worth of lifecycle/shutdown concerns for what a POC needs. |
| Buffer replay stops at the first unconfirmed row instead of skipping ahead | Preserves strict FIFO ordering -- correct for time-series signals -- at the cost of one slow/failing event blocking everything behind it until it either confirms or the connection drops again. Accepted: correctness of order matters more here than replay throughput. |
| Delivery guarantee is at-least-once, not exactly-once | A buffered row is deleted only after a real broker PUBACK (verified from paho-mqtt's own source, not assumed); the accepted gap is a possible duplicate replay if the process crashes between that acknowledgement and the delete. Closing that gap fully needs a distributed transaction this POC doesn't need to demonstrate. |
| Fault injection is 4 named scenarios, not a chaos framework | Deterministic and interview-explainable: force N publish failures, a malformed frame, an out-of-range frame, plus a real `docker compose stop` for the actual outage demo. A randomized chaos-monkey approach would add flakiness and complexity with no corresponding teaching value at this scale. |
| AWS IoT Core support extends `MqttPublisher` with optional TLS args, not a second publisher class | AWS IoT Core is plain MQTT over TLS -- the exact protocol `MqttPublisher` already speaks. Adding `tls_ca_certs`/`tls_certfile`/`tls_keyfile` as optional constructor args (all-or-nothing, or a `ValueError`) keeps exactly one publish/reconnect/backoff implementation, unchanged from Phase 4, rather than duplicating that logic in a `CloudPublisher` subclass. No new AWS SDK dependency is needed as a result. |
| Local-vs-AWS selection lives in `edge_gateway/cloud_publisher.py`, not in `gateway.py` or CLI flags | `EdgeGateway` still just receives an already-constructed `MqttPublisher` -- it has no idea, and doesn't need to know, whether that publisher is talking to Mosquitto or AWS IoT Core. Reading environment variables and deciding which to build is a separate, independently testable concern. |
| Partial AWS configuration fails fast, never falls back to local Mosquitto | Deliberate: a half-configured AWS setup silently talking to a developer's local broker instead would be a confusing, hard-to-notice failure mode. `cloud_publisher.py` raises `AwsIotConfigError` (uncaught) the moment some but not all `AWS_IOT_*` variables are set, or a configured certificate file doesn't exist. |
| Kinesis deferred again in Phase 5 | Reconfirms the Phase 0-era "Kinesis excluded" decision above: CloudWatch Logs alone fully proves the telemetry-reached-the-cloud story this phase needs. If ever justified, it's a second IoT Rule action pointed at a Kinesis stream -- an AWS-side routing change, not a Edge Gateway code change, which is exactly why an IoT Rule (not the gateway) owns fan-out. |
| AWS IoT device certificate generated by Terraform (`active = true`, no CSR), not device-generated | Simplest path for a single-developer POC -- one `terraform apply` produces a working key pair and certificate. A production fleet would instead have each device generate its own key locally and submit only a CSR, so the private key never exists in Terraform state; documented as a known, intentional simplification (see `docs/assumptions-and-limitations.md` and `docs/aws-setup.md`). |
| AI-assisted analyzer moved from Phase 9 to Phase 6 | Explicit reordering decision -- see the note under "Status" above. The analyzer only needs `DiagnosticEvent`s (Phase 2) and has no dependency on Observability, Testing/CI hardening, or the Resilience demo, so there was no architectural reason to wait for them. |
| Analyzer: "RULES DETECT, LLM EXPLAINS" as a structural boundary, not just a convention | `analyzer/analyzer.py` (the orchestrator) never imports `analyzer/llm_explainer.py` at all -- attaching an LLM explanation is a separate, caller-initiated step taken only after `DiagnosticAnalyzer.analyze()` has already returned its findings. This makes it structurally impossible for the LLM layer to create, delete, or alter a rule-decided finding, rather than relying on discipline to keep the boundary intact. |
| `DiagnosticAnalyzer` is stateless and keeps no clock | `analyze(events)` is a pure function of its input list: no memory of previous calls, and `AnomalyReport.detected_at` is drawn from the triggering events' own timestamps, never `datetime.now()`. Calling `analyze()` twice on the same events returns the same findings both times -- see `docs/analyzer-spec.md` and `analyzer/tests/test_analyzer.py`. |
| Anomaly detection uses single-pass time-window clustering, not per-event lookback | A naive "does this event's trailing window contain >= threshold matches" check emits one (duplicate, overlapping) report per event once a burst starts. `analyzer/rules._cluster_by_window` instead finds each maximal qualifying run once and resumes scanning immediately after it, so one burst of activity is exactly one `AnomalyReport`. |
| No persistence layer for diagnostic events or anomaly reports in Phase 6 | Explicitly out of scope per this phase's plan ("do not introduce databases, queues, or microservices"). `DiagnosticAnalyzer.analyze()` takes whatever event list a caller already has in memory (the same `on_event` collection pattern `simulation/uds/tests/test_uds_integration.py` already uses) -- a dashboard backend (Phase 10) is the natural place to eventually hold that state, not the analyzer itself. |
| `repeated_p0217_activity` capped at `warning`, not `critical` | Avoids overstating what a POC rule reading a static, illustrative DTC list actually knows -- see docs/analyzer-spec.md's "Avoiding exaggerated claims" section. Severity reflects the analyzer's own confidence, not the seriousness a real overtemperature condition would warrant. |
| LLM explanation layer uses the `anthropic` SDK directly, gated by `ANTHROPIC_API_KEY` | Consistent with this project only ever using official/lightweight SDKs for a single, well-defined job (paho-mqtt for MQTT, Terraform for AWS provisioning). No key set -- the default, including in CI -- means `explain_anomaly()` returns `None` immediately without importing `anthropic` or attempting a network call; the deterministic rules and all of `DiagnosticAnalyzer` are entirely unaffected either way. |
| Runtime standardized on Python 3.11 everywhere, not silently moved to a newer local interpreter | `.github/workflows/ci.yml` and `docker/Dockerfile` both pin `3.11` explicitly; README.md's "Requirements" section says so too. A developer whose own machine reports a different Python version (e.g. 3.14) is expected to use Docker or a `python3.11` interpreter directly, rather than the project quietly re-targeting whatever is locally installed -- reproducibility matters more here than convenience for one contributor's machine. |
| One `app` container runs `run_demo.py` unmodified; no separate simulator/gateway containers | `python-can`'s virtual bus is process-local (the same Phase 1 finding behind "Edge Gateway runs in the same process as the ECUs," above) -- it doesn't survive a process boundary, let alone a container boundary. `docker/Dockerfile`'s `CMD` is the existing entry point, not a new one invented for Docker. |
| `docker-compose.yml`'s `app` service depends on Mosquitto's healthcheck, not just container start | `MqttPublisher.connect()` is a synchronous call that raises if the broker isn't yet accepting connections -- Compose's default `depends_on` (container *started*, not *ready*) would make `app` crash on a cold `docker compose up` the moment Mosquitto's own startup is slower than usual. A `mosquitto_pub`-based healthcheck plus `condition: service_healthy` closes that race without touching application code. |
| AWS IoT Core is never modeled as a Compose service | It's a real external AWS resource (see section 7), not a local process this project could stand up in a container -- adding a fake "aws-iot" container would misrepresent the architecture. `app`'s `AWS_IOT_*` environment variables are all unset (blank) by default in `docker-compose.yml`, which keeps it talking to the real `mosquitto` service exactly like every other local run. |
| CI keeps installing Mosquitto via `apt-get`, not via Docker-in-Docker | Reconfirmed, not redesigned, during the reproducibility pass: GitHub-hosted runners don't guarantee a working Docker daemon, but `apt-get install mosquitto` reliably starts it as a systemd service. This is the same broker mechanism `docker/docker-compose.yml` provides for local dev -- just started a different way -- so CI still runs the real `mosquitto_broker`-fixture tests, not just the ones that skip without a broker. Confirmed by running the suite with and without a broker present: 154 passed/7 skipped without one, 160 passed/1 skipped (the AWS smoke test) with one. |
| `python -m compileall` added as a CI step before dependency installation | A plain syntax error fails in seconds, before the slower `apt-get`/`pip install` steps run -- "lightweight syntax/import check" from the reproducibility requirements, kept genuinely lightweight (no extra dependency, no import of third-party packages) rather than a second, redundant test runner. |
| No Dockerfile `HEALTHCHECK`, no dashboard/API port exposed | `app` is an MQTT *client*, not a server -- it has no port for anything to connect to, and nothing external polls its health (Compose's `restart: on-failure` is the safety net if it crashes). Keeps the image to exactly what running `run_demo.py` needs, per this pass's "avoid unnecessary complexity" constraint. |

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
│   ├── aws-setup.md                     [Phase 5 — done]
│   └── analyzer-spec.md                 [Phase 6 — done]
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
│   ├── ingestion.py, validation.py, normalization.py, gateway.py [Phase 3 — done, untouched since]
│   ├── mqtt_publisher.py                [Phase 3 — done; extended Phase 4 (reconnect/backoff) and Phase 5 (optional TLS)]
│   ├── tests/                           [Phase 3 — unit + real-broker integration — done, extended Phase 4 and 5]
│   ├── buffer.py, fault_injection.py    [Phase 4 — done]
│   ├── cloud_publisher.py (local-vs-AWS selection, TLS config) [Phase 5 — done]
│   └── metrics.py                       [Phase 7]
├── infra/                               [Phase 5 — done — Terraform: IoT Thing/certificate/policy/rule, CloudWatch log group]
├── analyzer/                            [Phase 6 — done]
│   ├── models.py                        [Phase 6 — AnomalyReport, Severity — done]
│   ├── rules.py                         [Phase 6 — 3 deterministic rules + window clustering — done]
│   ├── analyzer.py                      [Phase 6 — DiagnosticAnalyzer orchestrator, AnalyzerConfig — done]
│   ├── llm_explainer.py                 [Phase 6 — optional advisory explanation layer — done]
│   └── tests/                           [Phase 6 — done]
├── dashboard/                           [Phase 10: backend/ (REST+WebSocket), frontend/]
├── scenarios/resilience_demo.py         [Phase 9]
├── run_demo.py                          [Phase 3 — ECUs + gateway together — done]
├── docker/
│   ├── docker-compose.yml               [Phase 3 — local Mosquitto — done; extended in the reproducibility/Docker/CI pass to add the `app` service — done]
│   └── Dockerfile                       [reproducibility/Docker/CI pass — Python 3.11, runs run_demo.py — done]
├── .dockerignore                        [reproducibility/Docker/CI pass — done]
└── .github/workflows/ci.yml             [Phase 1 — minimal, grows every phase — extended in the reproducibility/Docker/CI pass (syntax-check step) — done]
```

## 5. Phase plan

| Phase | Scope | New files/dirs created |
|---|---|---|
| 0 | Minimal scaffolding — **done** | `README.md`, `.gitignore`, `requirements.txt`, `.env.example`, `ARCHITECTURE.md` |
| 1 | Vehicle simulation + shared schema foundation — **done** | `common/telemetry_schema.py`, `common/can_signal_map.py`, `common/tests/`, `simulation/can_bus.py`, `simulation/ecus/` (3 ECUs), `simulation/run_simulation.py`, `simulation/tests/`, `docs/can-signal-spec.md`, `pytest.ini`, `.github/workflows/ci.yml` |
| 2 | UDS diagnostics — **done** | `simulation/uds/`, `common/diagnostic_schema.py`, `docs/uds-spec.md` |
| 3 | Edge Gateway core — **done** | `edge_gateway/` (ingestion, validation, normalization, mqtt_publisher, logging, gateway), `run_demo.py`, `docker/docker-compose.yml` (local Mosquitto), `docs/edge-gateway-spec.md` |
| 4 | Buffering, retry, fault injection — **done** | `edge_gateway/buffer.py`, `edge_gateway/fault_injection.py` (a separate `common/operational_schema.py` turned out not to be needed -- the buffer's rows are plain SQLite columns, not a new Pydantic model) |
| 5 | AWS integration — **done** | `infra/` (Terraform), `edge_gateway/cloud_publisher.py`, `docs/aws-setup.md` (Kinesis deliberately deferred — see decision table above) |
| 6 | AI-assisted diagnostic analyzer — **done** (moved up from Phase 9; see "Phase reordering" note above) | `analyzer/` (models, deterministic rules, orchestrator, optional LLM explainer), `docs/analyzer-spec.md` |
| 7 | Observability | `edge_gateway/metrics.py`, correlation IDs finalized end-to-end |
| 8 | Testing & CI hardening | full pytest suite, one full-scenario integration test, expanded CI, finalized `assumptions-and-limitations.md` |
| 9 | Resilience demo | `scenarios/resilience_demo.py` |
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

## 9. Reproducibility, Docker & CI/CD

A cross-cutting pass (see the "Naming note" under Status, above) made the
repository, as it stood after Phase 6, reproducible on any machine and
enforced by CI on every push/PR — without changing any application code,
architecture, or the phase-numbered feature list.

**Python version.** Standardized on **3.11** everywhere a version is
specified: `.github/workflows/ci.yml` (`actions/setup-python@v5`),
`docker/Dockerfile` (`FROM python:3.11-slim`), and README.md's
"Requirements" section. Nothing was changed to match a *local*
interpreter version (this project's own sandbox reports Python 3.14 in
one environment, 3.10 in another) — those local differences are exactly
why this standardization matters, and application code was not touched
just to satisfy one machine's installed version.

**Docker's role.** `docker/Dockerfile` builds a single image that runs
the existing `run_demo.py` entry point unmodified (no invented
entrypoint) — the 3 simulated ECUs and the Edge Gateway, together, as
threads in one process/container, because `python-can`'s virtual CAN bus
only shares frames within one OS process (see section 3's decision table
and `docker/Dockerfile`'s own comments). The image runs as a non-root
user, takes all configuration from environment variables (AWS IoT
Core/Anthropic settings; see `.env.example`), contains no secrets, and
exposes no ports (it is an MQTT client, not a server). `docker compose -f
docker/docker-compose.yml up --build` builds and runs it alongside
Mosquitto — see README.md's "Run with Docker" section.

**GitHub Actions' role.** `.github/workflows/ci.yml` runs on every push
and pull request to `main`, on GitHub-hosted Ubuntu: checkout → Python
3.11 → a fast `python -m compileall` syntax check → `pip install -r
requirements.txt` → install and start Mosquitto via `apt-get` (a real,
running broker, not a mock — see section 3) → `pytest -v`. It does not
build the Docker image (kept out of scope deliberately — pytest is the
thing that needs to run on every change; a slower Docker build adds CI
time without adding test coverage the pytest run doesn't already provide,
and can be added later if a genuine need arises).

**Local Mosquitto vs. AWS IoT Core, restated for this pass.** Both remain
exactly as Phase 5 left them: local Mosquitto (via `docker/docker-
compose.yml`, `apt-get`, or now also the `app`+`mosquitto` Compose pair)
is the default and everything CI/Docker exercises; AWS IoT Core is a real
external AWS service that this pass explicitly does *not* model as a
container or mock — `docker-compose.yml`'s `app` service simply leaves
every `AWS_IOT_*` variable unset by default, the same "no AWS
configuration -> local Mosquitto" behavior `cloud_publisher.py` has had
since Phase 5.

**What CI actually proves vs. what needs real AWS credentials.** With
Mosquitto installed, CI runs the full suite against a real local broker:
159 unit/integration tests covering the simulator, UDS diagnostics, Edge
Gateway ingest/validate/normalize/publish, buffering/backoff/replay
resilience, fault injection, and the deterministic analyzer rules, plus
`analyzer/tests/test_llm_explainer.py`'s tests (all of which inject a
fake Anthropic client — none make a real API call). Exactly one test
(`edge_gateway/tests/test_aws_iot_integration.py`'s AWS smoke test) is
`skipif`-guarded on real `AWS_IOT_*` environment variables and therefore
always skips in CI, by design — proving actual connectivity to AWS IoT
Core requires real provisioned resources and real credentials, which is
inherently something only a run against a live AWS account can
demonstrate, not something CI should fake.

**Security, reaffirmed, not newly introduced:** no AWS credentials or
Anthropic API key anywhere in `docker/Dockerfile`, `docker/docker-
compose.yml`, or `.github/workflows/ci.yml` — both are environment-variable
based and unset by default; `.gitignore` already covered `.env`,
`*.pem`/`*.crt`/`*.key`, and `infra/certs/` before this pass, and
`.dockerignore` (new) keeps the same categories out of the Docker build
context too.
