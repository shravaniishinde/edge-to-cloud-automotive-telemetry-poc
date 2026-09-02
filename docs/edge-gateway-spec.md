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

`edge_gateway/tests/conftest.py`'s `mosquitto_broker` fixture launches
`mosquitto` directly as a subprocess (not via Docker) on port `18830` (not
the default `1883`, so it never collides with a broker a developer might
already have running), and skips the tests that need it if the
`mosquitto` binary isn't installed. This exists because this project's
sandbox (and possibly some CI runners) can't rely on a working Docker
daemon, but `apt-get install mosquitto` reliably provides the binary.
`docker/docker-compose.yml` is the equivalent setup for a developer's own
machine or manual demo use (`docker compose up`, then `python run_demo.py`)
-- same broker, same config concept, different launch mechanism; there is
no second, competing Mosquitto configuration to keep in sync.
