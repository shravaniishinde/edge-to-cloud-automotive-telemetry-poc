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
"""

from __future__ import annotations

import threading
import uuid
from typing import Optional

import can

from common.telemetry_schema import DEFAULT_VEHICLE_ID
from edge_gateway.ingestion import ingest_frame
from edge_gateway.logging_config import get_gateway_logger
from edge_gateway.mqtt_publisher import MqttPublisher
from edge_gateway.normalization import normalize
from edge_gateway.validation import validate_event

BUS_RECV_TIMEOUT_SECONDS = 0.5


class EdgeGateway:
    def __init__(
        self,
        bus: can.BusABC,
        publisher: MqttPublisher,
        vehicle_id: str = DEFAULT_VEHICLE_ID,
        session_id: Optional[str] = None,
    ) -> None:
        self._bus = bus
        self._publisher = publisher
        self._vehicle_id = vehicle_id
        self.session_id = session_id or str(uuid.uuid4())

        self._log_ingest = get_gateway_logger(self.session_id, "ingestion")
        self._log_validate = get_gateway_logger(self.session_id, "validation")
        self._log_publish = get_gateway_logger(self.session_id, "publish")

        # Phase 3 scope: counters exist only for this run's summary log line.
        # Persisted/exported metrics (processed, failed, buffered, replayed)
        # are Phase 6 -- see ARCHITECTURE.md's phase table.
        self.processed_count = 0
        self.rejected_count = 0
        self.publish_failure_count = 0

    def run_once(self) -> None:
        """Receive and handle exactly one CAN frame, if one arrives within
        the receive timeout. A no-op (returns immediately) on timeout, so
        the caller's loop can check its stop condition regularly."""
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
            self._log_publish.error(
                "failed to publish telemetry event (dropped, no retry/buffer in Phase 3)",
                extra={"event_id": event.event_id, "topic": topic},
            )

    def run(self, stop_event: threading.Event) -> None:
        """Runs run_once() in a loop until stop_event is set."""
        while not stop_event.is_set():
            self.run_once()
