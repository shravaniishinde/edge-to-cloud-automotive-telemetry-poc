"""
Normalization: decide what a validated TelemetryEvent looks like once it
leaves the gateway -- the MQTT topic it's published to, and its wire
payload. Deliberately does not introduce a new schema: the payload is
just TelemetryEvent's own JSON serialization, so there's still exactly
one definition of "what a telemetry reading looks like" (see
common/telemetry_schema.py's module docstring).

Topic scheme: vehicle/{vehicle_id}/telemetry/{source_ecu}/{signal_name}
-- one topic per signal, the standard MQTT/IoT practice of using the
topic hierarchy itself to let subscribers filter by vehicle, ECU, or
specific signal without having to inspect every payload.
"""

from __future__ import annotations

from typing import Tuple

from common.telemetry_schema import TelemetryEvent


def build_topic(event: TelemetryEvent) -> str:
    return f"vehicle/{event.vehicle_id}/telemetry/{event.source_ecu.value}/{event.signal_name.value}"


def build_payload(event: TelemetryEvent) -> bytes:
    return event.model_dump_json().encode("utf-8")


def normalize(event: TelemetryEvent) -> Tuple[str, bytes]:
    """Returns (topic, payload) ready to hand to the MQTT publisher."""
    return build_topic(event), build_payload(event)
