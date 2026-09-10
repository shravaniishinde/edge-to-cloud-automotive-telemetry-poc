"""
Connects to an already-running MQTT broker for the one integration test
module that needs one (test_gateway_integration.py), rather than mocking
MQTT entirely -- the whole point of those tests is proving the real
wire-level publish/subscribe path works, the same reasoning behind
Phase 2's real virtual-bus UDS integration test.

This fixture does NOT spawn its own Mosquitto process. It expects a
broker to already be listening -- normally the one started by
`docker compose -f docker/docker-compose.yml up`, this project's one
broker mechanism for local development, manual demo use, and CI alike
(see docs/edge-gateway-spec.md) -- and simply connects to it.

Defaults to `localhost:1883` (Mosquitto's standard port), overridable via
the `MQTT_BROKER_HOST` / `MQTT_BROKER_PORT` environment variables for
environments where the broker runs elsewhere. If nothing is reachable at
that host/port, every test that depends on this fixture is skipped
(not failed), with a message telling you to start the broker first.
"""

from __future__ import annotations

import os
import socket

import pytest


MQTT_BROKER_HOST = os.getenv("MQTT_BROKER_HOST", "localhost")
MQTT_BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))


def _port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def mosquitto_broker():
    """Use the MQTT broker provided by Docker Compose.

    The test suite connects to an already-running broker rather than
    starting a separate Mosquitto process. This keeps local development
    and CI aligned with the project's Docker-based deployment model.
    """
    if not _port_is_open(MQTT_BROKER_HOST, MQTT_BROKER_PORT):
        pytest.skip(
            f"MQTT broker unavailable at "
            f"{MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}. "
            "Start the broker with Docker Compose first."
        )

    yield MQTT_BROKER_PORT
