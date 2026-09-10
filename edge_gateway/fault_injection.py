"""
Phase 4: deterministic, explicit fault-injection helpers -- not a random
chaos-monkey framework, on purpose. A POC's fault injection should be a
small set of named, on-demand scenarios that are easy to trigger, easy to
test deterministically (no timing flakiness), and easy to explain in an
interview. Four scenarios are covered here:

- MQTT/cloud outage: `force_publish_failures()`, a context manager that
  makes MqttPublisher.publish() report failure without touching the
  network or Docker -- useful for fast unit/integration tests. The real
  demo instead uses a genuine `docker compose stop mosquitto` (see
  run_demo.py / README.md), since Docker Compose is already this
  project's one broker mechanism -- no new tooling needed to fake it.
- Recovery after outage: simply the context manager exiting (or the
  broker actually restarting) -- EdgeGateway's own reconnect/replay logic
  (mqtt_publisher.py's try_reconnect(), gateway.py's _replay_buffered())
  handles the rest; there is nothing extra to "inject" for recovery.
- Malformed telemetry: `malformed_frame()` -- reuses ingestion.py's
  existing wrong-payload-length handling (already proven by
  test_ingestion.py); this is a convenience builder, not new gateway
  logic.
- Out-of-range telemetry: `out_of_range_frame()` -- reuses
  validation.py's existing range check (already proven by
  test_validation.py / test_gateway_integration.py); again just a
  convenience builder for demos and tests.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import can

from common.can_signal_map import encode_signal, get_signal_definition
from edge_gateway.mqtt_publisher import MqttPublisher


@contextmanager
def force_publish_failures(publisher: MqttPublisher, count: int) -> Iterator[None]:
    """Simulates an MQTT/cloud outage: the next `count` calls to
    `publisher.publish()` report failure, so whatever code is exercising
    the gateway can assert on the buffering behavior without needing a
    real broker outage. Restores normal behavior on exit even if the
    `with` block raises."""
    publisher.inject_publish_failures(count)
    try:
        yield
    finally:
        publisher.clear_injected_failures()


def malformed_frame(can_id: int = 0x100) -> can.Message:
    """A known telemetry CAN ID with a payload of the wrong length --
    exercises ingestion.py's malformed-payload drop path (see
    test_ingestion.py::test_ingest_drops_malformed_payload_for_known_can_id
    for the same construction). `can_id` must be a real registered
    signal; only its payload is deliberately wrong."""
    get_signal_definition(can_id)  # raises UnknownCanIdError early if can_id isn't real
    return can.Message(arbitration_id=can_id, data=bytes(2), is_extended_id=False)


def out_of_range_frame(can_id: int = 0x200, value: float = 250.0) -> can.Message:
    """A structurally valid frame whose *decoded* value falls outside
    that signal's valid_range -- exercises validation.py's rejection
    path (see test_validation.py and
    test_gateway_integration.py::test_out_of_range_frame_is_rejected_and_never_published
    for the same construction, e.g. battery_soc_pct=250.0 against its
    (0.0, 100.0) valid range). The caller is responsible for choosing a
    `can_id`/`value` pair that's actually out of range for that signal --
    this only builds the frame, it doesn't check that for you."""
    return can.Message(arbitration_id=can_id, data=encode_signal(can_id, value), is_extended_id=False)
