"""Unit tests for edge_gateway/cloud_publisher.py -- the local-vs-AWS
decision logic. None of these touch a network or a real broker; AWS
config validation is exercised with dummy cert files created in
tmp_path, and MqttPublisher's own TLS wiring is already covered
separately in test_mqtt_publisher.py."""

import pytest

from edge_gateway.cloud_publisher import (
    DEFAULT_AWS_IOT_PORT,
    AwsIotConfigError,
    build_publisher_from_env,
    load_aws_iot_config,
)


def _write_dummy_cert_files(tmp_path):
    ca = tmp_path / "ca.pem"
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    for path in (ca, cert, key):
        path.write_text("dummy -- content is never parsed by config loading")
    return ca, cert, key


# --- load_aws_iot_config() ---


def test_no_aws_env_vars_returns_none():
    assert load_aws_iot_config({}) is None


def test_unrelated_env_vars_return_none():
    assert load_aws_iot_config({"PATH": "/usr/bin", "MQTT_BROKER_HOST": "localhost"}) is None


def test_partial_config_raises_and_names_missing_vars(tmp_path):
    ca, cert, _key = _write_dummy_cert_files(tmp_path)
    env = {
        "AWS_IOT_ENDPOINT": "xxxx-ats.iot.us-east-1.amazonaws.com",
        "AWS_IOT_CA_PATH": str(ca),
        "AWS_IOT_CERT_PATH": str(cert),
        # AWS_IOT_KEY_PATH deliberately missing
    }

    with pytest.raises(AwsIotConfigError, match="AWS_IOT_KEY_PATH"):
        load_aws_iot_config(env)


def test_config_with_nonexistent_cert_file_raises(tmp_path):
    ca, cert, key = _write_dummy_cert_files(tmp_path)
    env = {
        "AWS_IOT_ENDPOINT": "xxxx-ats.iot.us-east-1.amazonaws.com",
        "AWS_IOT_CA_PATH": str(ca),
        "AWS_IOT_CERT_PATH": str(cert),
        "AWS_IOT_KEY_PATH": str(tmp_path / "does-not-exist.pem"),
    }

    with pytest.raises(AwsIotConfigError, match="does not exist"):
        load_aws_iot_config(env)


def test_complete_valid_config_is_loaded(tmp_path):
    ca, cert, key = _write_dummy_cert_files(tmp_path)
    env = {
        "AWS_IOT_ENDPOINT": "xxxx-ats.iot.us-east-1.amazonaws.com",
        "AWS_IOT_CA_PATH": str(ca),
        "AWS_IOT_CERT_PATH": str(cert),
        "AWS_IOT_KEY_PATH": str(key),
    }

    config = load_aws_iot_config(env)

    assert config is not None
    assert config.endpoint == "xxxx-ats.iot.us-east-1.amazonaws.com"
    assert config.port == DEFAULT_AWS_IOT_PORT
    assert config.ca_path == ca
    assert config.cert_path == cert
    assert config.key_path == key
    assert config.client_id is None


def test_port_and_client_id_overrides_are_honored(tmp_path):
    ca, cert, key = _write_dummy_cert_files(tmp_path)
    env = {
        "AWS_IOT_ENDPOINT": "xxxx-ats.iot.us-east-1.amazonaws.com",
        "AWS_IOT_CA_PATH": str(ca),
        "AWS_IOT_CERT_PATH": str(cert),
        "AWS_IOT_KEY_PATH": str(key),
        "AWS_IOT_PORT": "8884",
        "AWS_IOT_CLIENT_ID": "edge-gateway-01",
    }

    config = load_aws_iot_config(env)

    assert config.port == 8884
    assert config.client_id == "edge-gateway-01"


def test_invalid_port_value_raises(tmp_path):
    ca, cert, key = _write_dummy_cert_files(tmp_path)
    env = {
        "AWS_IOT_ENDPOINT": "xxxx-ats.iot.us-east-1.amazonaws.com",
        "AWS_IOT_CA_PATH": str(ca),
        "AWS_IOT_CERT_PATH": str(cert),
        "AWS_IOT_KEY_PATH": str(key),
        "AWS_IOT_PORT": "not-a-number",
    }

    with pytest.raises(AwsIotConfigError, match="AWS_IOT_PORT"):
        load_aws_iot_config(env)


# --- build_publisher_from_env() ---


def test_no_env_builds_a_local_publisher():
    publisher = build_publisher_from_env({}, local_host="localhost", local_port=1883)

    assert publisher._host == "localhost"
    assert publisher._port == 1883
    assert publisher.is_tls_enabled() is False


def test_complete_env_builds_an_aws_publisher(monkeypatch, tmp_path):
    # build_publisher_from_env() hands these paths straight to MqttPublisher,
    # which calls the real paho-mqtt tls_set() -- that parses the files as
    # real PEM content, which our dummy placeholder files aren't. That
    # parsing is exactly what test_mqtt_publisher.py's TLS tests already
    # cover; this test only cares about cloud_publisher's own local-vs-AWS
    # decision, so the actual tls_set() call is stubbed out here.
    monkeypatch.setattr("paho.mqtt.client.Client.tls_set", lambda self, **kwargs: None)

    ca, cert, key = _write_dummy_cert_files(tmp_path)
    env = {
        "AWS_IOT_ENDPOINT": "xxxx-ats.iot.us-east-1.amazonaws.com",
        "AWS_IOT_CA_PATH": str(ca),
        "AWS_IOT_CERT_PATH": str(cert),
        "AWS_IOT_KEY_PATH": str(key),
    }

    publisher = build_publisher_from_env(env, local_host="localhost", local_port=1883)

    assert publisher._host == "xxxx-ats.iot.us-east-1.amazonaws.com"
    assert publisher._port == DEFAULT_AWS_IOT_PORT
    assert publisher.is_tls_enabled() is True


def test_partial_env_raises_and_does_not_fall_back_to_local(tmp_path):
    ca, cert, _key = _write_dummy_cert_files(tmp_path)
    env = {
        "AWS_IOT_ENDPOINT": "xxxx-ats.iot.us-east-1.amazonaws.com",
        "AWS_IOT_CA_PATH": str(ca),
        "AWS_IOT_CERT_PATH": str(cert),
        # AWS_IOT_KEY_PATH deliberately missing
    }

    with pytest.raises(AwsIotConfigError):
        build_publisher_from_env(env, local_host="localhost", local_port=1883)
