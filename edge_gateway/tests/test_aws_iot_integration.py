"""Optional, real-AWS-IoT-Core smoke test -- the AWS equivalent of
test_gateway_integration.py's real-Mosquitto tests, and skipped the same
way (see conftest.py's mosquitto_broker fixture) when what it needs isn't
available. This test:

- Never runs in CI (no AWS credentials/certificates exist there).
- Never runs locally unless you've actually provisioned AWS IoT Core
  (see infra/ and docs/aws-setup.md) and set the same AWS_IOT_*
  environment variables run_demo.py uses.
- Costs nothing to leave in the suite: pytest.skip() is nearly instant,
  so `pytest` with no AWS config configured still runs this file in a
  fraction of a second.

What it proves, when it does run: the exact same MqttPublisher class
used for local Mosquitto can open a real mutual-TLS connection to a real
AWS IoT Core endpoint and get a broker-acknowledged (QoS 1) publish --
i.e. the whole Phase 5 TLS/config story actually works end to end, not
just in mocked unit tests.
"""

from __future__ import annotations

import json

import pytest

from edge_gateway.cloud_publisher import AwsIotConfigError, load_aws_iot_config
from edge_gateway.mqtt_publisher import MqttPublisher

try:
    import os

    _aws_config = load_aws_iot_config(os.environ)
except AwsIotConfigError as exc:
    _aws_config = None
    _skip_reason = f"AWS IoT configuration is present but invalid: {exc}"
else:
    _skip_reason = (
        "No AWS IoT Core configuration found (AWS_IOT_ENDPOINT/AWS_IOT_CA_PATH/"
        "AWS_IOT_CERT_PATH/AWS_IOT_KEY_PATH). This test only runs against a real, "
        "already-provisioned AWS IoT Core endpoint -- see docs/aws-setup.md."
    )


@pytest.mark.skipif(_aws_config is None, reason=_skip_reason)
def test_publish_to_real_aws_iot_core_is_acknowledged():
    publisher = MqttPublisher(
        host=_aws_config.endpoint,
        port=_aws_config.port,
        client_id=_aws_config.client_id,
        tls_ca_certs=str(_aws_config.ca_path),
        tls_certfile=str(_aws_config.cert_path),
        tls_keyfile=str(_aws_config.key_path),
    )
    try:
        publisher.connect()
        payload = json.dumps({"smoke_test": True}).encode("utf-8")

        result = publisher.publish("vehicle/SIM-VEHICLE-01/telemetry/powertrain/vehicle_speed_kph", payload)

        assert result is True
    finally:
        publisher.disconnect()
