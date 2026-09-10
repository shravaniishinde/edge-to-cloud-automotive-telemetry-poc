"""Unit tests for MqttPublisher that don't require a live broker -- the
real pub/sub round trip against an actual Mosquitto instance is covered
separately in test_gateway_integration.py. The Phase 4 reconnect/backoff
tests below drive `try_reconnect()` directly with a fake `now`, and
monkeypatch the underlying paho-mqtt client's `reconnect()` method, so
none of them need a real network connection or real elapsed time."""

import pytest

from edge_gateway.mqtt_publisher import (
    INITIAL_RECONNECT_BACKOFF_SECONDS,
    MAX_RECONNECT_BACKOFF_SECONDS,
    MqttPublisher,
)


def test_publish_before_connect_returns_false_not_raise():
    publisher = MqttPublisher(host="localhost", port=1883)
    result = publisher.publish("some/topic", b"payload")
    assert result is False


def test_disconnect_before_connect_is_a_safe_no_op():
    publisher = MqttPublisher(host="localhost", port=1883)
    publisher.disconnect()  # must not raise


def test_is_connected_reflects_connect_state():
    publisher = MqttPublisher(host="localhost", port=1883)
    assert publisher.is_connected() is False


def test_on_disconnect_callback_flips_connected_to_false():
    publisher = MqttPublisher(host="localhost", port=1883)
    publisher._connected = True  # simulate a prior successful connect()

    publisher._handle_disconnect(publisher._client, None, None, reason_code=None)

    assert publisher.is_connected() is False


def test_try_reconnect_returns_true_immediately_if_already_connected(monkeypatch):
    publisher = MqttPublisher(host="localhost", port=1883)
    publisher._connected = True

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("reconnect() should not be called when already connected")

    monkeypatch.setattr(publisher._client, "reconnect", _fail_if_called)

    assert publisher.try_reconnect(now=0.0) is True


def test_try_reconnect_does_nothing_before_the_backoff_window_elapses(monkeypatch):
    publisher = MqttPublisher(host="localhost", port=1883)
    publisher._connected = False
    publisher._next_reconnect_attempt_at = 100.0

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("reconnect() should not be attempted before the backoff window elapses")

    monkeypatch.setattr(publisher._client, "reconnect", _fail_if_called)

    assert publisher.try_reconnect(now=50.0) is False


def test_try_reconnect_success_resets_backoff_and_marks_connected(monkeypatch):
    publisher = MqttPublisher(host="localhost", port=1883)
    publisher._connected = False
    publisher._reconnect_backoff_seconds = 8.0  # pretend we'd already backed off a few times

    monkeypatch.setattr(publisher._client, "reconnect", lambda: None)

    result = publisher.try_reconnect(now=0.0)

    assert result is True
    assert publisher.is_connected() is True
    assert publisher._reconnect_backoff_seconds == INITIAL_RECONNECT_BACKOFF_SECONDS


def test_try_reconnect_failure_schedules_the_next_attempt_and_doubles_backoff(monkeypatch):
    publisher = MqttPublisher(host="localhost", port=1883)
    publisher._connected = False

    def _raise_connection_refused():
        raise OSError("connection refused")

    monkeypatch.setattr(publisher._client, "reconnect", _raise_connection_refused)

    result = publisher.try_reconnect(now=0.0)

    assert result is False
    assert publisher.is_connected() is False
    assert publisher._next_reconnect_attempt_at == pytest.approx(INITIAL_RECONNECT_BACKOFF_SECONDS)
    assert publisher._reconnect_backoff_seconds == pytest.approx(INITIAL_RECONNECT_BACKOFF_SECONDS * 2)


def test_try_reconnect_backoff_is_capped(monkeypatch):
    publisher = MqttPublisher(host="localhost", port=1883)
    publisher._connected = False

    def _raise_connection_refused():
        raise OSError("connection refused")

    monkeypatch.setattr(publisher._client, "reconnect", _raise_connection_refused)

    now = 0.0
    for _ in range(10):  # far more than enough doublings to hit the cap
        publisher.try_reconnect(now=now)
        now = publisher._next_reconnect_attempt_at

    assert publisher._reconnect_backoff_seconds == MAX_RECONNECT_BACKOFF_SECONDS


def test_inject_publish_failures_forces_publish_to_return_false():
    publisher = MqttPublisher(host="localhost", port=1883)
    publisher._connected = True

    publisher.inject_publish_failures(2)

    assert publisher.publish("some/topic", b"payload") is False
    assert publisher.publish("some/topic", b"payload") is False
    assert publisher._forced_failures_remaining == 0


def test_clear_injected_failures_stops_forcing_failures():
    publisher = MqttPublisher(host="localhost", port=1883)
    publisher.inject_publish_failures(5)

    publisher.clear_injected_failures()

    assert publisher._forced_failures_remaining == 0


# --- Phase 5: TLS construction (AWS IoT Core support) ---
# These only check that MqttPublisher wires TLS paths into paho-mqtt
# correctly -- they never touch a network, real or fake. The actual
# AWS IoT Core round trip is covered by the optional, skip-by-default
# test_aws_iot_integration.py.


def test_no_tls_args_leaves_tls_disabled():
    publisher = MqttPublisher(host="localhost", port=1883)

    assert publisher.is_tls_enabled() is False


def test_full_tls_args_call_tls_set_with_the_given_paths(monkeypatch, tmp_path):
    ca = tmp_path / "ca.pem"
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"

    captured = {}

    def _fake_tls_set(**kwargs):
        captured.update(kwargs)

    # MqttPublisher.__init__ calls tls_set() on the real paho-mqtt Client
    # it just constructed, before we get a chance to monkeypatch an
    # instance -- so patch the Client method on the class itself.
    monkeypatch.setattr("paho.mqtt.client.Client.tls_set", lambda self, **kwargs: _fake_tls_set(**kwargs))

    publisher = MqttPublisher(
        host="xxxx-ats.iot.us-east-1.amazonaws.com",
        port=8883,
        tls_ca_certs=str(ca),
        tls_certfile=str(cert),
        tls_keyfile=str(key),
    )

    assert publisher.is_tls_enabled() is True
    assert captured == {"ca_certs": str(ca), "certfile": str(cert), "keyfile": str(key)}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tls_ca_certs": "ca.pem"},
        {"tls_certfile": "cert.pem"},
        {"tls_keyfile": "key.pem"},
        {"tls_ca_certs": "ca.pem", "tls_certfile": "cert.pem"},
    ],
)
def test_partial_tls_args_raise_value_error(kwargs):
    with pytest.raises(ValueError):
        MqttPublisher(host="localhost", port=1883, **kwargs)
