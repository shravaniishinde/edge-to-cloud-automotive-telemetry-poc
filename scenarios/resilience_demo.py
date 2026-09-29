"""
Phase 9: a runnable, self-verifying MQTT outage -> recovery demonstration.

    python -m scenarios.resilience_demo            # needs local Mosquitto on localhost:1883

It wires the real system exactly like run_demo.py -- 3 seeded simulated
ECUs and the EdgeGateway as threads in ONE process (python-can's virtual
bus is process-local), the real SQLite TelemetryBuffer, the real
MqttPublisher talking to a real local Mosquitto -- and then walks through:

    [1/6] normal telemetry is published and received by a real subscriber
    [2/6] an MQTT connection outage is injected
          (edge_gateway.fault_injection.simulated_connection_outage)
    [3/6] the gateway keeps ingesting; publishes fail and are buffered in SQLite
    [4/6] the outage is cleared; the gateway's own backoff-gated reconnect notices
    [5/6] the gateway replays the buffer in FIFO order, before any new live event
    [6/6] live telemetry resumes, the buffer is empty, metrics add up

Nothing here re-implements buffering, reconnect, replay, or metrics. This
module only *drives* the scenario and *observes* it, through two
read-only channels:

- the gateway's existing structured log records (a logging.Handler on the
  "edge_gateway" logger) -- which event_ids were published, buffered, and
  replayed, in the exact order the gateway did it; and
- a real MQTT subscriber -- what actually reached the broker.

Every stage waits on an observable condition (event counts, a reconnect
log line, the replay covering every buffered event_id) with a bounded
timeout; a stage that times out fails the demo with a clear message
instead of hanging, and cleanup (threads, bus handles, MQTT clients,
SQLite file, temp dir) always runs.

Delivery is at-least-once (see docs/edge-gateway-spec.md): a duplicate is
possible in principle if the process dies between a broker ack and the
buffer row's deletion. This controlled demo normally sees none, and
reports -- rather than hides -- any it does see.

Local Mosquitto only, by design: this builds MqttPublisher directly
rather than via cloud_publisher.build_publisher_from_env(), so AWS_IOT_*
variables are ignored and no AWS or Anthropic configuration is needed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import shutil
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, ContextManager, Dict, List, Optional

import paho.mqtt.client as mqtt

from common.telemetry_schema import TelemetryEvent
from edge_gateway.buffer import TelemetryBuffer
from edge_gateway.fault_injection import simulated_connection_outage
from edge_gateway.gateway import EdgeGateway
from edge_gateway.logging_config import JsonFormatter, configure_logging
from edge_gateway.mqtt_publisher import MqttPublisher
from simulation.can_bus import get_bus, run_ecu
from simulation.ecus.battery_ecu import BatteryECU
from simulation.ecus.body_ecu import BodyECU
from simulation.ecus.powertrain_ecu import PowertrainECU

logger = logging.getLogger("scenarios.resilience_demo")

TOTAL_STAGES = 6
THREAD_JOIN_TIMEOUT_SECONDS = 5.0

# Gateway/publisher log messages this demo observes (existing messages --
# see edge_gateway/gateway.py and mqtt_publisher.py).
MSG_PUBLISHED = "published telemetry event"
MSG_BUFFERED = "publish failed -- buffered for replay"
MSG_REPLAYED = "replayed buffered events"
MSG_RECONNECTED = "MQTT reconnected"
MSG_RECONNECT_FAILED_PREFIX = "MQTT reconnect attempt failed"

OutageFactory = Callable[[MqttPublisher], ContextManager[None]]


class DemoFailure(Exception):
    """A scenario stage did not reach its expected state."""


@dataclass
class DemoConfig:
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    vehicle_id: Optional[str] = None           # default: a unique RESILIENCE-DEMO-xxxxxxxx
    buffer_path: Optional[str] = None          # default: a fresh temp file, deleted afterwards
    seed: int = 42
    normal_events: int = 40                    # live publishes to see before the outage
    outage_events: int = 80                    # events to buffer before clearing the outage
    recovery_events: int = 40                  # live publishes to see after replay
    stage_timeout_seconds: float = 20.0
    recovery_timeout_seconds: float = 45.0     # > the 30s reconnect-backoff cap
    outage: OutageFactory = simulated_connection_outage  # injectable for tests only
    announce: Callable[[str], None] = print    # human-readable stage output
    show_gateway_logs: bool = False            # also let gateway logs reach the root logger


@dataclass
class DemoResult:
    passed: bool = False
    failure_reason: Optional[str] = None
    failed_stage: Optional[str] = None
    session_id: Optional[str] = None
    vehicle_id: Optional[str] = None
    published_before_outage: int = 0
    buffered_during_outage: int = 0
    max_buffer_depth: int = 0
    failed_reconnect_attempts: int = 0
    replayed: int = 0
    replayed_received: int = 0
    live_published_after_recovery: int = 0
    live_received_after_recovery: int = 0
    final_buffer_depth: Optional[int] = None
    fifo_replay_verified: bool = False
    event_ids_preserved: bool = False
    no_live_event_overtook_replay: bool = False
    recovered: bool = False
    buffer_drained: bool = False
    duplicates_received: int = 0
    metrics: Dict[str, object] = field(default_factory=dict)
    cleanup_completed: bool = False
    threads_alive_after_cleanup: int = 0
    buffered_event_ids: List[str] = field(default_factory=list, repr=False)

    def summary_fields(self) -> Dict[str, object]:
        fields = asdict(self)
        fields.pop("buffered_event_ids")
        return fields


class _GatewayLogRecorder(logging.Handler):
    """Records the gateway's own structured log records, in emission
    order, so the demo can see exactly what the gateway did -- without
    any new hook in the gateway itself."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self._lock_ = threading.Lock()
        self._records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock_:
            self._records.append(record)

    def records(self) -> List[logging.LogRecord]:
        with self._lock_:
            return list(self._records)

    def event_ids(self, message: str) -> List[str]:
        return [r.event_id for r in self.records() if r.getMessage() == message]

    def replayed_event_ids(self) -> List[str]:
        ids: List[str] = []
        for r in self.records():
            if r.getMessage() == MSG_REPLAYED:
                ids.extend(r.event_ids)
        return ids

    def count(self, predicate: Callable[[str], bool]) -> int:
        return sum(1 for r in self.records() if predicate(r.getMessage()))


class _TelemetryObserver:
    """A real MQTT subscriber: what actually reached the broker. Waits for
    the broker's SUBACK before returning, so nothing published afterwards
    can be missed."""

    def __init__(self, host: str, port: int, topic: str, timeout: float) -> None:
        self._lock = threading.Lock()
        self._received: List[TelemetryEvent] = []
        self._topics: List[str] = []
        self._subscribed = threading.Event()
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = lambda c, u, f, rc, p: c.subscribe(topic, qos=1)
        self._client.on_subscribe = lambda c, u, mid, rcs, p: self._subscribed.set()
        self._client.on_message = self._on_message
        self._client.connect(host, port, keepalive=10)
        self._client.loop_start()
        if not self._subscribed.wait(timeout):
            self.close()
            raise DemoFailure(f"subscriber got no SUBACK for {topic!r} within {timeout}s")

    def _on_message(self, client, userdata, msg) -> None:
        event = TelemetryEvent.model_validate_json(msg.payload)
        with self._lock:
            self._received.append(event)
            self._topics.append(msg.topic)

    def received(self) -> List[TelemetryEvent]:
        with self._lock:
            return list(self._received)

    def received_ids(self) -> List[str]:
        return [e.event_id for e in self.received()]

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


def _wait_for(condition: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    if not condition():
        raise DemoFailure(f"timed out after {timeout:.0f}s waiting for {what}")


def _is_subsequence(needles: List[str], haystack: List[str]) -> bool:
    """True if `needles` appear in `haystack` in the same relative order."""
    iterator = iter(haystack)
    return all(any(item == needle for item in iterator) for needle in needles)


def run_scenario(config: DemoConfig) -> DemoResult:
    """Runs the whole outage -> recovery scenario and returns what was
    observed. Never raises for a scenario failure (see
    `result.passed`/`failure_reason`), never hangs past its timeouts, and
    always cleans up."""
    result = DemoResult()
    stage = "setup"
    announce = config.announce
    vehicle_id = config.vehicle_id or f"RESILIENCE-DEMO-{uuid.uuid4().hex[:8]}"
    result.vehicle_id = vehicle_id

    recorder = _GatewayLogRecorder()
    gateway_logger = logging.getLogger("edge_gateway")
    previous_level = gateway_logger.level
    previous_propagate = gateway_logger.propagate
    stop_event = threading.Event()
    threads: List[threading.Thread] = []
    gateway: Optional[EdgeGateway] = None
    buffer: Optional[TelemetryBuffer] = None
    observer: Optional[_TelemetryObserver] = None

    with contextlib.ExitStack() as cleanup:
        try:
            gateway_logger.addHandler(recorder)
            if gateway_logger.getEffectiveLevel() > logging.INFO:
                gateway_logger.setLevel(logging.INFO)
            cleanup.callback(gateway_logger.setLevel, previous_level)
            cleanup.callback(gateway_logger.removeHandler, recorder)
            # Without this, the outage's per-event WARNING/ERROR lines would
            # reach Python's last-resort stderr handler and bury the stage
            # output. --show-logs turns them back on (as JSON).
            gateway_logger.propagate = config.show_gateway_logs
            cleanup.callback(setattr, gateway_logger, "propagate", previous_propagate)

            # --- setup: same wiring as run_demo.py ---
            if config.buffer_path is None:
                temp_dir = tempfile.mkdtemp(prefix="resilience-demo-")
                cleanup.callback(shutil.rmtree, temp_dir, ignore_errors=True)
                buffer_path = Path(temp_dir) / "buffer.db"
            else:
                buffer_path = Path(config.buffer_path)
                if buffer_path.exists():
                    raise DemoFailure(
                        f"buffer path {buffer_path} already exists; the demo needs a fresh buffer "
                        "so a leftover backlog can't skew its counts"
                    )

            try:
                observer = _TelemetryObserver(
                    config.mqtt_host, config.mqtt_port, f"vehicle/{vehicle_id}/telemetry/#",
                    timeout=config.stage_timeout_seconds,
                )
            except OSError as exc:
                raise DemoFailure(
                    f"cannot reach MQTT broker at {config.mqtt_host}:{config.mqtt_port} ({exc}). "
                    "Start it with: docker compose -f docker/docker-compose.yml up -d mosquitto"
                ) from exc
            cleanup.callback(observer.close)

            gateway_bus = get_bus()  # opened before any ECU sends
            cleanup.callback(gateway_bus.shutdown)
            publisher = MqttPublisher(
                host=config.mqtt_host, port=config.mqtt_port,
                logger=logging.getLogger("edge_gateway.mqtt_publisher"),
            )
            try:
                publisher.connect()
            except OSError as exc:
                raise DemoFailure(f"gateway cannot connect to MQTT broker ({exc})") from exc
            cleanup.callback(publisher.disconnect)
            buffer = TelemetryBuffer(buffer_path)
            cleanup.callback(buffer.close)
            gateway = EdgeGateway(gateway_bus, publisher, buffer, vehicle_id=vehicle_id)
            result.session_id = gateway.session_id

            ecus = [
                PowertrainECU("resilience-demo-ecus", vehicle_id, rng_seed=config.seed),
                BatteryECU("resilience-demo-ecus", vehicle_id, rng_seed=config.seed),
                BodyECU("resilience-demo-ecus", vehicle_id, rng_seed=config.seed),
            ]
            ecu_buses = [get_bus() for _ in ecus]
            for bus in ecu_buses:
                cleanup.callback(bus.shutdown)
            threads = [threading.Thread(target=gateway.run, args=(stop_event,), name="gateway", daemon=True)] + [
                threading.Thread(target=run_ecu, args=(ecu, bus, stop_event, ecu.TICK_INTERVAL_SECONDS),
                                 name=type(ecu).__name__, daemon=True)
                for ecu, bus in zip(ecus, ecu_buses)
            ]

            def _stop_threads() -> None:
                stop_event.set()
                for thread in threads:
                    thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)

            # Registered last, so it runs FIRST during cleanup (LIFO):
            # threads stop before the bus/publisher/buffer they use close.
            cleanup.callback(_stop_threads)

            announce("=" * 60)
            announce("RESILIENCE DEMO")
            announce(f"  gateway session_id : {gateway.session_id}")
            announce(f"  vehicle_id         : {vehicle_id}")
            announce(f"  broker             : {config.mqtt_host}:{config.mqtt_port}")
            announce(f"  SQLite buffer      : {buffer_path}")
            announce("=" * 60)
            for thread in threads:
                thread.start()

            # [1/6] NORMAL OPERATION
            stage = "normal operation"
            announce(f"[1/{TOTAL_STAGES}] Starting normal telemetry...")
            _wait_for(lambda: len(observer.received_ids()) >= config.normal_events,
                      config.stage_timeout_seconds, f"{config.normal_events} events to reach the subscriber")
            announce(f"      {gateway.metrics.processed} events published and received so far")

            with config.outage(publisher):
                # [2/6] INJECT OUTAGE
                stage = "outage injection"
                announce(f"[2/{TOTAL_STAGES}] Injecting MQTT outage...")
                _wait_for(lambda: gateway.metrics.buffered >= 1, config.stage_timeout_seconds,
                          "the gateway to detect the outage (first failed publish)")
                announce("      gateway detected the outage: publish failed, event buffered")

                # [3/6] BUFFERING
                stage = "buffering"
                announce(f"[3/{TOTAL_STAGES}] Buffering telemetry in SQLite while MQTT is down...")
                _wait_for(lambda: gateway.metrics.buffered >= config.outage_events,
                          config.stage_timeout_seconds, f"{config.outage_events} events to be buffered")
                announce(f"      {gateway.metrics.buffered} events buffered, "
                         f"{recorder.count(lambda m: m.startswith(MSG_RECONNECT_FAILED_PREFIX))} "
                         "reconnect attempts failed (exponential backoff)")

            # [4/6] RESTORE CONNECTIVITY
            stage = "recovery"
            announce(f"[4/{TOTAL_STAGES}] Restoring MQTT connectivity (waiting for the gateway's backoff-gated reconnect)...")
            _wait_for(lambda: recorder.count(lambda m: m == MSG_RECONNECTED) >= 1,
                      config.recovery_timeout_seconds, "the gateway to reconnect")
            result.recovered = True
            announce("      gateway reconnected")

            # [5/6] REPLAY
            stage = "replay"
            announce(f"[5/{TOTAL_STAGES}] Replaying buffered telemetry (FIFO)...")
            buffered_ids = recorder.event_ids(MSG_BUFFERED)
            _wait_for(lambda: len(recorder.replayed_event_ids()) >= len(buffered_ids),
                      config.stage_timeout_seconds, f"all {len(buffered_ids)} buffered events to be replayed")
            _wait_for(lambda: set(buffered_ids) <= set(observer.received_ids()),
                      config.stage_timeout_seconds, "every replayed event to reach the subscriber")
            announce(f"      {len(recorder.replayed_event_ids())} buffered events replayed and received "
                     "(includes events buffered while waiting for the reconnect attempt)")

            # [6/6] RETURN TO NORMAL
            stage = "return to normal"
            replay_done_at = len(recorder.records())
            def _live_after_recovery() -> List[str]:
                return [r.event_id for r in recorder.records()[replay_done_at:] if r.getMessage() == MSG_PUBLISHED]
            _wait_for(lambda: len(_live_after_recovery()) >= config.recovery_events,
                      config.stage_timeout_seconds, f"{config.recovery_events} new live events after recovery")
            _wait_for(lambda: set(_live_after_recovery()) <= set(observer.received_ids()),
                      config.stage_timeout_seconds, "post-recovery live events to reach the subscriber")

            # --- stop, then verify against the final, settled state ---
            stage = "verification"
            _stop_threads()
            if any(t.is_alive() for t in threads):
                raise DemoFailure("a gateway/ECU thread did not stop")
            _verify(result, recorder, observer, gateway, buffer)
            announce(f"[6/{TOTAL_STAGES}] Recovery verified." if result.passed
                     else f"[6/{TOTAL_STAGES}] Recovery NOT verified: {result.failure_reason}")
        except DemoFailure as exc:
            result.passed = False
            result.failed_stage = stage
            result.failure_reason = str(exc)
            announce(f"FAILED during {stage}: {exc}")
        except Exception as exc:  # an unexpected error is still a clean FAIL, never a hang
            result.passed = False
            result.failed_stage = stage
            result.failure_reason = f"unexpected {type(exc).__name__}: {exc}"
            announce(f"FAILED during {stage}: {result.failure_reason}")
        finally:
            if gateway is not None:
                result.metrics = gateway.metrics_snapshot().as_log_fields()
            # ExitStack now runs every cleanup callback (LIFO), even on failure.

    result.threads_alive_after_cleanup = sum(1 for t in threads if t.is_alive())
    result.cleanup_completed = result.threads_alive_after_cleanup == 0
    return result


def _verify(result: DemoResult, recorder: _GatewayLogRecorder, observer: _TelemetryObserver,
            gateway: EdgeGateway, buffer: TelemetryBuffer) -> None:
    records = recorder.records()
    messages = [r.getMessage() for r in records]
    buffered_ids = recorder.event_ids(MSG_BUFFERED)
    replayed_ids = recorder.replayed_event_ids()
    first_buffered = messages.index(MSG_BUFFERED)
    last_replay = len(messages) - 1 - messages[::-1].index(MSG_REPLAYED)

    result.buffered_event_ids = buffered_ids
    result.published_before_outage = messages[:first_buffered].count(MSG_PUBLISHED)
    result.buffered_during_outage = len(buffered_ids)
    result.max_buffer_depth = max(r.buffer_size for r in records if r.getMessage() == MSG_BUFFERED)
    result.failed_reconnect_attempts = sum(1 for m in messages if m.startswith(MSG_RECONNECT_FAILED_PREFIX))
    result.replayed = len(replayed_ids)
    result.live_published_after_recovery = messages[last_replay:].count(MSG_PUBLISHED)

    # Everything the broker acknowledged must reach the subscriber.
    acknowledged = set(recorder.event_ids(MSG_PUBLISHED)) | set(replayed_ids)
    _wait_for(lambda: acknowledged <= set(observer.received_ids()), 5.0, "every acknowledged event to arrive")
    received = observer.received()
    received_ids = [e.event_id for e in received]
    by_id = {e.event_id: e for e in received}

    result.replayed_received = sum(1 for i in replayed_ids if i in by_id)
    live_after = {r.event_id for r in records[last_replay:] if r.getMessage() == MSG_PUBLISHED}
    result.live_received_after_recovery = sum(1 for i in live_after if i in by_id)
    result.duplicates_received = len(received_ids) - len(set(received_ids))

    # FIFO, gateway side: the replay order (SQLite ORDER BY id) equals the
    # order events were buffered, and no live publish slipped in between
    # the outage and the end of replay (replay drains before new traffic).
    result.fifo_replay_verified = replayed_ids == buffered_ids
    result.no_live_event_overtook_replay = MSG_PUBLISHED not in messages[first_buffered:last_replay]
    # FIFO, subscriber side: per topic (MQTT's ordering guarantee), the
    # replayed events arrived in buffered order, with non-decreasing
    # original timestamps.
    if result.fifo_replay_verified:
        for topic_ids in _group_by_topic(buffered_ids, by_id).values():
            received_on_topic = [i for i in received_ids if i in set(topic_ids)]
            if not _is_subsequence(topic_ids, received_on_topic):
                result.fifo_replay_verified = False
        timestamps = [by_id[i].timestamp for i in buffered_ids if i in by_id]
        if timestamps != sorted(timestamps):
            result.fifo_replay_verified = False

    # Identity: replayed payloads keep the event_id and the session_id of
    # the gateway run that originally buffered them (here, this same run
    # -- a replay never rewrites an event).
    result.event_ids_preserved = all(
        i in by_id and by_id[i].session_id == gateway.session_id and by_id[i].vehicle_id == result.vehicle_id
        for i in buffered_ids
    )

    snapshot = gateway.metrics_snapshot()
    result.metrics = snapshot.as_log_fields()
    result.final_buffer_depth = buffer.count()
    result.buffer_drained = result.final_buffer_depth == 0

    checks = {
        "outage produced buffered events": result.buffered_during_outage > 0,
        "gateway reconnected": result.recovered,
        "every buffered event was replayed": set(replayed_ids) == set(buffered_ids),
        "every replayed event was received": result.replayed_received == len(buffered_ids),
        "FIFO replay order": result.fifo_replay_verified,
        "no live event overtook the replay": result.no_live_event_overtook_replay,
        "event_id/session_id preserved": result.event_ids_preserved,
        "live telemetry resumed": result.live_received_after_recovery > 0,
        "buffer drained": result.buffer_drained,
        "metrics agree with observations": (
            snapshot.buffered == snapshot.publish_failures == len(buffered_ids)
            and snapshot.replayed == len(replayed_ids)
            and snapshot.rejected == 0 and snapshot.dropped == 0
            and snapshot.processed == len(recorder.event_ids(MSG_PUBLISHED))
        ),
    }
    failed = [name for name, ok in checks.items() if not ok]
    result.passed = not failed
    if failed:
        raise DemoFailure("verification failed: " + ", ".join(failed))


def _group_by_topic(ids: List[str], by_id: Dict[str, TelemetryEvent]) -> Dict[str, List[str]]:
    groups: Dict[str, List[str]] = {}
    for i in ids:
        if i in by_id:
            event = by_id[i]
            groups.setdefault(f"{event.source_ecu.value}/{event.signal_name.value}", []).append(i)
    return groups


def print_summary(result: DemoResult, out=print) -> None:
    def yes(flag: bool) -> str:
        return "yes" if flag else "NO"

    out("=" * 60)
    out(f"RESULT: {'PASS' if result.passed else 'FAIL'}")
    if not result.passed:
        out(f"  failed stage               : {result.failed_stage}")
        out(f"  reason                     : {result.failure_reason}")
    out(f"  gateway session_id         : {result.session_id}")
    out(f"  published before outage    : {result.published_before_outage}")
    out(f"  buffered during outage     : {result.buffered_during_outage} (max SQLite depth {result.max_buffer_depth})")
    out(f"  failed reconnect attempts  : {result.failed_reconnect_attempts}")
    out(f"  replayed / received        : {result.replayed} / {result.replayed_received}")
    out(f"  live after recovery (recv) : {result.live_received_after_recovery}")
    out(f"  final buffer depth         : {result.final_buffer_depth}")
    out(f"  FIFO replay verified       : {yes(result.fifo_replay_verified)}")
    out(f"  no live event overtook     : {yes(result.no_live_event_overtook_replay)}")
    out(f"  event_ids preserved        : {yes(result.event_ids_preserved)}")
    out(f"  recovery succeeded         : {yes(result.recovered and result.buffer_drained)}")
    out(f"  duplicates received        : {result.duplicates_received} (at-least-once: allowed, not expected here)")
    out(f"  metrics                    : {json.dumps(result.metrics)}")
    out("=" * 60)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Self-verifying MQTT outage -> buffering -> reconnect -> FIFO replay demo (local Mosquitto).",
    )
    parser.add_argument("--mqtt-host", default="localhost")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--vehicle-id", default=None, help="default: a unique RESILIENCE-DEMO-xxxxxxxx")
    parser.add_argument("--buffer-path", default=None, help="default: a temporary file, deleted afterwards (must not exist)")
    parser.add_argument("--outage-events", type=int, default=DemoConfig.outage_events,
                        help="how many events to buffer before clearing the outage (default: %(default)s)")
    parser.add_argument("--show-logs", action="store_true", help="also stream the gateway's JSON logs (stderr)")
    args = parser.parse_args(argv)

    if args.show_logs:
        configure_logging(verbose=False)
    # The final structured summary is always emitted as one JSON line.
    summary_handler = logging.StreamHandler(sys.stderr)
    summary_handler.setFormatter(JsonFormatter())
    previous_level, previous_propagate = logger.level, logger.propagate
    logger.addHandler(summary_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # exactly one copy of the summary line
    try:
        result = run_scenario(DemoConfig(
            mqtt_host=args.mqtt_host, mqtt_port=args.mqtt_port, vehicle_id=args.vehicle_id,
            buffer_path=args.buffer_path, outage_events=args.outage_events,
            show_gateway_logs=args.show_logs,
        ))
        print_summary(result)
        sys.stdout.flush()  # keep the human summary ahead of the JSON line when piped
        logger.info("resilience demo finished", extra=result.summary_fields())
    finally:
        # Leave logging as found, so calling main() in-process (e.g. from a
        # test) doesn't leak a handler or stack duplicate summary lines.
        logger.removeHandler(summary_handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
