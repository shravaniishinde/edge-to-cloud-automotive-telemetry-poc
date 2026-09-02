"""
End-to-end EdgeGateway test: a real virtual CAN frame goes in, a real
Mosquitto broker (via the `mosquitto_broker` fixture) is on the other end,
and a real MQTT subscriber confirms what actually arrived -- proving the
whole ingest -> validate -> normalize -> publish pipeline works together,
not just each step's logic in isolation (that's what test_ingestion.py /
test_validation.py / test_normalization.py already cover).
"""

import json
import time

import can
import paho.mqtt.client as mqtt
import pytest

from common.can_signal_map import encode_signal
from edge_gateway.gateway import EdgeGateway
from edge_gateway.mqtt_publisher import MqttPublisher
from simulation.can_bus import get_bus


class _Subscriber:
    """Small real MQTT subscriber used only by these tests to observe what
    the gateway actually published."""

    def __init__(self, port: int, topic: str) -> None:
        self.messages = []
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = lambda c, u, f, rc, p: c.subscribe(topic)
        self._client.on_message = lambda c, u, msg: self.messages.append(msg)
        self._client.connect("localhost", port, keepalive=5)
        self._client.loop_start()
        time.sleep(0.3)  # let the subscribe complete before the publisher sends anything

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


@pytest.fixture
def gateway(mosquitto_broker):
    bus = get_bus()
    publisher = MqttPublisher(host="localhost", port=mosquitto_broker)
    publisher.connect()
    gw = EdgeGateway(bus, publisher, session_id="integration-test-session")

    yield gw

    publisher.disconnect()
    bus.shutdown()


def test_valid_telemetry_frame_is_published_over_real_mqtt(gateway, mosquitto_broker):
    subscriber = _Subscriber(mosquitto_broker, "vehicle/+/telemetry/+/+")

    sender_bus = get_bus()
    frame = can.Message(arbitration_id=0x100, data=encode_signal(0x100, 62.3), is_extended_id=False)
    sender_bus.send(frame)

    gateway.run_once()  # processes exactly the frame just sent
    time.sleep(0.3)      # let the subscriber's callback fire

    subscriber.close()
    sender_bus.shutdown()

    assert len(subscriber.messages) == 1
    msg = subscriber.messages[0]
    assert msg.topic == "vehicle/SIM-VEHICLE-01/telemetry/powertrain/vehicle_speed_kph"
    payload = json.loads(msg.payload)
    assert payload["value"] == pytest.approx(62.3, abs=0.1)
    assert gateway.processed_count == 1
    assert gateway.rejected_count == 0


def test_out_of_range_frame_is_rejected_and_never_published(gateway, mosquitto_broker):
    subscriber = _Subscriber(mosquitto_broker, "vehicle/+/telemetry/+/+")

    sender_bus = get_bus()
    # battery_soc_pct's valid_range is (0.0, 100.0), but encode_signal only
    # enforces that the raw value fits its data type (uint16 here) -- it
    # happily encodes 250% (a corrupted/faulty sensor reading), exactly the
    # "structurally valid but physically wrong" case Phase 1 left for the
    # gateway to catch.
    frame = can.Message(arbitration_id=0x200, data=encode_signal(0x200, 250.0), is_extended_id=False)
    sender_bus.send(frame)

    gateway.run_once()
    time.sleep(0.3)

    subscriber.close()
    sender_bus.shutdown()

    assert len(subscriber.messages) == 0  # never forwarded
    assert gateway.rejected_count == 1
    assert gateway.processed_count == 0


def test_uds_frame_sharing_the_bus_is_ignored_by_the_gateway(gateway, mosquitto_broker):
    subscriber = _Subscriber(mosquitto_broker, "vehicle/+/telemetry/+/+")

    sender_bus = get_bus()
    uds_frame = can.Message(arbitration_id=0x7E0, data=bytes(8), is_extended_id=False)
    sender_bus.send(uds_frame)

    gateway.run_once()
    time.sleep(0.3)

    subscriber.close()
    sender_bus.shutdown()

    assert len(subscriber.messages) == 0
    assert gateway.processed_count == 0
    assert gateway.rejected_count == 0
