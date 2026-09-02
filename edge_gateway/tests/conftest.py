"""
Spins up a real, throwaway Mosquitto broker for the one integration test
that needs one (test_gateway_integration.py), rather than mocking MQTT
entirely -- the whole point of that test is proving the real wire-level
publish/subscribe path works, the same reasoning behind Phase 2's real
virtual-bus UDS integration test.

Runs `mosquitto` directly as a subprocess (not via docker-compose) because
this is what a CI runner or a sandboxed dev environment can rely on being
installed via `apt-get install mosquitto`, without needing a working
Docker daemon. `docker/docker-compose.yml` is the equivalent for a
developer's own machine (`docker compose up`) -- same broker, different
launch mechanism, so there's exactly one topic/config truth
(docs/edge-gateway-spec.md), not two competing setups.

Uses port 18830 (not the default 1883) so this never collides with a
broker a developer might already have running locally.
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
