"""
Phase 5: decides whether the Edge Gateway talks to local Mosquitto or AWS
IoT Core, and builds the right `MqttPublisher` for it -- this is the ONE
place that decision gets made. `gateway.py` is not touched at all by
Phase 5: it still just receives whatever `MqttPublisher` its caller (this
module, or a test) hands it and calls `publish()`/`is_connected()`/
`try_reconnect()` on it, exactly as it has since Phase 3.

Configuration is read from environment variables (never CLI flags or
hardcoded values), following this project's existing `.env`/`.env.example`
convention. Locked-in behavior (per Phase 5 approval):

- No AWS_IOT_* variables set at all -> build a local Mosquitto publisher.
  This is the default, and is exactly what every Phase 1-4 test and demo
  run already does -- nothing about that path changes.
- All required AWS_IOT_* variables set, and every certificate file they
  point to actually exists -> build an AWS IoT Core publisher (TLS,
  typically port 8883).
- SOME but not all required AWS_IOT_* variables set, or one points at a
  file that doesn't exist -> raise AwsIotConfigError immediately. This
  deliberately does NOT fall back to local Mosquitto -- a half-configured
  AWS setup silently talking to a developer's local broker instead would
  be a much more confusing failure mode than a clear startup error naming
  exactly what's missing.

This module never imports boto3 or any other AWS SDK. AWS IoT Core's
device endpoint speaks plain MQTT over TLS -- paho-mqtt (already a Phase 3
dependency) is sufficient, so no new dependency is added for this.

AWS credentials vs. the device certificate -- two separate things:
This module and the gateway process only ever handle the X.509 DEVICE
CERTIFICATE (the AWS_IOT_CERT_PATH/AWS_IOT_KEY_PATH/AWS_IOT_CA_PATH files
below), which is how AWS IoT Core authenticates one connecting device.
Separately, provisioning those AWS resources in the first place (creating
the Thing/certificate/policy/rule -- see infra/) uses ordinary AWS IAM
credentials, via Terraform, on whoever's machine runs `terraform apply`.
The gateway process never sees, needs, or stores an AWS access key -- see
docs/aws-setup.md for the full explanation of that boundary.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from edge_gateway.mqtt_publisher import MqttPublisher

DEFAULT_LOCAL_HOST = "localhost"
DEFAULT_LOCAL_PORT = 1883
DEFAULT_AWS_IOT_PORT = 8883  # AWS IoT Core's standard mutual-TLS MQTT port

# All four of these must be set together to select AWS IoT Core.
ENV_ENDPOINT = "AWS_IOT_ENDPOINT"
ENV_CA_PATH = "AWS_IOT_CA_PATH"
ENV_CERT_PATH = "AWS_IOT_CERT_PATH"
ENV_KEY_PATH = "AWS_IOT_KEY_PATH"
REQUIRED_AWS_ENV_VARS = (ENV_ENDPOINT, ENV_CA_PATH, ENV_CERT_PATH, ENV_KEY_PATH)

# Optional, with sensible defaults.
ENV_PORT = "AWS_IOT_PORT"
ENV_CLIENT_ID = "AWS_IOT_CLIENT_ID"


class AwsIotConfigError(ValueError):
    """Raised when AWS_IOT_* environment variables are present but
    incomplete or invalid -- e.g. only some of the required variables are
    set, or a cert file path doesn't exist. Deliberately a subclass of
    ValueError (not caught anywhere in this project) so it propagates all
    the way up and stops startup with a clear message, per the "fail
    fast, never silently fall back to local Mosquitto" decision."""


@dataclass(frozen=True)
class AwsIotConfig:
    """A fully validated set of AWS IoT Core connection settings. If you
    have one of these, every field has already been checked to be
    present and every path has already been checked to exist."""

    endpoint: str
    port: int
    ca_path: Path
    cert_path: Path
    key_path: Path
    client_id: Optional[str]


def load_aws_iot_config(env: Mapping[str, str]) -> Optional[AwsIotConfig]:
    """Reads and validates AWS_IOT_* settings from `env`.

    Returns None if NONE of the required variables are set (the "use
    local Mosquitto" case). Raises AwsIotConfigError if SOME but not all
    are set, or if a configured certificate file path doesn't exist.
    Never returns a partially-filled config -- callers can treat a
    non-None result as ready to use.
    """
    present = [name for name in REQUIRED_AWS_ENV_VARS if env.get(name)]
    if not present:
        return None

    missing = [name for name in REQUIRED_AWS_ENV_VARS if not env.get(name)]
    if missing:
        raise AwsIotConfigError(
            "Partial AWS IoT Core configuration detected -- "
            f"set: {', '.join(present)}; missing: {', '.join(missing)}. "
            f"Set all of {', '.join(REQUIRED_AWS_ENV_VARS)} to connect to "
            "AWS IoT Core, or none of them to use local Mosquitto instead. "
            "See docs/aws-setup.md."
        )

    ca_path = Path(env[ENV_CA_PATH])
    cert_path = Path(env[ENV_CERT_PATH])
    key_path = Path(env[ENV_KEY_PATH])
    for var_name, path in ((ENV_CA_PATH, ca_path), (ENV_CERT_PATH, cert_path), (ENV_KEY_PATH, key_path)):
        if not path.is_file():
            raise AwsIotConfigError(
                f"{var_name} is set to '{path}', but that file does not exist. "
                "AWS IoT Core configuration must point at real certificate "
                "files -- see docs/aws-setup.md."
            )

    try:
        port = int(env.get(ENV_PORT, DEFAULT_AWS_IOT_PORT))
    except ValueError as exc:
        raise AwsIotConfigError(
            f"{ENV_PORT} is set to '{env[ENV_PORT]}', which is not a valid port number."
        ) from exc

    return AwsIotConfig(
        endpoint=env[ENV_ENDPOINT],
        port=port,
        ca_path=ca_path,
        cert_path=cert_path,
        key_path=key_path,
        client_id=env.get(ENV_CLIENT_ID) or None,
    )


def build_publisher_from_env(
    env: Optional[Mapping[str, str]] = None,
    *,
    local_host: str = DEFAULT_LOCAL_HOST,
    local_port: int = DEFAULT_LOCAL_PORT,
    logger: Optional[logging.Logger] = None,
) -> MqttPublisher:
    """The one function `run_demo.py` (or any future entry point) should
    call to get a ready-to-`connect()` MqttPublisher. `local_host`/
    `local_port` are only used when no AWS configuration is present at
    all -- they're what today's `--mqtt-host`/`--mqtt-port` CLI flags
    become when AWS isn't configured.

    Raises AwsIotConfigError (uncaught, by design) if AWS configuration
    is present but incomplete/invalid -- this function never guesses or
    falls back in that case.
    """
    env = os.environ if env is None else env
    aws_config = load_aws_iot_config(env)  # raises on partial config; propagates

    if aws_config is None:
        if logger is not None:
            logger.info(
                "no AWS IoT Core configuration found -- using local Mosquitto",
                extra={"mqtt_host": local_host, "mqtt_port": local_port},
            )
        return MqttPublisher(host=local_host, port=local_port, logger=logger)

    if logger is not None:
        logger.info(
            "AWS IoT Core configuration found -- connecting to AWS IoT Core",
            extra={"aws_iot_endpoint": aws_config.endpoint, "aws_iot_port": aws_config.port},
        )
    return MqttPublisher(
        host=aws_config.endpoint,
        port=aws_config.port,
        client_id=aws_config.client_id,
        logger=logger,
        tls_ca_certs=str(aws_config.ca_path),
        tls_certfile=str(aws_config.cert_path),
        tls_keyfile=str(aws_config.key_path),
    )
