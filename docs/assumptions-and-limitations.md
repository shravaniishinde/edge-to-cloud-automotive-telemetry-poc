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
  Phase 6 adds a real metrics system.
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
  Phase 3's counters. A real metrics system is still Phase 6.

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
