"""
EdgeGateway: wires ingestion -> validation -> normalization -> publish into
one loop, with structured logging at each step.

session_id is minted here, once per EdgeGateway instance -- not by the
simulator. Raw CAN frames never carried a session concept (only an
arbitration ID and 8 data bytes exist on the wire), so session_id was
always simulator-side bookkeeping that never actually reached the wire.
Now that a real listener exists, the gateway is the natural place to own
the correlation ID for "one ingestion session" -- see
docs/edge-gateway-spec.md.

Phase 4 adds resilience around the one step that can fail for reasons
outside this process's control: publishing. A live publish failure no
longer means the event is lost -- it goes into `buffer` (a
`TelemetryBuffer`, see edge_gateway/buffer.py) instead, and is replayed,
in order, once the connection recovers. See `_replay_buffered()` below
and docs/edge-gateway-spec.md's "Resilience" section for the full design
and its one accepted limitation (an at-least-once, not exactly-once,
delivery guarantee).

Phase 7 moves the per-run counters into one `GatewayMetrics` object
(edge_gateway/metrics.py) owned by this instance -- same increment points,
same meanings -- and has `run()` log a structured "gateway stopped"
summary (a `MetricsSnapshot`, tagged with this run's session_id) when it
exits. The old `processed_count`/... attributes remain as read-only
properties backed by that object.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from typing import Optional

import can

from common.telemetry_schema import DEFAULT_VEHICLE_ID
from edge_gateway.buffer import TelemetryBuffer
from edge_gateway.ingestion import ingest_frame
from edge_gateway.logging_config import get_gateway_logger
from edge_gateway.metrics import GatewayMetrics, MetricsSnapshot
from edge_gateway.mqtt_publisher import MqttPublisher
from edge_gateway.normalization import normalize
from edge_gateway.validation import validate_event

BUS_RECV_TIMEOUT_SECONDS = 0.5

# How many buffered rows to publish per replay pass before re-checking the
# buffer's current size -- keeps one very large backlog from monopolizing
# run_once() calls indefinitely, without needing a separate thread.
REPLAY_BATCH_SIZE = 100


class EdgeGateway:
    def __init__(
        self,
        bus: can.BusABC,
        publisher: MqttPublisher,
        buffer: TelemetryBuffer,
        vehicle_id: str = DEFAULT_VEHICLE_ID,
        session_id: Optional[str] = None,
    ) -> None:
        self._bus = bus
        self._publisher = publisher
        self._buffer = buffer
        self._vehicle_id = vehicle_id
        self.session_id = session_id or str(uuid.uuid4())

        self._log_ingest = get_gateway_logger(self.session_id, "ingestion")
        self._log_validate = get_gateway_logger(self.session_id, "validation")
        self._log_publish = get_gateway_logger(self.session_id, "publish")
        self._log_buffer = get_gateway_logger(self.session_id, "buffer")
        self._log_metrics = get_gateway_logger(self.session_id, "metrics")

        # Phase 7: one metrics object for this gateway instance's lifetime
        # (i.e. one run), tagged with the same session_id as every log line.
        self.metrics = GatewayMetrics(session_id=self.session_id)

        # Replay is requested after MQTT recovery, or when the gateway
        # starts with a persisted backlog. A live publish failure alone
        # does not trigger immediate replay.
        self._replay_requested = False

    def run_once(self) -> None:
        """Receive and handle exactly one CAN frame, if one arrives within
        the receive timeout. A no-op on timeout (still checks for a
        reconnect/replay opportunity first), so the caller's loop can
        check its stop condition regularly and reconnect attempts aren't
        tied to CAN traffic continuing to arrive."""
        if not self._publisher.is_connected():
            if self._publisher.try_reconnect():
                self._replay_requested = True

        if self._replay_requested and self._publisher.is_connected():
            drained = self._replay_buffered()
            if drained:
                self._replay_requested = False        

        message = self._bus.recv(timeout=BUS_RECV_TIMEOUT_SECONDS)
        if message is None:
            return

        event = ingest_frame(
            message, session_id=self.session_id, vehicle_id=self._vehicle_id,
            logger=self._log_ingest,
        )
        if event is None:
            return  # not telemetry (e.g. a Phase 2 UDS frame), or malformed

        result = validate_event(event)
        if not result.is_valid:
            self.metrics.record_rejected()
            self._log_validate.warning(
                "rejected telemetry event", extra={
                    "event_id": event.event_id, "can_id": event.can_id, "reason": result.reason,
                },
            )
            return  # dropped, not forwarded -- see docs/edge-gateway-spec.md

        topic, payload = normalize(event)
        published = self._publisher.publish(topic, payload)
        if published:
            self.metrics.record_processed()
            self._log_publish.info(
                "published telemetry event", extra={
                    "event_id": event.event_id, "topic": topic,
                },
            )
        else:
            self.metrics.record_publish_failure()
            self._buffer.enqueue(event.event_id, topic, payload)
            self.metrics.record_buffered()
            self._log_buffer.warning(
                "publish failed -- buffered for replay", extra={
                    "event_id": event.event_id, "topic": topic, "buffer_size": self._buffer.count(),
                },
            )

    def _replay_buffered(self) -> bool:
        """Drains the buffer in strict FIFO order (`id ASC`), publishing
        each event through the exact same `MqttPublisher.publish()` path
        live telemetry uses -- one publish implementation, not two. A row
        is removed only after `publish()` returns True for it (see
        mqtt_publisher.py's docstring for exactly what that means: a
        broker-acknowledged QoS-1 publish, not merely "no exception was
        raised"). Stops at the first row that doesn't confirm -- whether
        because the connection dropped again mid-replay or the publish
        genuinely failed -- so nothing later in the buffer is ever sent
        out of order ahead of something earlier that's still unconfirmed.
        """
        while True:
            batch = self._buffer.peek_batch(limit=REPLAY_BATCH_SIZE)
            if not batch:
                return True

            confirmed_ids = []
            confirmed_event_ids = []  # Phase 7: per-event traceability for replays
            for row in batch:
                if not self._publisher.is_connected():
                    break
                if self._publisher.publish(row.topic, row.payload):
                    confirmed_ids.append(row.id)
                    confirmed_event_ids.append(row.event_id)
                    self.metrics.record_replayed()
                else:
                    break

            if confirmed_ids:
                self._buffer.remove(confirmed_ids)
                self._log_buffer.info(
                    "replayed buffered events", extra={
                        "replayed": len(confirmed_ids), "buffer_size": self._buffer.count(),
                        "event_ids": confirmed_event_ids,
                    },
                )

            if len(confirmed_ids) < len(batch):
                return False # didn't clear the whole batch -- stop for now, try again next run_once()

    def run(self, stop_event: threading.Event) -> None:
        """Runs run_once() in a loop until stop_event is set, then logs one
        structured "gateway stopped" line with this run's metrics snapshot
        (the log line already carries session_id via the logger adapter)."""
        if self._buffer.count() > 0:
            self._replay_requested = True

        try:
            while not stop_event.is_set():
                self.run_once()
        finally:
            # The summary must never mask an exception from the loop itself,
            # nor fail if a caller already closed the buffer after a join()
            # timeout (run_demo.py joins with a timeout shorter than one
            # worst-case iteration) -- report the pending depth as unknown.
            try:
                buffer_pending = self._buffer.count()
            except sqlite3.Error:
                buffer_pending = None
            self._log_metrics.info(
                "gateway stopped", extra={
                    **self.metrics_snapshot().as_log_fields(),
                    "buffer_pending": buffer_pending,
                },
            )

    def metrics_snapshot(self) -> MetricsSnapshot:
        """This run's counters plus the buffer's own (authoritative)
        dropped-event count, as one immutable, log-ready snapshot."""
        return self.metrics.snapshot(dropped=self._buffer.dropped_count)

    # --- Backward-compatible, read-only views of self.metrics (Phases 3-4
    # exposed these as plain attributes; tests and run_demo.py read them). ---

    @property
    def processed_count(self) -> int:
        return self.metrics.processed

    @property
    def rejected_count(self) -> int:
        return self.metrics.rejected

    @property
    def publish_failure_count(self) -> int:
        return self.metrics.publish_failures

    @property
    def buffered_count(self) -> int:
        return self.metrics.buffered

    @property
    def replayed_count(self) -> int:
        return self.metrics.replayed