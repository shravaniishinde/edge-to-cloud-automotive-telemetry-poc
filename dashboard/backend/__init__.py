"""
Phase 10: the Engineering Dashboard's backend -- a read/visualization
layer, deliberately separate from the telemetry pipeline.

Data sources (see ARCHITECTURE.md section 12):
- telemetry: a read-only MQTT subscription to `vehicle/+/telemetry/+/+`
  (the gateway's existing topics and `TelemetryEvent` payloads);
- gateway metrics/state: a gateway run *hosted in this process* using the
  existing components (the VirtualBus is process-local, so this is the
  only way to read the real `GatewayMetrics`), plus a logging tap on the
  gateway's existing structured log records;
- buffer depth: a read-only SQLite query on the gateway's buffer file;
- diagnostics: the existing UDS client/server + deterministic analyzer.

Standard library HTTP (no web framework dependency), Server-Sent Events
for live updates, bounded in-memory history only.
"""
