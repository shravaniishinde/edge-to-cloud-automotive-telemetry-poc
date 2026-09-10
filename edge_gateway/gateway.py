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
"""

from __future__ import annotations

import threading
import uuid
from typing import Optional

import can

from common.telemetry_schema import DEFAULT_VEHICLE_ID
from edge_gateway.buffer import TelemetryBuffer
from edge_gateway.ingestion import ingest_frame
from edge_gateway.logging_config import get_gateway_logger
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

        # Counters exist for this run's own summary log line. Persisted/
        # exported metrics are Phase 6 -- see ARCHITECTURE.md's phase table.
        self.processed_count = 0
        self.rejected_count = 0
        self.publish_failure_count = 0
        self.buffered_count = 0
        self.replayed_count = 0
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
            self.rejected_count += 1
            self._log_validate.warning(
                "rejected telemetry event", extra={
                    "event_id": event.event_id, "can_id": event.can_id, "reason": result.reason,
                },
            )
            return  # dropped, not forwarded -- see docs/edge-gateway-spec.md

        topic, payload = normalize(event)
        published = self._publisher.publish(topic, payload)
        if published:
            self.processed_count += 1
            self._log_publish.info(
                "published telemetry event", extra={
                    "event_id": event.event_id, "topic": topic,
                },
            )
        else:
            self.publish_failure_count += 1
            self._buffer.enqueue(event.event_id, topic, payload)
            self.buffered_count += 1
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
            for row in batch:
                if not self._publisher.is_connected():
                    break
                if self._publisher.publish(row.topic, row.payload):
                    confirmed_ids.append(row.id)
                    self.replayed_count += 1
                else:
                    break

            if confirmed_ids:
                self._buffer.remove(confirmed_ids)
                self._log_buffer.info(
                    "replayed buffered events", extra={
                        "replayed": len(confirmed_ids), "buffer_size": self._buffer.count(),
                    },
                )

            if len(confirmed_ids) < len(batch):
                return False # didn't clear the whole batch -- stop for now, try again next run_once()

    def run(self, stop_event: threading.Event) -> None:
        """Runs run_once() in a loop until stop_event is set."""
        if self._buffer.count() > 0:
            self._replay_requested = True

        while not stop_event.is_set():
            self.run_once()