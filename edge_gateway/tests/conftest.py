"""
Connects to an already-running MQTT broker for the integration test
modules that need one (test_gateway_integration.py and
test_full_scenario_integration.py), rather than mocking MQTT entirely --
the whole point of those tests is proving the real wire-level
publish/subscribe path works, the same reasoning behind Phase 2's real
virtual-bus UDS integration test.

This fixture does NOT spawn its own Mosquitto process. It expects a
broker to already be listening -- normally the one started by
`docker compose -f docker/docker-compose.yml up -d mosquitto` for local
development, or the apt-installed `mosquitto` service in CI (see
.github/workflows/ci.yml) -- and simply connects to it.

Defaults to `localhost:1883` (Mosquitto's standard port), overridable via
the `MQTT_BROKER_HOST` / `MQTT_BROKER_PORT` environment variables for
environments where the broker runs elsewhere. If nothing is reachable at
that host/port, every test that depends on this fixture is skipped
(not failed), with a message telling you to start the broker first --
UNLESS `MQTT_BROKER_REQUIRED=1` is set (CI sets it), in which case a
missing broker is a test failure, so broker-backed tests can never be
silently skipped in CI.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from typing import Callable, List

import paho.mqtt.client as mqtt
import pytest


MQTT_BROKER_HOST = os.getenv("MQTT_BROKER_HOST", "localhost")
MQTT_BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))
MQTT_BROKER_REQUIRED = os.getenv("MQTT_BROKER_REQUIRED", "") == "1"

SUBSCRIBE_TIMEOUT_SECONDS = 5.0


def _port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def mosquitto_broker():
    """Yields the port of an already-running MQTT broker (see module
    docstring). Skips dependent tests if none is reachable, or fails them
    if `MQTT_BROKER_REQUIRED=1`."""
    if not _port_is_open(MQTT_BROKER_HOST, MQTT_BROKER_PORT):
        message = (
            f"MQTT broker unavailable at "
            f"{MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}. "
            "Start the broker with Docker Compose first."
        )
        if MQTT_BROKER_REQUIRED:
            pytest.fail(f"{message} (MQTT_BROKER_REQUIRED=1, so this is a failure, not a skip)")
        pytest.skip(message)

    yield MQTT_BROKER_PORT


@pytest.fixture(scope="session")
def mosquitto_host(mosquitto_broker):
    """The broker host matching `mosquitto_broker`'s port -- honours
    `MQTT_BROKER_HOST` the same way the reachability check does."""
    return MQTT_BROKER_HOST


class MqttTestSubscriber:
    """Small real MQTT subscriber used only by tests to observe what the
    gateway actually published. The constructor blocks until the broker
    has acknowledged the subscription (SUBACK), so nothing published after
    it returns can be missed -- no fixed sleep needed."""

    def __init__(self, host: str, port: int, topic: str) -> None:
        self._lock = threading.Lock()
        self._messages: List[mqtt.MQTTMessage] = []
        self._subscribed = threading.Event()
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = lambda c, u, f, rc, p: c.subscribe(topic, qos=1)
        self._client.on_subscribe = lambda c, u, mid, rcs, p: self._subscribed.set()
        self._client.on_message = self._on_message
        self._client.connect(host, port, keepalive=5)
        self._client.loop_start()
        if not self._subscribed.wait(SUBSCRIBE_TIMEOUT_SECONDS):
            self.close()
            raise TimeoutError(f"no SUBACK for {topic!r} within {SUBSCRIBE_TIMEOUT_SECONDS}s")

    def _on_message(self, client, userdata, msg) -> None:
        with self._lock:
            self._messages.append(msg)

    @property
    def messages(self) -> List[mqtt.MQTTMessage]:
        with self._lock:
            return list(self._messages)

    def wait_until(self, predicate: Callable[[List[mqtt.MQTTMessage]], bool], timeout: float = 5.0) -> bool:
        """Polls until `predicate(messages)` is true or `timeout` elapses.
        Returns the predicate's final result rather than raising, so the
        caller's own assertion produces the useful failure message."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self.messages):
                return True
            time.sleep(0.02)
        return predicate(self.messages)

    def wait_for_count(self, count: int, timeout: float = 5.0) -> bool:
        return self.wait_until(lambda msgs: len(msgs) >= count, timeout)

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


@pytest.fixture
def mqtt_subscriber(mosquitto_host, mosquitto_broker):
    """Factory fixture: `mqtt_subscriber(topic)` returns a connected,
    already-subscribed MqttTestSubscriber. Every subscriber created is
    closed at teardown even if the test fails part-way through."""
    created: List[MqttTestSubscriber] = []

    def _make(topic: str) -> MqttTestSubscriber:
        subscriber = MqttTestSubscriber(mosquitto_host, mosquitto_broker, topic)
        created.append(subscriber)
        return subscriber

    yield _make

    for subscriber in created:
        subscriber.close()
