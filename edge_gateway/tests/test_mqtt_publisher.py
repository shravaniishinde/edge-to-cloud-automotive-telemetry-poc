"""Unit tests for MqttPublisher that don't require a live broker -- the
real pub/sub round trip against an actual Mosquitto instance is covered
separately in test_gateway_integration.py."""

from edge_gateway.mqtt_publisher import MqttPublisher


def test_publish_before_connect_returns_false_not_raise():
    publisher = MqttPublisher(host="localhost", port=1883)
    result = publisher.publish("some/topic", b"payload")
    assert result is False


def test_disconnect_before_connect_is_a_safe_no_op():
    publisher = MqttPublisher(host="localhost", port=1883)
    publisher.disconnect()  # must not raise
