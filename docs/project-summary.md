# Project summary: explanations, interview Q&A, resume bullets

Reusable material for talking about this project. Everything here is
grounded in the actual implementation; file references point to where
each claim can be checked.

## 30-second explanation

> I built a no-hardware automotive telemetry pipeline in Python. Three
> simulated ECUs send CAN frames on a virtual bus; an edge gateway
> decodes, validates, and normalizes them and publishes to MQTT, either a
> local Mosquitto broker or AWS IoT Core over mutual TLS, provisioned
> with Terraform. The interesting part is resilience: when the broker is
> unreachable, the gateway keeps collecting, buffers to SQLite, reconnects
> with exponential backoff, and replays in strict FIFO order with
> at-least-once delivery. It also has UDS diagnostics with a deterministic
> anomaly analyzer, and a read-only engineering dashboard, all covered by
> broker-backed integration tests in CI.

## 2-minute explanation

**Problem.** A vehicle produces telemetry continuously, but its cloud
link is unreliable. An edge component has to turn raw bus data into
trustworthy events, keep working offline, and catch up without losing or
reordering data, while staying observable.

**Architecture.** Three logical ECUs (powertrain, battery, body) send 11
CAN messages at 10, 2, and 1 Hz on a `python-can` virtual bus. The Edge
Gateway filters telemetry CAN IDs, decodes them through one shared signal
registry, rejects physically implausible values, and normalizes each
reading into a `TelemetryEvent` JSON payload on
`vehicle/{vehicle_id}/telemetry/{ecu}/{signal}`. It publishes with MQTT
QoS 1 to local Mosquitto, or, when `AWS_IOT_*` is configured, to AWS IoT
Core over mutual TLS, where an IoT Rule forwards the telemetry to
CloudWatch Logs.

**Resilience.** A publish only counts as successful once the broker's
PUBACK arrives. Otherwise the event goes into a bounded SQLite buffer. The
gateway loop retries the connection with exponential backoff (1 s up to
30 s), and on reconnect replays the buffer oldest-first before reading the
next CAN frame, stopping at the first unconfirmed row so nothing is
reordered. Rows are deleted only after acknowledgement: at-least-once.

**Observability and diagnostics.** Every gateway run has a `session_id`
and every event an `event_id`, both in the payload and on structured
JSON log lines; `GatewayMetrics` counts processed, rejected, failed,
buffered, and replayed events. Separately, a UDS client/server over ISO-TP
reads DIDs and DTCs, and three deterministic rules flag suspicious
patterns; an optional LLM step may only add explanation text.

**Dashboard and testing.** A read-only dashboard (standard-library HTTP
plus Server-Sent Events) shows live telemetry, metrics, and the resilience
lifecycle, and can drive a simulated outage. The test suite includes
real-broker integration tests, a full ECU-to-subscriber scenario, and a
self-verifying resilience demo. CI runs it all against a real Mosquitto
broker and fails if that broker is missing.

## Technical highlights

- **Python 3.11** throughout (local, Docker, CI), pinned dependencies,
  no web framework.
- **CAN:** `python-can` virtual bus; one registry (`common/can_signal_map.py`)
  defines IDs, byte layout, scale, units, and valid ranges for all 11
  messages.
- **UDS:** `udsoncan` + `can-isotp`; hand-rolled server (udsoncan is
  client-only) supporting 0x10, 0x22, and 0x19.
- **Schemas:** Pydantic `TelemetryEvent` / `DiagnosticEvent`. Shape is
  validated by the schema; plausibility is checked by the gateway.
- **MQTT:** paho-mqtt 2.x, QoS 1, success means an actual PUBACK
  (`is_published()`), not just "no exception".
- **Buffer:** SQLite (`AUTOINCREMENT` ordering, 5,000-row cap with
  drop-oldest, delete only after confirmation).
- **Reconnect:** backoff-gated `try_reconnect()` driven by the gateway
  loop; no extra thread.
- **AWS:** Terraform (Thing, certificate, least-privilege policy, IoT
  Rule, CloudWatch Logs); mutual TLS through the same publisher class; no
  AWS SDK at runtime.
- **Analyzer:** stateless, deterministic windowed rules; LLM layer is
  structurally separate and never called without an API key.
- **Observability:** JSON logging with a logger adapter that attaches
  `session_id`; `GatewayMetrics` / `MetricsSnapshot`.
- **Fault injection:** named deterministic scenarios, including a
  simulated connection outage that exercises the real reconnect path.
- **Dashboard:** `ThreadingHTTPServer`, SSE, read-only MQTT subscriber and
  SQLite probe, fixed action allowlist, CSP, `textContent`-only rendering.
- **Delivery:** Docker/Compose (optional dashboard profile) and GitHub
  Actions with a real Mosquitto service.

## Interview questions and answers

**1. Why a virtual CAN bus instead of SocketCAN?**
SocketCAN (`vcan0`) needs a Linux kernel module and usually a privileged
container, so it doesn't work on Windows, Docker Desktop, or hosted CI.
`python-can`'s virtual backend behaves the same at the application level
everywhere. The trade-off: it only shares frames within one process, so
the ECUs and the gateway run as threads in one process (and one container).

**2. How is a CAN frame turned into telemetry?**
The gateway ignores any arbitration ID not in the signal registry, such
as the UDS IDs 0x7E0/0x7E8. For known IDs it unpacks the bytes (uint8,
int16, uint16, or uint32), applies scale and offset, and builds a
`TelemetryEvent` with the gateway's `session_id` and a new `event_id`. A
wrong payload length is dropped and logged rather than crashing the loop.

**3. What does validation do?**
Pydantic checks shape (types, known ECU and signal names). The gateway
then checks the decoded value against that signal's `valid_range` from
the same registry, and rejects and logs out-of-range values (e.g. 250 %
state of charge). Invalid data is never published and never buffered.

**4. What UDS services are implemented, and why only those?**
`DiagnosticSessionControl` (0x10), `ReadDataByIdentifier` (0x22: VIN
0xF190 plus live speed and RPM DIDs), and `ReadDTCInformation` (0x19,
returning a static list with P0217 and P0420), over ISO-TP. That's enough
to show request/response diagnostics and negative responses without
building the whole ISO 14229 catalog. Sessions are tracked but don't gate
services.

**5. Why MQTT, and why QoS 1?**
MQTT is the standard lightweight pub/sub protocol for IoT and is what AWS
IoT Core speaks natively, so the same client works locally and in the
cloud. QoS 1 gives a broker acknowledgement (PUBACK) per message, which is
exactly the signal needed to decide when a buffered event can be deleted.

**6. What counts as a successful publish?**
Only `is_published()` becoming true, which in paho-mqtt happens when the
PUBACK arrives. `wait_for_publish()` doesn't raise on timeout, so the
code checks explicitly. A timeout, disconnect, or missing acknowledgement
all return False and the event is buffered.

**7. Why SQLite for the buffer?**
It's in the standard library, needs no separate service, is
transactional, and survives a process restart. A restarted gateway replays
whatever the previous run left. Redis or RabbitMQ would be an extra
service for a single-process local queue.

**8. How is FIFO order guaranteed during replay?**
Rows are read `ORDER BY id` (an `AUTOINCREMENT` key) in batches of 100.
Replay stops at the first row that isn't acknowledged, and runs inside the
gateway loop before the next CAN frame is read, so no newer live event
can overtake older buffered ones.

**9. How does reconnect work without hammering the broker?**
Each loop iteration calls `try_reconnect()`, which is usually just a
timestamp check. A real attempt happens only when the backoff window has
elapsed: 1 s, doubling, capped at 30 s. A successful reconnect triggers a
replay; so does a non-empty buffer at startup.

**10. Why at-least-once and not exactly-once?**
A row is deleted only after its PUBACK, so a crash between the
acknowledgement and the delete replays that event again. Exactly-once
would need a transaction spanning the broker and SQLite. Consumers can
de-duplicate by `event_id`. Manual testing also found extra duplicates
during a real broker outage (likely paho re-sending queued messages that
were also buffered); it's documented and not yet root-caused.

**11. What are `session_id` and `event_id`?**
`session_id` is minted once per `EdgeGateway` instance and identifies one
gateway run; it's on every gateway log line and in every payload.
`event_id` is minted per decoded frame and identifies one event. A replay
keeps the original IDs, and the replay log line lists the replayed
`event_id`s under the replaying run's `session_id`.

**12. How is the device authenticated to AWS IoT Core?**
Mutual TLS on port 8883 with an X.509 device certificate and private key
generated by Terraform, plus the Amazon root CA. The IoT policy only
allows connecting as the client ID equal to the Thing name and publishing
under `vehicle/*`. The certificate, key, `.env`, and Terraform state are
gitignored. In production each device would generate its own key and
submit a CSR.

**13. What happens in AWS after the message arrives?**
An IoT Rule (`SELECT * FROM 'vehicle/+/telemetry/+/+'`) forwards
telemetry, IDs included, to a CloudWatch Logs group. Kinesis was
deliberately deferred: fan-out would be another IoT Rule action, with no
gateway change.

**14. How does the dashboard get its data without being in the pipeline?**
It subscribes to the telemetry topics read-only. Gateway metrics live
inside the process that runs the gateway, and the virtual bus is
process-local, so the dashboard can host a demo gateway run in its own
process and read its real `GatewayMetrics`. Other gateways' metrics are
shown as unavailable, never estimated. Buffer depth comes from a separate
read-only SQLite connection.

**15. How is the resilience state on the dashboard derived?**
From observable facts only: publisher connection flag, SQLite row count,
recent replay log lines, and whether the dashboard injected or cleared
the outage. REPLAYING requires a replay batch in the last 3 s, because
during a real outage the publisher can report "connected" while publishes
still fail; testing caught the dashboard mislabelling that case.

**16. How does fault injection work?**
Named deterministic scenarios in `edge_gateway/fault_injection.py`: force
N publish failures, a simulated connection outage, and malformed or
out-of-range frames. The simulated outage makes the gateway see a real
disconnect, so its own reconnect and replay code runs. That scenario was
added after finding that forced publish failures never trigger replay,
because the publisher still reports connected.

**17. What is the LLM's role?**
Advisory only. The three rules (repeated negative responses, repeated DTC
queries, repeated P0217) decide everything. `analyzer.py` never imports
the LLM module; a caller can afterwards ask it to fill
`AnomalyReport.llm_explanation`. Without `ANTHROPIC_API_KEY` it returns
None immediately, and tests use a fake client.

**18. How is it tested?**
Unit tests per module; broker-backed integration tests against real
Mosquitto with a real subscriber; a full scenario from seeded ECUs to a
subscriber, checking exact first values, IDs, and log traceability; a
self-verifying resilience demo; and dashboard state, HTTP, and real-chain
tests. CI installs Mosquitto and sets `MQTT_BROKER_REQUIRED=1` so those
tests can't silently skip. The AWS smoke test runs manually with real
credentials.

**19. What are the main limitations?**
Process-local virtual bus; a static DTC list; a simulated outage that
isn't a network failure; duplicate deliveries in real outages; a
dashboard that is unauthenticated, in-memory, and localhost-only; and no
Kinesis or cloud persistence beyond CloudWatch Logs.

**20. What would you do next?**
Root-cause the real-outage duplicates (likely by discarding paho's queued
copy of a publish the gateway has already buffered, or marking the
connection up only on CONNACK), then add dashboard authentication before
it leaves localhost, and add cloud streaming only if more consumers need
it.

## Resume bullets

- Built a no-hardware automotive telemetry pipeline in Python 3.11:
  3 simulated ECUs on a `python-can` virtual CAN bus (11 messages) feeding
  an edge gateway that decodes, validates, and publishes normalized
  events over MQTT to Mosquitto or AWS IoT Core.
- Designed store-and-forward resilience: SQLite buffering on
  unacknowledged QoS 1 publishes, exponential-backoff reconnect, and
  strict FIFO replay with at-least-once delivery, verified by a
  self-checking outage/recovery scenario.
- Provisioned AWS IoT Core with Terraform (Thing, X.509 certificate,
  least-privilege policy, IoT Rule to CloudWatch Logs) and connected over
  mutual TLS with no runtime AWS SDK.
- Implemented UDS diagnostics over ISO-TP and a deterministic anomaly
  analyzer, with an optional LLM layer that can only add advisory
  explanations to rule-based findings.
- Added observability (per-run `session_id`, per-event `event_id`,
  structured logs, gateway metrics), a read-only engineering dashboard,
  and CI running broker-backed integration tests.
