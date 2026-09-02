"""
Thin wrapper around paho-mqtt's client, confirmed via smoke test to need
the newer `CallbackAPIVersion.VERSION2` (paho-mqtt 2.x deprecated the old
default callback signatures). Deliberately minimal for Phase 3: connect,
publish, disconnect. No retry, no reconnect-on-failure handling, no local
buffering on a failed publish -- those are Phase 4's job. A publish that
fails here is reported back to the caller (the gateway) as `False` and it
is the gateway's responsibility (for now: log and drop) to decide what
that means.
"""

from __future__ import annotations

import logging
from typing import Optional

import paho.mqtt.client as mqtt

DEFAULT_QOS = 1  # "at least once" -- matches this being telemetry, not a
                 # once-only command; Phase 4 revisits delivery guarantees
                 # once buffering/replay exist.
PUBLISH_CONFIRM_TIMEOUT_SECONDS = 2.0


class MqttPublisher:
    def __init__(
        self,
        host: str,
        port: int = 1883,
        client_id: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._logger = logger
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        self._connected = False

    def connect(self, keepalive: int = 30) -> None:
        self._client.connect(self._host, self._port, keepalive=keepalive)
        self._client.loop_start()  # background thread drives the network loop
        self._connected = True

    def publish(self, topic: str, payload: bytes, qos: int = DEFAULT_QOS) -> bool:
        """Returns True if the broker confirmed the publish within the
        timeout, False otherwise (broker unreachable, not connected, or
        confirmation timed out) -- never raises for a publish-time failure,
        since Phase 3's gateway loop must keep running past one bad send."""
        if not self._connected:
            if self._logger is not None:
                self._logger.error("publish attempted before connect()", extra={"topic": topic})
            return False
        try:
            info = self._client.publish(topic, payload, qos=qos)
            info.wait_for_publish(timeout=PUBLISH_CONFIRM_TIMEOUT_SECONDS)
            return info.is_published()
        except (OSError, RuntimeError, ValueError) as exc:
            if self._logger is not None:
                self._logger.error(
                    "publish failed: %s", exc, extra={"topic": topic},
                )
            return False

    def disconnect(self) -> None:
        if self._connected:
            self._client.loop_stop()
            self._client.disconnect()
            self._connected = False
