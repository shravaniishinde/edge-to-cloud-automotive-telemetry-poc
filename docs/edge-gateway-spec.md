# Edge Gateway Specification (Phase 3)

The human-readable version of `edge_gateway/`'s design, the same
relationship `docs/can-signal-spec.md` and `docs/uds-spec.md` have to
their code. If this file and the code disagree, the code is correct and
this file is stale.

## What the Edge Gateway does

It's a separate consumer sitting on the same virtual CAN bus the ECUs
publish to. For every frame that arrives, it runs a 4-step pipeline:

```
Raw CAN frame
  --[ingestion.py]--> TelemetryEvent (or nothing, if not ours to interpret)
  --[validation.py]--> pass/reject (physical plausibility check)
  --[normalization.py]--> (MQTT topic, JSON payload)
  --[mqtt_publisher.py]--> published to the broker (or logged as failed)
```

Each step is a small, independently testable module (`edge_gateway/tests/`
has direct unit tests for each), and `edge_gateway/gateway.py`'s
`EdgeGateway` class wires them into one loop with structured logging at
every step.

## Why the gateway runs in the same process as the ECUs

`python-can`'s virtual bus only shares frames *within one OS process*
(confirmed by smoke test in Phase 1) -- it's an in-memory registry, not a
real inter-process transport. So `run_demo.py` starts the 3 ECU threads
*and* the gateway thread together in one Python process, all sharing one
virtual bus handle setup. This does **not** make the gateway architecturally
part of the simulator: it's still its own package, with its own thread,
its own `session_id`, and it only ever touches the ECUs through CAN
frames on the shared bus -- never through direct object references (the
way, say, Phase 2's UDS server had to read `PowertrainECU.speed_kph`
directly, because UDS is request/response and that value isn't itself a
CAN message). The gateway/ECU process boundary here is a simulation
convenience; the one real inter-process (and inter-machine) boundary in
this whole project is the MQTT connection to the broker, which is exactly
the boundary that matters for the "edge-to-cloud" story.

## Ingestion: filtering, not just decoding

The virtual bus in this project carries more than telemetry -- Phase 2's
UDS request/response frames (`0x7E0`/`0x7E8`) share it too. A real
gateway's CAN interface would apply a hardware or software filter for the
message IDs it actually cares about; `ingestion.py` does the same thing
in software, checking each frame's arbitration ID against
`common/can_signal_map.SIGNAL_REGISTRY` before attempting to decode it.
Anything else (UDS traffic, or any future CAN ID this gateway doesn't
know about) is silently ignored, not an error.

A frame with a *known* CAN ID but the wrong payload length is different:
that's a malformed frame, logged as a warning and dropped, rather than
crashing the ingestion loop over one bad frame.

## `session_id` is now gateway-owned

This is a deliberate change from Phase 1, flagged back then as a
"revisit at Phase 3" item. The reasoning:

- A raw CAN frame carries only an 11-bit arbitration ID and up to 8 data
  bytes -- nothing else. It never carried `session_id`, `vehicle_id`, or
  any other metadata, even in Phase 1.
- Phase 1's ECUs *did* construct `TelemetryEvent` objects with a
  `session_id` (see `simulation/run_simulation.py`), but that was always
  in-memory bookkeeping for the simulator's own use (its tests, mainly)
  -- `simulation/can_bus.send_event()` only ever puts `can_id` and the
  encoded 8 bytes onto the wire. That `session_id` never actually reached
  anything downstream.
- Now that a real downstream listener exists, the gateway is the natural
  place to own the correlation ID: it mints one `session_id` per
  `EdgeGateway` instance (i.e. per ingestion run) and attaches it to
  every `TelemetryEvent` it decodes via `decode_to_event()`. This mirrors
  how a real edge gateway establishes the session/correlation context for
  a monitoring window -- the sensors themselves don't know about
  "sessions," the collection point does.

`vehicle_id` stays a fixed constant on both sides for now (only one
simulated vehicle exists, and the CAN wire doesn't carry vehicle identity
either -- you're implicitly on that vehicle's own network).

## Validation: reject, don't just flag

`validation.py` checks each decoded `TelemetryEvent.value` against the
`valid_range` already defined per-signal in
`common/can_signal_map.SIGNAL_REGISTRY` -- no separate range table, reusing
the same shared registry the simulator's encode/decode logic already
depends on. An out-of-range value (e.g. `battery_soc_pct=500`, which
Pydantic's schema deliberately allows -- see the design note in
`common/telemetry_schema.py`) is **dropped**: logged with a reason, never
forwarded to MQTT. This is what gives Phase 4's fault injection something
real to test the gateway's defenses against.

## Normalization: MQTT topic and payload

Topic scheme: `vehicle/{vehicle_id}/telemetry/{source_ecu}/{signal_name}`
-- one topic per signal, standard MQTT/IoT practice, so a subscriber can
filter by vehicle, by ECU, or by a specific signal using the topic
hierarchy alone. Example: `vehicle/SIM-VEHICLE-01/telemetry/powertrain/vehicle_speed_kph`.

Payload is the `TelemetryEvent`'s own JSON serialization
(`model_dump_json()`) -- deliberately no new schema introduced here, so
there remains exactly one definition of "what a telemetry reading looks
like," per the project's shared-model principle.

## Publishing: paho-mqtt, and what Phase 3 does NOT do

`mqtt_publisher.py` wraps `paho.mqtt.client.Client`, using
`CallbackAPIVersion.VERSION2` -- confirmed via smoke test before
implementation, since paho-mqtt 2.x deprecated the old default callback
signatures (`on_connect`/`on_publish`/etc. gained new parameters under
VERSION2). QoS 1 ("at least once") is used for all publishes.

Explicitly out of scope for Phase 3, on purpose:

- **No retry or backoff.** A publish that doesn't get confirmed within
  its timeout is logged as a failure and dropped. Phase 4 adds retry.
- **No local buffering.** There's no SQLite (or any other) store yet for
  telemetry that couldn't be published. Also Phase 4.
- **No reconnect handling** beyond whatever paho-mqtt's client does by
  default. A broker outage mid-run is not yet gracefully recovered from.
- **No metrics export.** `EdgeGateway` tracks `processed_count`,
  `rejected_count`, and `publish_failure_count` as plain instance
  attributes for this run's own summary log line -- there's no persisted
  or externally-queryable metrics system yet. That's Phase 6.
- **No AWS IoT Core / TLS.** The broker is local, unauthenticated
  Mosquitto (`allow_anonymous true` in `docker/mosquitto/mosquitto.conf`)
  -- fine for a developer's own machine, explicitly not something to
  expose on a real network. Phase 5 adds TLS + X.509 client-cert auth
  when the gateway starts talking to real AWS IoT Core.
- **No UDS integration.** Phase 2's UDS client/server system stays a
  standalone demo; `DiagnosticEvent`s are not (yet) ingested, validated,
  or published by this gateway.

## Testing strategy

Same two-tier pattern used since Phase 2: fast unit tests per module
(`test_ingestion.py`, `test_validation.py`, `test_normalization.py`,
plus a couple of no-broker-needed cases in `test_mqtt_publisher.py`) that
never touch a network, plus one set of real-broker integration tests
(`test_gateway_integration.py`) proving the full pipeline works together
against an actual Mosquitto instance -- publishing a valid frame,
confirming an out-of-range frame is silently dropped, and confirming a
UDS frame sharing the bus is ignored.

`edge_gateway/tests/conftest.py`'s `mosquitto_broker` fixture connects to
an already-running broker at `localhost:1883` by default (overridable via
the `MQTT_BROKER_HOST` / `MQTT_BROKER_PORT` environment variables) rather
than spawning one itself, and skips the tests that need it if nothing is
listening there. `docker/docker-compose.yml` is the normal way to provide
that broker -- for local development, manual demo use
(`docker compose up`, then `python run_demo.py`), and CI alike, so there
is exactly one broker-provisioning story, not two competing ones. CI's
own `mosquitto` package install (`.github/workflows/ci.yml`) starts an
equivalent broker via its systemd service instead of Docker, since
GitHub-hosted runners don't guarantee a working Docker daemon -- same
broker, same config concept, different launch mechanism.

## Resilience (Phase 4)

Phase 3 explicitly left a publish failure as "log it, drop it." Phase 4
gives it a second chance instead, using three small, separately testable
pieces: `edge_gateway/buffer.py` (a persistent queue), `mqtt_publisher.py`
additions (knowing when the connection is actually down, and retrying it
sanely), and `gateway.py` wiring them together.

### Delivery guarantee, made explicit

Before changing anything, Phase 4 first pinned down exactly what
`MqttPublisher.publish()` returning `True` already meant, by reading
paho-mqtt 2.1.0's own source rather than assuming: `True` only happens
once `MQTTMessageInfo.is_published()` is true, which for QoS 1 (this
project's `DEFAULT_QOS`) is only ever set by `Client._handle_pubackcomp()`
-- i.e. a real PUBACK was received from the broker. `wait_for_publish()`
does **not** raise when it times out; it just returns, leaving
`is_published()` false if no PUBACK arrived. `publish()` already checked
`is_published()` afterward rather than treating "no exception" as
success, so this was correct before Phase 4 touched it and needed no
logic change -- only making the guarantee explicit in the docstring,
since Phase 4's buffer-removal rule depends on it being genuinely true.
The upshot: this system is **at least once**, not exactly once -- a
publish that the broker acknowledges is never silently lost, but a crash
in the narrow window between that acknowledgement and this process
deleting the buffered row could cause one re-send on the next run. That
window is accepted, not hidden -- see
`docs/assumptions-and-limitations.md`.

### Buffer: SQLite, persistent, FIFO, bounded

`TelemetryBuffer` (edge_gateway/buffer.py) is one SQLite table,
`pending_telemetry(id, event_id, topic, payload, enqueued_at)`, opened
against a real file (not `:memory:`) so it survives a gateway process
restart. `id` is an `AUTOINCREMENT` primary key, which is what makes
`peek_batch()` (`ORDER BY id ASC`) a genuine, gap-free FIFO read. A
publish failure calls `buffer.enqueue()` instead of just logging; nothing
is ever silently dropped on a failed live publish.

The buffer is capped (`DEFAULT_MAX_BUFFERED_EVENTS = 5000`, roughly
minutes-to-tens-of-minutes of outage coverage at this simulation's
combined ECU rate -- the right scale for a demo POC, not a real
multi-hour outage). When full, `enqueue()` drops the *oldest* row to make
room for the newest, logs a warning, and increments
`TelemetryBuffer.dropped_count` -- the most recent vehicle state is
judged more valuable to eventually deliver than a stale one from minutes
ago.

### Reconnect: no background thread, exponential backoff capped at 30s

`MqttPublisher` now registers paho-mqtt's `on_disconnect` callback, which
is the one thing Phase 3 never had: a way to notice a connection that
dropped *after* `connect()` succeeded (before Phase 4, `_connected` was
only ever set in `connect()`/`disconnect()`). `try_reconnect()` is a
single bounded attempt gated by a `time.monotonic()` deadline: if it's
not yet time to retry, it returns immediately without touching the
network at all, which is what keeps this from becoming a tight
reconnect loop. On failure the backoff doubles (starting at 1s, capped
at 30s); on success it resets. There is deliberately no dedicated
background thread driving this -- `EdgeGateway.run_once()` calls
`try_reconnect()` once per iteration of its own existing loop (checked
every tick regardless of whether a CAN frame arrived, so retries aren't
tied to bus traffic continuing), so the gateway's own natural cadence
paces the retries instead.

### Replay: same publish path, strict order, remove-only-on-confirmation

`EdgeGateway._replay_buffered()` runs the instant `try_reconnect()`
reports success. It reads a batch in `id` order and publishes each row
through the exact same `MqttPublisher.publish()` call live telemetry
uses -- there is one publish implementation, not two. A row is deleted
only after `publish()` returns `True` for it. The loop stops at the
first row that doesn't confirm (connection dropped again mid-replay, or
a genuine publish failure) rather than skipping ahead, so nothing later
in the buffer can ever be sent out of order ahead of something earlier
that's still unconfirmed.

### Fault injection: four named scenarios, not a chaos framework

`edge_gateway/fault_injection.py` provides `force_publish_failures()` (a
context manager that makes `publish()` report failure for a fixed count,
without touching the network -- used by the deterministic tests) plus
`malformed_frame()` and `out_of_range_frame()` builders that reuse
`ingestion.py`/`validation.py`'s existing rejection paths. The real demo
uses an actual `docker compose stop mosquitto` for the outage scenario
(see README.md) -- Docker Compose is already this project's one broker
mechanism, so there's no need to fake that particular failure mode.

### Testing strategy, extended

`test_buffer.py` and `test_fault_injection.py` are new no-broker-needed
unit tests, alongside `test_gateway_unit.py` (mocked bus/publisher/
buffer, verifying `run_once()` calls `try_reconnect()` and then
`_replay_buffered()` only on a successful reconnect -- fast and
deterministic, no real broker). `test_mqtt_publisher.py` gained backoff/
disconnect tests using `pytest`'s `monkeypatch` on the underlying
paho-mqtt client, so timing and network failures are simulated rather
than waited-for. `test_gateway_integration.py` gained three new
real-broker tests (publish failure buffers the event; the gateway keeps
ingesting/validating through an outage; buffered events replay in order
and drain the buffer) built with `force_publish_failures()` for a
deterministic "outage," while the three pre-existing Phase 3 tests in
that file are unchanged except for one added assertion each (`buffer
stays empty`) confirming a rejected or ignored frame is never buffered
either.

## Publishing to AWS IoT Core (Phase 5)

Phase 5 does not add a second publisher implementation. AWS IoT Core is
plain MQTT over TLS -- exactly the protocol `mqtt_publisher.py` already
speaks -- so `MqttPublisher` gained three optional constructor arguments
(`tls_ca_certs`, `tls_certfile`, `tls_keyfile`) instead. All three must
be given together, or a `ValueError` is raised immediately; given none
(every Phase 1-4 caller), the client behaves exactly as before. `connect()`,
`publish()`, `try_reconnect()`, and every line of Phase 4's buffering/
backoff logic are unchanged.

`edge_gateway/cloud_publisher.py` is the one place that decides which
broker a run actually talks to, by reading `AWS_IOT_*` environment
variables (see `docs/aws-setup.md`): none set -> build a local Mosquitto
`MqttPublisher`; all four required ones set and valid -> build an
AWS-IoT-Core-pointed one with TLS; anything in between -> raise
`AwsIotConfigError` and refuse to start, rather than silently falling
back to local Mosquitto. `gateway.py` is not part of this decision at
all -- it receives whichever `MqttPublisher` `run_demo.py` (via
`cloud_publisher.py`) constructed, and has no branch anywhere for "which
broker is this."

The MQTT topic scheme and payload are identical either way:
`vehicle/{vehicle_id}/telemetry/{source_ecu}/{signal_name}`, carrying
`TelemetryEvent`'s own JSON -- no schema or topic change was needed for
AWS IoT Core. On the AWS side, one IoT Rule
(`SELECT * FROM 'vehicle/+/telemetry/+/+'`, see `infra/iot_rule.tf`)
routes matching telemetry to CloudWatch Logs; Kinesis is deliberately
deferred (see `ARCHITECTURE.md`'s Phase 5 decision row).
