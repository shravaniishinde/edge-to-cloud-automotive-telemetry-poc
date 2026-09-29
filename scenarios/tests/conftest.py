"""Reuses the Phase 8 broker fixtures (edge_gateway/tests/conftest.py) for
the resilience-demo tests, so they get the same skip-without-broker /
fail-under-MQTT_BROKER_REQUIRED=1 behaviour -- no second broker helper."""

from edge_gateway.tests.conftest import (  # noqa: F401  (re-exported fixtures)
    mosquitto_broker,
    mosquitto_host,
    mqtt_subscriber,
)
