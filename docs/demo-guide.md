# Demo guide (about 10 minutes)

A reproducible walkthrough for presenting the project. All commands exist
in the repository and are shown for Windows PowerShell from the
repository root, with the project's Python 3.11 virtual environment (see
the README's Quick start).

## Before the presentation (5 minutes, not on screen)

1. Start Docker Desktop, then the local broker:

   ```powershell
   docker compose -f docker/docker-compose.yml up -d mosquitto
   ```

2. Start the dashboard in its own terminal and leave it running:

   ```powershell
   .\.venv\Scripts\python.exe -m dashboard.backend
   ```

3. Open http://127.0.0.1:8080. Do one dry run (**Start live demo**, then
   **Stop live demo**) so nothing is cold.
4. Optional, for the AWS segment: open AWS Console → IoT Core → MQTT test
   client in another browser tab (region ap-south-1) and subscribe to
   `vehicle/#`. Keep `.env`, certificate files, and Terraform output off
   screen.

Only one dashboard instance can run on port 8080; a second one refuses to
start with a clear message.

## 0:00–1:00 · Overview and architecture

Show the README's architecture diagram. Say:

- Three simulated ECUs send 11 CAN messages on a virtual bus; no hardware.
- The Edge Gateway decodes, validates, normalizes, and publishes with MQTT
  QoS 1, to local Mosquitto here or AWS IoT Core in the cloud.
- The focus is what happens when the network fails: buffer, reconnect,
  replay in order.
- The dashboard only observes; it is not in the telemetry path.

## 1:00–2:00 · Start the system

In the dashboard, click **Start live demo**. This runs the same wiring as
`run_demo.py` (3 ECU threads plus the gateway on a virtual CAN bus)
inside the dashboard process, which is how the dashboard can read the
gateway's real metrics. Point out:

- **System status:** gateway *running*, its `session_id`, broker
  *connected*, telemetry *flowing* at roughly 40 events/s.
- Only one broker, one process, no cloud account needed.

## 2:00–3:30 · Live telemetry

Scroll to **Live telemetry**:

- Charts for speed, RPM, state of charge, and pack current, with units
  and a 60 s window.
- The table of all 11 signals with their latest value, unit, timestamp,
  and `event_id`.
- **Vehicles / ECUs:** per-ECU event counts. Powertrain sends at 10 Hz,
  battery at 2 Hz, body at 1 Hz.

To show that this is real MQTT traffic, optionally run in a spare
terminal:

```powershell
docker compose -f docker/docker-compose.yml exec mosquitto mosquitto_sub -t "vehicle/#" -v
```

Each line is a topic plus a `TelemetryEvent` JSON payload. Press Ctrl+C
to stop.

## 3:30–5:00 · Gateway metrics and event stream

- **Gateway metrics:** *Processed* climbs. *Rejected*, *Publish failures*,
  *Buffered*, *Replayed*, and *Dropped* are all 0. Explain that these are
  cumulative counters, and that *Buffer depth (now)* is the current SQLite
  row count, a different thing.
- **Event stream:** every row has an `event_id` (unique per event) and the
  run's `session_id`, and its status is *live*.
- **Activity:** the gateway's own structured log lines.

## 5:00–7:00 · Trigger an outage

Click **Inject MQTT outage**. Within a second or two:

- The lifecycle moves to **BUFFERING**; *Gateway → MQTT* shows
  *disconnected*.
- *Publish failures* and *Buffered* climb together, and *Buffer depth
  (now)* climbs: events are being written to SQLite.
- Telemetry flow turns *stale* because nothing reaches the broker, while
  the ECUs and the gateway keep running.
- **Activity** shows reconnect attempts failing with growing backoff.

Wait **about 5–6 seconds**, then click **Clear outage**. (Backoff doubles:
attempts come at roughly 1, 3, 7, 15 s after the outage starts, so a short
outage recovers quickly. After a 20 s outage you would wait up to 16 s.)

## 7:00–8:00 · Recovery and FIFO replay

- The lifecycle shows **RECONNECTING** until the gateway's next
  backoff-timed attempt, then **RECOVERED** (the replay itself usually
  takes under a second).
- *Replayed* equals *Buffered*, *Buffer depth (now)* is back to 0, and
  *Dropped* is 0.
- In the **Event stream**, replayed rows are tagged *replayed*, with
  their original timestamps and `event_id`s. The charts show no gap,
  because replayed events fill in at their original times.

Explain FIFO: replay runs oldest-first before the gateway reads the next
CAN frame, and stops at the first unacknowledged row. Delivery is
at-least-once.

For a scripted, self-checking version (after clicking **Stop live
demo**, since both use the same virtual bus), click **Run scripted
resilience check** and wait for *PASS* under the controls, or run it in a
terminal:

```powershell
.\.venv\Scripts\python.exe -m scenarios.resilience_demo
```

It prints six stages and a PASS/FAIL summary: buffered vs. replayed
counts, FIFO verified, event IDs preserved, final buffer depth 0.

## 8:00–9:00 · UDS diagnostics and the analyzer

Click **Run UDS diagnostic session**. Under **Diagnostics / anomalies**:

- About 10 real UDS transactions over ISO-TP on the virtual bus: session
  control, VIN, live speed and RPM, three DTC reads, and three reads of an
  unsupported DID (negative responses).
- Three deterministic findings: repeated negative responses, repeated DTC
  queries, repeated P0217 activity. The script is designed to exercise
  all three rules.
- *LLM explanation: not requested.* Explain: rules detect, the LLM may
  only explain afterwards, and it is never called here.

## 9:00–10:00 · AWS IoT Core and the cloud path

Explain with the README's diagram:

- The same gateway code publishes to AWS IoT Core when the `AWS_IOT_*`
  variables are set: mutual TLS on 8883 with an X.509 device certificate.
- Terraform provisions the Thing, certificate, a least-privilege policy,
  and an IoT Rule that forwards `vehicle/+/telemetry/+/+` to CloudWatch
  Logs.
- Secrets are never committed; CI never needs AWS.

If AWS is prepared (see [aws-setup.md](aws-setup.md), including
`AWS_IOT_CLIENT_ID`), run the smoke test with the variables exported and
show `{"smoke_test": true}` arriving in the MQTT test client:

```powershell
.\.venv\Scripts\python.exe -m pytest edge_gateway/tests/test_aws_iot_integration.py -v -rs
```

Otherwise, describe it: this test has passed against the provisioned
endpoint, and the message was received in the AWS test client.

## Afterwards

Click **Stop live demo**, press Ctrl+C in the dashboard terminal, then:

```powershell
docker compose -f docker/docker-compose.yml stop mosquitto
```

If you exported `AWS_IOT_*` variables, clear them with
`Get-ChildItem Env:AWS_IOT_* | Remove-Item`.

## If something goes wrong

| Symptom | Fix |
|---|---|
| Dashboard shows *broker: disconnected* | Mosquitto isn't running: `docker compose -f docker/docker-compose.yml up -d mosquitto`. |
| "Cannot listen on 127.0.0.1:8080" | Another dashboard is already running; stop it or pass `--port 8081`. |
| *Start live demo* is refused | A scripted resilience check is still running, or a live demo is already running. |
| Recovery seems slow after **Clear outage** | Normal after a long outage: the reconnect waits for its backoff window (up to 30 s). |
| AWS smoke test is `SKIPPED` | The `AWS_IOT_*` variables weren't exported in that terminal. |
