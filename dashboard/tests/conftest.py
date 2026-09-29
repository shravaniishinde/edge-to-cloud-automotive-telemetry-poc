"""Reuses the Phase 8 broker fixtures (edge_gateway/tests/conftest.py), so
the dashboard's broker-backed tests skip without a broker and fail under
MQTT_BROKER_REQUIRED=1 -- same policy as every other broker test."""

from edge_gateway.tests.conftest import (  # noqa: F401  (re-exported fixtures)
    mosquitto_broker,
    mosquitto_host,
    mqtt_subscriber,
)
