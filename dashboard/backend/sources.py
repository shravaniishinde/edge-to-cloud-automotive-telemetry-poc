"""
Read-only adapters that feed DashboardState. None of them publishes,
writes, or changes anything in the telemetry pipeline:

- TelemetrySubscriber: an MQTT *subscriber* on the gateway's existing
  telemetry topics. Tolerates the broker being down at startup or later
  (paho retries in the background); its own connection state is reported
  separately from the gateway's.
- LogTap: a logging.Handler on the existing "edge_gateway" logger -- the
  same structured records the gateway already emits -- reduced to plain,
  allowlisted fields. It sees only gateways running in this process.
- read_buffer_depth(): `SELECT COUNT(*)` on the gateway's SQLite buffer
  file through a separate read-only connection, so it never touches the
  gateway's own connection or holds a write lock.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional, Union

import paho.mqtt.client as mqtt

from dashboard.backend.state import DashboardState

TELEMETRY_TOPIC_FILTER = "vehicle/+/telemetry/+/+"

_LOG_RECORD_FIELDS = (
    "session_id", "event_id", "event_ids", "component", "topic", "can_id", "reason", "buffer_size",
    "replayed", "reason_code", "processed", "rejected", "publish_failures", "buffered", "dropped",
    "buffer_pending",
)


class TelemetrySubscriber:
    def __init__(self, state: DashboardState, host: str, port: int) -> None:
        self._state = state
        self.host, self.port = host, port
        self._lock = threading.Lock()
        self._status = "not started"
        self._last_error: Optional[str] = None
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=None)
        self._client.reconnect_delay_set(min_delay=1, max_delay=10)
        self._client.on_connect = self._on_connect
        self._client.on_connect_fail = self._on_connect_fail
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    def start(self) -> None:
        self._set_status("connecting")
        try:
            self._client.connect_async(self.host, self.port, keepalive=15)
        except (OSError, ValueError) as exc:  # e.g. an unresolvable host
            self._set_status("disconnected", str(exc))
            return
        self._client.loop_start()  # retries in the background until the broker is reachable

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()
        self._set_status("stopped")

    def status(self) -> dict:
        with self._lock:
            return {"host": self.host, "port": self.port, "topic_filter": TELEMETRY_TOPIC_FILTER,
                    "state": self._status, "last_error": self._last_error}

    def _set_status(self, status: str, error: Optional[str] = None) -> None:
        with self._lock:
            changed = status != self._status
            self._status = status
            if error is not None:
                self._last_error = error
        if changed and status in ("connected", "disconnected"):
            self._state.add_activity("dashboard", "INFO" if status == "connected" else "WARNING",
                                     f"dashboard MQTT subscription {status}",
                                     {"broker": f"{self.host}:{self.port}", **({"error": error} if error else {})})

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code.is_failure:
            self._set_status("disconnected", f"broker refused connection: {reason_code}")
            return
        client.subscribe(TELEMETRY_TOPIC_FILTER, qos=1)
        self._set_status("connected")

    def _on_connect_fail(self, client, userdata) -> None:
        self._set_status("disconnected", "broker unreachable; retrying")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if self._status != "stopped":
            self._set_status("disconnected", f"disconnected: {reason_code}")

    def _on_message(self, client, userdata, message) -> None:
        # Never let a bad payload escape into paho's network thread.
        try:
            self._state.ingest_telemetry(message.topic, bytes(message.payload))
        except Exception as exc:  # pragma: no cover -- ingest_telemetry already never raises
            self._state.add_activity("dashboard", "ERROR", f"telemetry handling error: {type(exc).__name__}")


class LogTap(logging.Handler):
    """Copies the gateway's own structured log records into the dashboard."""

    def __init__(self, state: DashboardState) -> None:
        super().__init__(level=logging.INFO)
        self._state = state
        self._attached_to: Optional[logging.Logger] = None
        self._previous_level: Optional[int] = None

    def attach(self, logger_name: str = "edge_gateway") -> None:
        logger = logging.getLogger(logger_name)
        self._previous_level = logger.level
        if logger.getEffectiveLevel() > logging.INFO:
            logger.setLevel(logging.INFO)
        logger.addHandler(self)
        self._attached_to = logger

    def detach(self) -> None:
        if self._attached_to is not None:
            self._attached_to.removeHandler(self)
            self._attached_to.setLevel(self._previous_level)
            self._attached_to = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            reduced = {"message": record.getMessage(), "level": record.levelname, "logger": record.name}
            for key in _LOG_RECORD_FIELDS:
                value = getattr(record, key, None)
                if value is not None:
                    reduced[key] = list(value) if key == "event_ids" else value
            self._state.ingest_log_record(reduced)
        except Exception:  # a dashboard problem must never break the gateway's logging
            self.handleError(record)


def read_buffer_depth(db_path: Optional[Union[str, Path]]) -> Optional[int]:
    """Current number of rows waiting in a TelemetryBuffer SQLite file, or
    None if there is no such file (yet) or it can't be read."""
    if db_path is None:
        return None
    path = Path(db_path)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.5)
        try:
            (count,) = conn.execute("SELECT COUNT(*) FROM pending_telemetry").fetchone()
            return int(count)
        finally:
            conn.close()
    except sqlite3.Error:
        return None
