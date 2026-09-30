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
- Any AI/LLM-based analysis (originally planned for Phase 9; actually
  delivered in Phase 6 -- see `ARCHITECTURE.md`) is advisory only. It
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
  A real metrics system was planned for "Phase 6" when this was written;
  after the Phase 6 reordering it became Phase 7 (`edge_gateway/metrics.py`),
  now implemented -- see the Phase 7 section below.
- Phase 2's UDS diagnostic events are not ingested, validated, or
  published by the gateway. UDS remains a standalone client/server demo.
- The automated test suite's real-broker integration tests connect to an
  already-running broker (`localhost:1883` by default, overridable via
  `MQTT_BROKER_HOST`/`MQTT_BROKER_PORT`) rather than spawning one
  themselves. `docker/docker-compose.yml` provides that broker for local
  development and manual demo use; CI provides an equivalent broker by
  installing the `mosquitto` package directly (which starts it as a
  systemd service), since GitHub-hosted runners don't guarantee a
  working Docker daemon.

## Assumptions and limitations added in Phase 4

- Delivery is **at least once, not exactly once**. A row is only removed
  from the buffer after `MqttPublisher.publish()` reports the broker
  actually acknowledged it (a real QoS-1 PUBACK, confirmed by reading
  paho-mqtt 2.1.0's own source -- see docs/edge-gateway-spec.md). If the
  process crashes in the narrow window between that acknowledgement and
  the buffer row being deleted, the same event could be replayed again
  on the next run. This gap is accepted, not solved -- closing it
  completely would need a transactional publish+delete across two
  separate systems (the broker and SQLite), which is real complexity
  this POC doesn't need to demonstrate the resilience story.
- The buffer is capped at `DEFAULT_MAX_BUFFERED_EVENTS = 5000` rows (a
  constructor parameter, not hardcoded) -- roughly minutes-to-tens-of-
  minutes of outage coverage at this simulation's combined ECU rate, not
  an attempt to survive a real multi-hour outage. Once full, the oldest
  buffered event is dropped to make room for the newest, logged and
  counted (`TelemetryBuffer.dropped_count`) rather than silently lost.
- Reconnect attempts are paced by `EdgeGateway.run_once()`'s own loop,
  not a dedicated background thread. `try_reconnect()` is a cheap
  timestamp check on every iteration and only actually touches the
  network once its exponential backoff window (1s, doubling, capped at
  30s) has elapsed -- so reconnect timing has the same granularity as
  `run_once()`'s own cadence (bounded by `BUS_RECV_TIMEOUT_SECONDS`,
  0.5s), not sub-tick precision.
- `TelemetryBuffer` opens its SQLite connection with
  `check_same_thread=False`, because `run_demo.py` constructs the buffer
  on the main thread while `EdgeGateway.run()` (which does all the
  enqueue/replay work) executes on its own dedicated thread. Only one
  thread ever actively uses the connection at a given moment; this
  disables sqlite3's same-thread-origin check, it does not add real
  concurrent access. Found by an actual crash during this phase's own
  `run_demo.py` verification, not anticipated up front -- see the git
  history for `edge_gateway/buffer.py`.
- Fault injection (`edge_gateway/fault_injection.py`) is four small,
  named, deterministic scenarios, not a randomized chaos framework:
  forcing publish failures for a fixed count (no real network or Docker
  involved), plus builders for a malformed frame and an out-of-range
  frame that reuse `ingestion.py`/`validation.py`'s existing rejection
  paths rather than adding new gateway logic for cases already handled.
  The demo's actual broker-outage scenario uses a real
  `docker compose stop mosquitto` -- Docker Compose is already this
  project's one broker mechanism, so there's no need to fake that one.
- No metrics are persisted or exported yet -- `buffered_count`,
  `replayed_count`, and `TelemetryBuffer.dropped_count` are still plain
  in-memory counters for this run's own summary log line, same as
  Phase 3's counters. (Superseded: Phase 7 moved all of these into
  `edge_gateway/metrics.py` -- see the Phase 7 section below.)

## Assumptions and limitations added in Phase 5

- Kinesis is deliberately **not** provisioned or wired up in this phase.
  CloudWatch Logs is the only IoT Rule destination -- sufficient to prove
  telemetry reached AWS, which is this phase's actual job. If a
  multi-consumer streaming story is ever justified (e.g. the dashboard
  and the analyzer both needing to tail live telemetry independently),
  it's a second IoT Rule action added in `infra/iot_rule.tf`, not an
  Edge Gateway code change -- see `ARCHITECTURE.md`'s decision table.
- AWS configuration is intentionally all-or-nothing. Setting only some of
  the four required `AWS_IOT_*` environment variables raises
  `AwsIotConfigError` and refuses to start, rather than falling back to
  local Mosquitto -- a silent fallback there would be a much more
  confusing failure than a clear startup error. See
  `edge_gateway/cloud_publisher.py`.
- Config validation checks that each certificate file *exists*, not that
  its *contents* are valid PEM data. A syntactically present but garbage
  cert/key file passes `cloud_publisher.py`'s check and only fails later,
  inside paho-mqtt's `tls_set()`/`connect()`, with a `ssl.SSLError`. This
  boundary is accepted rather than duplicating PEM parsing here just to
  produce a slightly earlier error.
- The device certificate is generated by Terraform (`aws_iot_certificate`
  with `active = true`, no CSR) rather than device-generated. This means
  the private key exists, briefly, in Terraform's own process and state
  -- an accepted simplification for a single-developer POC. A production
  fleet would have each device generate its own key locally and submit
  only a Certificate Signing Request, so the private key never leaves
  that device. See `docs/aws-setup.md`'s "Dev vs. production" section.
- `MqttPublisher` gained optional `tls_ca_certs`/`tls_certfile`/
  `tls_keyfile` constructor arguments (all-or-nothing, else `ValueError`)
  rather than a separate cloud-specific publisher class. `connect()`,
  `publish()`, `try_reconnect()`, and all of Phase 4's buffering/backoff
  logic are byte-for-byte unchanged; a caller that passes no TLS
  arguments (every Phase 1-4 test, and local Mosquitto use) is
  unaffected. This preserves "one publish implementation," the same
  principle Phase 4's replay logic already relied on.
- AWS (IAM) credentials used by Terraform to provision resources and the
  X.509 device certificate the gateway uses at runtime are two separate,
  non-overlapping things. The gateway process never sees, stores, or
  needs an AWS access key. See `docs/aws-setup.md`'s opening section for
  the full explanation.
- No AWS SDK (boto3 or otherwise) was added as a dependency. AWS IoT
  Core's device endpoint is plain MQTT over TLS, which the existing
  `paho-mqtt` dependency (Phase 3) already speaks -- provisioning
  (Terraform) and runtime publishing (paho-mqtt) are different concerns
  with different tools, and the gateway itself needs only the latter.
- The AWS smoke test (`test_aws_iot_integration.py`) skips unless real
  AWS IoT configuration is present in the environment -- it never runs in
  CI (no AWS credentials exist there) and never runs locally unless
  you've actually provisioned AWS IoT Core via `infra/`. This mirrors
  Phase 3/4's `mosquitto_broker` fixture skip pattern exactly.

## Assumptions and limitations added in Phase 6

- The Diagnostic Anomaly Analyzer (originally planned for Phase 9; moved
  up -- see `ARCHITECTURE.md`'s "Phase reordering" note) is a set of
  threshold-based, hand-tuned-for-a-POC rules, not a trained or validated
  anomaly detection system. Its thresholds and time windows were chosen
  for interview explainability, not against any real vehicle telemetry
  data (none exists for this project) or a measured false-positive/
  false-negative rate. See `docs/analyzer-spec.md`'s "False positives and
  false negatives" section for specific, expected examples.
- `repeated_dtc_queries` is expected to produce false positives in normal
  use: a technician or scan tool legitimately polling DTCs can easily
  exceed the default threshold. It is rated `info` severity specifically
  because of this, not `warning`.
- `repeated_p0217_activity` will fire on nearly every DTC-query burst in
  this POC, because `simulation/uds/uds_server.py`'s server always
  returns its full static DTC list (including P0217) on every positive
  `ReadDTCInformation` response, regardless of the requested status mask
  (a Phase 2 simplification -- see `docs/uds-spec.md`). This is a property
  of the simulated ECU's fixed DTC list, not evidence that the rule
  itself is well-calibrated against realistic, varying DTC data.
- `DiagnosticAnalyzer` and every rule are stateless and read no clock:
  `analyze()` takes whatever event list it's given and returns the same
  findings no matter how many times, or when, it's called with that same
  list. There is no persistence layer for `DiagnosticEvent`s or
  `AnomalyReport`s in this phase -- both exist only in memory, in
  whatever list a caller is holding (a test, or eventually a dashboard
  backend). This is an intentional, minimal scope for this phase, not an
  oversight -- see the "no databases/queues/microservices for Phase 6"
  constraint this phase was built under.
- The optional LLM explanation layer never runs unless `ANTHROPIC_API_KEY`
  is set (never the default, and never set in CI). When it does run, its
  output is exactly as reliable as the underlying model's output for a
  short summarization task -- it is stored separately
  (`AnomalyReport.llm_explanation`) and is never treated as authoritative
  or used to alter a rule's decision. A network failure, timeout, or
  missing `anthropic` package produces `None`, not an exception, so a
  flaky LLM call can never interrupt or crash analysis.
- None of the analyzer's severity levels (`info`/`warning`/`critical`)
  should be read as a safety determination. `repeated_p0217_activity` is
  deliberately capped at `warning` rather than `critical`, even though
  the underlying DTC concerns engine overtemperature, specifically to
  avoid overstating what a POC rule reading a static, illustrative DTC
  list actually knows -- see `docs/analyzer-spec.md`.

Further entries are added as each phase is implemented.

## Assumptions and limitations added in the reproducibility/Docker/CI pass

- The Edge Gateway's SQLite buffer (`edge_gateway/data/buffer.db`) is
  created inside the `app` container's own filesystem at runtime. It is
  not backed by a named volume by default, so it does not survive
  `docker compose down` / a container recreation — only container
  restarts. This is consistent with the buffer's existing purpose
  (surviving a process restart during an outage demo, not acting as
  durable long-term storage) and was not changed for this pass.
- `docker/Dockerfile` was written and its syntax verified (`docker
  compose config` parses cleanly), but a full `docker build` could not be
  executed inside the sandbox this work was done in: that sandbox's
  outbound network policy blocks the Docker Hub registry
  (`registry-1.docker.io`) needed to pull the `python:3.11-slim` base
  image, returning an explicit `403 Forbidden` at the proxy level. This
  is a sandbox network restriction, not a defect in the Dockerfile or
  Compose file. The full pytest suite (154 passed/7 skipped without a
  broker, 160 passed/1 skipped with a local Mosquitto broker running) was
  run directly in a Python 3.11 virtual environment in the same sandbox
  and is unaffected by this restriction. Building the image and running
  `docker compose up` should be verified on a machine with normal
  internet access (e.g. the developer's own machine) before relying on
  it.
- `.github/workflows/ci.yml` does not build or run the Docker image --
  only `pytest`. GitHub Actions' own hosted runners do have normal
  internet access, so this is a scope decision (kept out to keep CI fast
  and focused on tests), not a limitation carried over from the sandbox
  restriction above.

## Phase 7 observability status (audited at the start of Phase 8)

*Historical: this audit was written before Phase 7 was implemented. Its
"Not implemented" and "Known correlation gaps" items were addressed by
Phase 7 -- see "Assumptions and limitations added in Phase 7" below.*

Phase 8 (testing & CI hardening) was carried out before the original
Phase 7 (observability). An audit of what Phase 7 would build on found:

- **Already in place:** structured JSON-lines logging
  (`edge_gateway/logging_config.py`) where every gateway log record
  carries the gateway-owned `session_id` and a `component` label; a
  per-event `event_id` (a UUID minted when the gateway decodes a frame)
  on the ingest, reject, publish, and buffer log lines; and both IDs
  inside every published MQTT payload (it is the `TelemetryEvent` JSON
  itself), so they also reach CloudWatch via the IoT Rule's
  `SELECT *`. Per-run counters exist on `EdgeGateway`
  (`processed_count`, `rejected_count`, `publish_failure_count`,
  `buffered_count`, `replayed_count`) and `TelemetryBuffer`
  (`dropped_count`).
- **Correlation:** `session_id` *is* this project's correlation ID --
  `ARCHITECTURE.md` uses "session/correlation ID" interchangeably, and
  one gateway run's ingest -> validate -> publish/buffer story can be
  reconstructed by filtering logs on it (Phase 8's full-scenario test
  now asserts exactly that). No separate `correlation_id` field was
  added: nothing in the implemented contract needs one, and adding it
  would only duplicate `session_id`.
- **Not implemented (still Phase 7 scope, deliberately left untouched):**
  `edge_gateway/metrics.py` does not exist. The counters above are plain
  in-memory attributes, exposed only through `run_demo.py`'s final
  "demo stopped" summary log line -- nothing periodically emits, exports,
  or persists them, and they reset every run.
- **Known correlation gaps, left for Phase 7:** the "replayed buffered
  events" log line records only counts, not the `event_id`s replayed
  (the replayed payloads themselves still carry their `event_id`). A
  backlog replayed after a restart is logged under the *new* run's
  `session_id`, while the replayed payloads keep the *original* run's
  `session_id` -- intentional (a replay never rewrites an event), and now
  covered by a test, but it means one session's logs alone don't list
  events it replayed on behalf of an earlier session. The simulator's own
  `session_id` never reaches the CAN wire (unchanged since Phase 3), so
  correlation starts at the gateway, not at the ECU.

## Assumptions and limitations added in Phase 8 (testing & CI hardening)

- **Full-scenario integration test**
  (`edge_gateway/tests/test_full_scenario_integration.py`): 3 seeded
  ECUs on their own `run_ecu` threads -> virtual CAN bus ->
  `EdgeGateway.run()` on its own thread -> real Mosquitto -> a real MQTT
  subscriber, wired exactly like `run_demo.py` and all in one process
  (the virtual-bus constraint). Only each ECU's *first* tick is
  value-checked: seeding makes it exactly predictable, but how many
  later ticks happen before shutdown depends on thread scheduling, so
  later messages are checked for schema/identity/range consistency, not
  specific values. It publishes to the local broker only -- it says
  nothing about AWS IoT Core, which remains covered by the optional,
  credential-gated smoke test alone.
- **Broker-backed tests skip locally, fail in CI.** Without a reachable
  broker, the broker-backed tests (the full-scenario test plus
  `test_gateway_integration.py`) skip, so `pytest` stays usable on a
  machine with no Docker/Mosquitto. CI sets `MQTT_BROKER_REQUIRED=1`,
  which turns that skip into a failure, so a broken Mosquitto install
  can never make CI green by silently skipping the integration tests.
  The only expected skip in CI is the AWS IoT Core smoke test.
- **Local vs. CI broker.** Locally the broker comes from Docker Compose
  (`docker compose -f docker/docker-compose.yml up -d mosquitto`); CI
  uses Ubuntu's apt-installed `mosquitto` service (no Docker-in-Docker).
  Both are plain, anonymous Mosquitto on port 1883; the CI one uses the
  distro's default config rather than `docker/mosquitto/mosquitto.conf`.
- **Test isolation from other broker traffic.** Broker-backed tests
  publish under a per-run random `vehicle_id` and subscribe only to that
  vehicle's topics. Before Phase 8 they used `SIM-VEHICLE-01` and
  subscribed to every vehicle, so running them while the Compose `app`
  container (`run_demo.py`) was also publishing to the same broker made
  4 of 6 fail -- reproduced, then fixed. Tests still assume nothing else
  publishes to *their* random vehicle ID.
- **Timing.** Test subscribers now wait for the broker's SUBACK instead
  of a fixed sleep, and positive assertions poll with a timeout. Tests
  that assert "nothing was published" still, unavoidably, listen for a
  fixed 0.3 s window -- an absence can only be observed for a bounded
  time.
- **No real Anthropic calls, enforced.** A repo-root `conftest.py`
  removes `ANTHROPIC_API_KEY` (and `ANTHROPIC_BASE_URL`/`ANTHROPIC_MODEL`)
  from the environment for every test, so a key in a developer's shell
  can't turn a test into a real, billed API call. `AWS_IOT_*` variables
  are deliberately not stripped, so the optional AWS smoke test still
  runs for a developer who has provisioned AWS and exported them.
- **Not added, on purpose:** no coverage gate, linter, type checker, or
  per-test timeout plugin (no new dependencies). CI instead has a
  15-minute job timeout so a hung thread fails fast, and validates the
  Compose file with `docker compose config` (no image build or pull).
  The Docker image itself is still not built in CI, as before.
- **Docker verified outside the sandbox.** The reproducibility pass's
  note above (a full `docker build` couldn't run in its sandbox) is now
  resolved on a developer machine: during Phase 8,
  `docker compose -f docker/docker-compose.yml up -d --build app` built
  the image and the `app` container published telemetry to the Compose
  Mosquitto broker. No Docker files were changed.

## Assumptions and limitations added in Phase 7 (observability)

Phase 7 was implemented after Phase 8, on top of the audit above.

- **Correlation: finalized, not replaced.** `session_id` (one per
  `EdgeGateway` instance, i.e. per gateway run) is the run-level
  correlation ID; `event_id` (one per decoded frame) is the per-event
  identity. No separate `correlation_id` was introduced -- it would only
  duplicate `session_id`. The audit's replay gap was closed: the
  "replayed buffered events" log line now lists the replayed `event_id`s,
  so events replayed on behalf of an earlier run are traceable from the
  replaying run's logs. Correlation still starts at the gateway: the
  simulator's own session ID never reaches the CAN wire.
- **Metrics are an in-process abstraction, not a monitoring system.**
  `edge_gateway/metrics.py`'s `GatewayMetrics` holds `processed`,
  `rejected`, `publish_failures`, `buffered`, and `replayed` for one
  gateway instance; `MetricsSnapshot` adds `session_id` and `dropped`.
  They are exposed via `EdgeGateway.metrics_snapshot()`, a "gateway
  stopped" log line when `run()` exits, and run_demo.py's "demo stopped"
  summary. Nothing emits them periodically, exports them (no Prometheus,
  OpenTelemetry, Grafana, or CloudWatch Metrics), or persists them; they
  start at zero for every new gateway instance and there is no reset
  method. If the process dies without `run()` exiting, no summary line is
  written -- the per-event log lines remain.
- **`dropped` has one authority.** It is read from
  `TelemetryBuffer.dropped_count` at snapshot time rather than counted
  twice. That count belongs to the buffer *instance*, so it equals "this
  run's drops" only when, as in run_demo.py, each gateway run opens its
  own `TelemetryBuffer`. Drop counts are not persisted, so drops from a
  previous process are not included.
- **`buffered` equals `publish_failures` today.** Every failed live
  publish is buffered; both are kept because they are different facts.
  A replay attempt that stops early is not counted as a publish failure
  (unchanged from Phase 4) -- the rows simply stay buffered.
- **Thread safety.** Counters are only incremented on the gateway's own
  thread today; a lock still guards each increment and snapshot, so a
  snapshot read from another thread (run_demo.py's main thread, tests)
  is always internally consistent.
- **Log field change.** run_demo.py's "demo stopped" line now uses the
  snapshot's field names: `buffer_dropped` became `dropped`, and the line
  gained `session_id`. Its "starting demo" line is unchanged and still
  names the same value `gateway_session_id`.

## Assumptions and limitations added in Phase 9 (resilience demo)

- **The outage is simulated at the publisher boundary, not the network.**
  `simulated_connection_outage()` makes the gateway see a disconnect
  (`is_connected()` false, publishes fail, reconnect attempts fail with
  real exponential backoff), but the MQTT socket itself stays open. So
  the demo proves the *gateway's* outage handling -- detection,
  buffering, backoff, reconnect-triggered FIFO replay, metrics -- not
  paho-mqtt's or Mosquitto's own behaviour when a TCP connection really
  drops. The manual `docker compose stop/start mosquitto` walkthrough in
  README.md remains the way to see a genuine broker outage.
- **`force_publish_failures()` does not trigger replay** (found while
  building this phase, not changed): it leaves the publisher "connected",
  and the gateway only requests a replay after it observes a reconnect or
  at startup with a backlog. Publish failures that happen *without* a
  detected disconnect (e.g. a PUBACK timeout on a still-open connection)
  therefore stay buffered until the next reconnect or restart, and live
  events published meanwhile go out ahead of them. This is existing
  Phase 4 behaviour ("a live publish failure alone does not trigger
  immediate replay"), documented here rather than redesigned.
- **Recovery is backoff-gated.** After the outage is cleared, the gateway
  notices only on its next reconnect attempt (1 s, doubling, capped at
  30 s), and it keeps buffering until then -- which is why the demo
  usually buffers more events than `--outage-events`. The recovery
  timeout (45 s) is deliberately longer than the 30 s backoff cap.
- **Delivery is at-least-once.** The controlled demo normally receives no
  duplicates, but a crash between a broker acknowledgement and the buffer
  row's deletion could replay an event twice; the demo reports a
  `duplicates_received` count rather than asserting exactly-once.
- **FIFO across topics is checked on the gateway side.** MQTT only
  guarantees ordering per topic, so the subscriber-side FIFO check is per
  topic; global order is verified from the gateway's own log sequence
  (the replayed `event_id` order equals the buffered order, and no live
  publish happens between the outage and the end of replay).
- **Local Mosquitto only.** The demo builds `MqttPublisher` directly
  (ignoring `AWS_IOT_*`), needs no AWS/Anthropic configuration, and is
  not part of the Docker image (`docker/Dockerfile` still copies only
  what `run_demo.py` needs). It needs a fresh buffer: by default a
  temporary file that is deleted afterwards; `--buffer-path` must not
  already exist, so a leftover backlog can't skew the counts.
- **Test cost.** The broker-backed demo tests run the real threads with
  smaller event counts and take roughly 5 s each; together they roughly
  double the broker-backed suite's run time (still well under a minute).

## Assumptions and limitations added in Phase 10 (Engineering Dashboard)

- **Metrics only for gateways the dashboard hosts.** `GatewayMetrics`,
  the gateway's MQTT connection state, and its structured log records
  exist only inside the process running that gateway. The dashboard
  shows them for runs it hosts (its "live demo", which uses the
  `run_demo.py` wiring, and the scripted Phase 9 check). Gateways in
  other processes (`python run_demo.py`, the Compose `app` container)
  are visible through their MQTT telemetry and payload `session_id`s
  only. Their metrics are shown as unavailable rather than estimated.
  Buffer depth of such a gateway can be read from its SQLite file
  (`--external-buffer`), read-only.
- **The resilience lifecycle is derived, not reported by the gateway.**
  It comes from the publisher's connection flag, the SQLite row count,
  recent replay log lines, and whether the dashboard itself injected or
  cleared the simulated outage (see ARCHITECTURE.md section 12). For a
  real broker outage the dashboard cannot know when the broker is back,
  so it shows BUFFERING until replay starts, never RECONNECTING.
- **Observed during manual testing: the publisher can report "connected"
  during a real broker outage.** With `docker compose stop mosquitto` on
  Docker Desktop (Windows), the hosted gateway's `is_connected()` stayed
  true while publishes failed and the buffer grew. `try_reconnect()` marks
  the publisher connected once `client.reconnect()` returns, before any
  CONNACK, and Docker Desktop's port proxy accepts the TCP connection
  even with the container stopped. The gateway still buffered everything
  and replayed it all in FIFO order once the broker returned (591 of 591
  and 2,454 of 2,454 in two runs). This is existing Phase 4 behaviour and
  was not changed in Phase 10. The dashboard was adjusted so it doesn't
  mislabel this state as REPLAYING.
- **Observed during manual testing: duplicate deliveries after a real
  broker outage.** In the same real-outage run the dashboard counted
  1,181 duplicate `event_id`s for 591 buffered events. That's roughly
  two extra copies per buffered event, with no process crash. This goes
  beyond the crash-window duplicate described in the Phase 4 entry
  above, though it is still within at-least-once delivery. Likely cause,
  not yet root-caused: paho-mqtt keeps QoS 1 messages it could not
  deliver and re-sends them after reconnecting, while the gateway has
  already counted those publishes as failed, buffered them, and replays
  them too. The simulated outage (Phase 9) does not show this, because
  `publish()` returns before handing anything to paho. The gateway was
  not changed in Phase 10; the dashboard's `duplicates` counter makes
  the effect visible.
- **Subscriber-side view.** "Telemetry flow", receive rate, duplicates,
  and the event stream reflect what the dashboard's own MQTT
  subscription received (QoS 1, clean session). Messages published while
  the dashboard was disconnected from the broker are not seen by it.
  "Late" means an event arrived more than 2 s after its own timestamp;
  "replayed" is only known for hosted runs (from the gateway's replay
  log line). Duplicates are counted, never hidden: every delivery
  appears in the event stream and the counts. The `duplicates` figure
  is a lower bound, because only the last 5,000 `event_id`s are
  remembered.
- **Bounded history, no persistence.** Everything is in memory with fixed
  caps (ARCHITECTURE.md section 12) and is lost when the dashboard stops.
  Chart history is at most 120 points per signal. Vehicles beyond 20 are
  evicted least-recently-seen first.
- **Diagnostics are on demand.** No live diagnostic stream exists in the
  pipeline, so "Run UDS diagnostic session" runs a fixed UDS script over
  the VirtualBus (session control, VIN, speed, RPM, 3 DTC reads, 3
  unsupported-DID reads) and applies the deterministic rules to that
  run's events. It is designed to exercise all three rules, so the
  findings illustrate the rules rather than detect a real fault. The LLM
  explainer is never called.
- **One hosted gateway run at a time.** The live demo and the scripted
  resilience check both use the process-local VirtualBus, and the
  scripted check's verification reads every gateway log record, so the
  dashboard refuses to run them together.
- **Local, unauthenticated.** The dashboard binds to 127.0.0.1 by default
  and has no login. POST actions need a custom header, which blocks
  simple cross-site requests but is not authentication. The optional
  Compose service publishes port 8080 on the host's loopback only. The
  dashboard uses local Mosquitto and never reads AWS or Anthropic
  configuration.
- **No browser-level automated test.** API, SSE, static-file, and data-path
  behaviour are tested with a real server. The page itself was checked
  manually in a browser (live demo, outage/recovery, diagnostics), not by
  an automated UI test.

## Phase 11 (final documentation)

- Phase 11 changed documentation only; no application, test, Terraform,
  Docker, or CI behaviour changed.
- `.env.example` previously used the project's real AWS IoT data endpoint
  as its "example" value. It now uses a placeholder. The old value remains
  in git history. An IoT endpoint is not a credential (authentication is
  the device certificate and private key, which were never committed),
  but it does identify the account's endpoint.
- `AWS_IOT_CLIENT_ID` is now documented as required in practice: the IoT
  policy only allows connecting as the Thing name, while the loader treats
  the client ID as optional (see `docs/aws-setup.md`).
