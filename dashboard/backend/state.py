"""
The dashboard's in-memory view of the system: bounded, thread-safe, and
free of I/O, so every rule here is unit-testable.

Everything stored is derived from real inputs only -- MQTT telemetry
payloads (validated against the existing `TelemetryEvent` schema), the
gateway's own structured log records, and diagnostic results. Nothing is
invented: a value the dashboard cannot derive is reported as unknown
(`None`), never guessed.

Retention limits (all bounded; oldest dropped first):
- MAX_RECENT_EVENTS   recent telemetry events for the event stream
- MAX_ACTIVITY        activity/log entries (identical consecutive messages
                      are coalesced into one entry with a count)
- MAX_VEHICLES        vehicles tracked (least recently seen evicted)
- MAX_SERIES_POINTS   chart points per (vehicle, signal), sampled at most
                      every SERIES_SAMPLE_SECONDS of *event* time
- MAX_SEEN_EVENT_IDS  event_ids remembered for duplicate detection
- MAX_REPLAYED_IDS    event_ids the hosted gateway reported replaying
- MAX_DIAGNOSTIC_*    diagnostic events / anomaly reports from the last run
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from pydantic import ValidationError

from common.can_signal_map import SIGNAL_REGISTRY
from common.telemetry_schema import TelemetryEvent
from edge_gateway.normalization import build_topic

MAX_RECENT_EVENTS = 200
MAX_ACTIVITY = 200
MAX_VEHICLES = 20
MAX_SERIES_POINTS = 120
SERIES_SAMPLE_SECONDS = 0.5
MAX_SEEN_EVENT_IDS = 5000
MAX_REPLAYED_IDS = 5000
MAX_DIAGNOSTIC_EVENTS = 100
MAX_DIAGNOSTIC_ANOMALIES = 50
MAX_PAYLOAD_BYTES = 16 * 1024

FLOW_FRESH_SECONDS = 3.0          # telemetry "flowing" if something arrived this recently
RATE_WINDOW_SECONDS = 10.0        # receive-rate window
LATE_EVENT_SECONDS = 2.0          # received this long after its own timestamp -> "late"
RECENT_REPLAY_SECONDS = 3.0
COALESCE_LOOKBACK = 4             # activity entries checked for a repeat of the same message

# Gateway log messages too frequent to be useful in the activity feed
# (they are already visible, per event, in the event stream).
_ROUTINE_MESSAGES = frozenset({"published telemetry event", "ingested frame", "ignoring non-telemetry frame"})

# Only these log-record fields are copied into the activity feed -- an
# allowlist, so nothing unexpected (e.g. an environment value someone
# logged) can ever reach the browser.
_ACTIVITY_FIELDS = (
    "session_id", "event_id", "component", "topic", "can_id", "reason", "buffer_size",
    "replayed", "reason_code", "processed", "rejected", "publish_failures", "buffered",
    "dropped", "buffer_pending",
)
_SUMMARY_FIELDS = ("session_id", "processed", "rejected", "publish_failures", "buffered", "replayed",
                   "dropped", "buffer_pending")

KNOWN_SIGNALS = {d.signal_name.value: {"ecu": d.ecu.value, "unit": d.unit, "valid_range": list(d.valid_range)}
                 for d in SIGNAL_REGISTRY.values()}


def _iso(ts: Optional[float]) -> Optional[str]:
    return None if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@dataclass
class _ReceivedEvent:
    event: TelemetryEvent
    topic: str
    received_at: float


class DashboardState:
    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._started_at = clock()
        self._recent: Deque[_ReceivedEvent] = deque(maxlen=MAX_RECENT_EVENTS)
        self._receive_times: Deque[float] = deque(maxlen=2000)
        self._seen_ids: "OrderedDict[str, None]" = OrderedDict()
        self._replayed_ids: "OrderedDict[str, None]" = OrderedDict()
        self._vehicles: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._sessions_seen: "OrderedDict[str, float]" = OrderedDict()
        self._activity: Deque[Dict[str, Any]] = deque(maxlen=MAX_ACTIVITY)
        self._total_received = 0
        self._malformed = 0
        self._duplicates = 0
        self._last_received_at: Optional[float] = None
        self._last_event_timestamp: Optional[str] = None
        self._last_replay: Optional[Dict[str, Any]] = None
        self._last_stopped_summary: Optional[Dict[str, Any]] = None
        self._last_reconnect_at: Optional[float] = None
        self._diagnostics: Dict[str, Any] = {"status": "not run", "events": [], "anomalies": []}

    # ------------------------------------------------------------------ telemetry

    def ingest_telemetry(self, topic: str, payload: bytes) -> bool:
        """Validates one MQTT message against the existing TelemetryEvent
        schema and topic scheme. Returns False (and counts it as
        malformed, never raises) for anything that doesn't fit."""
        now = self._clock()
        reason = None
        event = None
        if len(payload) > MAX_PAYLOAD_BYTES:
            reason = f"payload larger than {MAX_PAYLOAD_BYTES} bytes"
        else:
            try:
                event = TelemetryEvent.model_validate_json(payload)
            except (ValidationError, ValueError) as exc:
                reason = f"not a valid TelemetryEvent ({type(exc).__name__})"
        if event is not None:
            if not math.isfinite(event.value):
                reason = "non-finite value"
            elif build_topic(event) != topic:
                reason = "payload does not match its topic"
        if reason is not None:
            with self._lock:
                self._malformed += 1
            self.add_activity("dashboard", "WARNING", f"ignored malformed telemetry: {reason}", {"topic": topic[:200]})
            return False

        with self._lock:
            self._total_received += 1
            self._receive_times.append(now)
            self._last_received_at = now
            self._last_event_timestamp = event.timestamp.isoformat()
            if event.event_id in self._seen_ids:
                self._duplicates += 1
            self._remember(self._seen_ids, event.event_id, MAX_SEEN_EVENT_IDS)
            self._sessions_seen[event.session_id] = now
            self._sessions_seen.move_to_end(event.session_id)
            while len(self._sessions_seen) > 10:
                self._sessions_seen.popitem(last=False)
            self._recent.append(_ReceivedEvent(event, topic, now))
            self._update_vehicle(event, now)
        return True

    @staticmethod
    def _remember(store: "OrderedDict[str, None]", key: str, limit: int) -> None:
        store[key] = None
        store.move_to_end(key)
        while len(store) > limit:
            store.popitem(last=False)

    def _update_vehicle(self, event: TelemetryEvent, now: float) -> None:
        vehicle = self._vehicles.get(event.vehicle_id)
        if vehicle is None:
            vehicle = {"vehicle_id": event.vehicle_id, "event_count": 0, "ecus": {}, "series": {}}
            self._vehicles[event.vehicle_id] = vehicle
        self._vehicles.move_to_end(event.vehicle_id)
        while len(self._vehicles) > MAX_VEHICLES:
            self._vehicles.popitem(last=False)

        vehicle["event_count"] += 1
        vehicle["last_seen"] = now
        vehicle["last_session_id"] = event.session_id
        ecu = vehicle["ecus"].setdefault(event.source_ecu.value, {"event_count": 0, "signals": {}})
        ecu["event_count"] += 1
        ecu["last_seen"] = now
        ecu["signals"][event.signal_name.value] = {
            "value": event.value, "unit": event.unit, "timestamp": event.timestamp.isoformat(),
            "event_id": event.event_id,
        }
        series = vehicle["series"].setdefault(event.signal_name.value, deque(maxlen=MAX_SERIES_POINTS))
        t = event.timestamp.timestamp()
        if not series or t - series[-1][0] >= SERIES_SAMPLE_SECONDS:
            series.append((t, event.value))

    # ------------------------------------------------------------------ gateway log tap

    def ingest_log_record(self, record: Dict[str, Any]) -> None:
        """One structured gateway log record (already reduced to plain
        fields by LogTap). Updates replay/recovery bookkeeping and the
        activity feed."""
        message = record.get("message", "")
        now = self._clock()
        with self._lock:
            if message == "replayed buffered events":
                for event_id in record.get("event_ids") or []:
                    self._remember(self._replayed_ids, str(event_id), MAX_REPLAYED_IDS)
                self._last_replay = {
                    "at": _iso(now), "at_epoch": now, "replayed": record.get("replayed"),
                    "buffer_size": record.get("buffer_size"), "session_id": record.get("session_id"),
                }
            elif message == "gateway stopped":
                self._last_stopped_summary = {k: record.get(k) for k in _SUMMARY_FIELDS}
                self._last_stopped_summary["at"] = _iso(now)
            elif message == "MQTT reconnected":
                self._last_reconnect_at = now
        if message not in _ROUTINE_MESSAGES:
            fields = {k: record[k] for k in _ACTIVITY_FIELDS if record.get(k) is not None}
            if message == "replayed buffered events" and record.get("event_ids"):
                fields["first_event_id"] = record["event_ids"][0]
            self.add_activity(record.get("logger", "edge_gateway"), record.get("level", "INFO"), message, fields)

    def add_activity(self, source: str, level: str, message: str, fields: Optional[Dict[str, Any]] = None) -> None:
        now = self._clock()
        with self._lock:
            # Coalesce repeats (e.g. one "buffered for replay" per event during
            # an outage, interleaved with the publisher's own error line) into
            # one entry with a count, moved to the newest position.
            for entry in list(self._activity)[-COALESCE_LOOKBACK:]:
                if entry["message"] == message and entry["source"] == source:
                    self._activity.remove(entry)
                    entry.update(count=entry["count"] + 1, at=_iso(now), level=level, fields=dict(fields or {}))
                    self._activity.append(entry)
                    return
            self._activity.append({"at": _iso(now), "source": source, "level": level, "message": message,
                                   "fields": dict(fields or {}), "count": 1})

    # ------------------------------------------------------------------ diagnostics

    def set_diagnostics(self, diagnostics: Dict[str, Any]) -> None:
        diagnostics = dict(diagnostics)
        diagnostics["events"] = list(diagnostics.get("events", []))[-MAX_DIAGNOSTIC_EVENTS:]
        diagnostics["anomalies"] = list(diagnostics.get("anomalies", []))[-MAX_DIAGNOSTIC_ANOMALIES:]
        with self._lock:
            self._diagnostics = diagnostics

    # ------------------------------------------------------------------ reads

    def last_replay_age(self) -> Optional[float]:
        with self._lock:
            return None if self._last_replay is None else self._clock() - self._last_replay["at_epoch"]

    def snapshot(self, vehicle_id: Optional[str] = None, max_events: int = 50) -> Dict[str, Any]:
        """A JSON-serializable view. `vehicle_id` selects whose chart
        history is included (default: the most recently seen vehicle)."""
        now = self._clock()
        with self._lock:
            in_window = [t for t in self._receive_times if now - t <= RATE_WINDOW_SECONDS]
            # Divide by the time actually covered, so the first seconds aren't under-reported.
            span = min(RATE_WINDOW_SECONDS, max(now - in_window[0], 1.0)) if in_window else RATE_WINDOW_SECONDS
            recent_rate = len(in_window) / span
            if self._last_received_at is None:
                flow = "none"
            elif now - self._last_received_at <= FLOW_FRESH_SECONDS:
                flow = "flowing"
            else:
                flow = "stale"
            if vehicle_id not in self._vehicles:
                vehicle_id = next(reversed(self._vehicles)) if self._vehicles else None
            events = []
            for item in list(self._recent)[-max_events:][::-1]:
                e = item.event
                lag = item.received_at - e.timestamp.timestamp()
                if e.event_id in self._replayed_ids:
                    status = "replayed"
                elif lag > LATE_EVENT_SECONDS:
                    status = "late"
                else:
                    status = "live"
                events.append({
                    "timestamp": e.timestamp.isoformat(), "received_at": _iso(item.received_at),
                    "lag_seconds": round(lag, 2), "event_id": e.event_id, "session_id": e.session_id,
                    "vehicle_id": e.vehicle_id, "source_ecu": e.source_ecu.value,
                    "signal_name": e.signal_name.value, "value": e.value, "unit": e.unit,
                    "topic": item.topic, "status": status,
                })
            vehicles = []
            for v in reversed(self._vehicles.values()):
                vehicles.append({
                    "vehicle_id": v["vehicle_id"], "event_count": v["event_count"],
                    "last_seen": _iso(v["last_seen"]), "seconds_since_seen": round(now - v["last_seen"], 1),
                    "last_session_id": v["last_session_id"],
                    "ecus": {name: {"event_count": ecu["event_count"], "last_seen": _iso(ecu["last_seen"]),
                                    "signals": dict(ecu["signals"])}
                             for name, ecu in sorted(v["ecus"].items())},
                })
            series = {}
            if vehicle_id is not None:
                series = {name: [[round(t, 3), value] for t, value in points]
                          for name, points in self._vehicles[vehicle_id]["series"].items()}
            last_replay = None
            if self._last_replay is not None:
                last_replay = {k: v for k, v in self._last_replay.items() if k != "at_epoch"}
                last_replay["seconds_ago"] = round(now - self._last_replay["at_epoch"], 1)
            return {
                "generated_at": _iso(now),
                "dashboard": {"started_at": _iso(self._started_at), "uptime_seconds": round(now - self._started_at, 1)},
                "telemetry": {
                    "flow": flow, "total_received": self._total_received, "malformed": self._malformed,
                    "duplicates": self._duplicates, "rate_per_second": round(recent_rate, 1),
                    "last_received_at": _iso(self._last_received_at),
                    "last_event_timestamp": self._last_event_timestamp,
                    "vehicle_count": len(self._vehicles),
                    "sessions_seen": list(reversed(self._sessions_seen)),
                },
                "vehicles": vehicles,
                "selected_vehicle": vehicle_id,
                "series": series,
                "signals": KNOWN_SIGNALS,
                "events": events,
                "activity": list(self._activity)[::-1],
                "last_replay": last_replay,
                "last_stopped_summary": self._last_stopped_summary,
                "diagnostics": self._diagnostics,
                "limits": {"recent_events": MAX_RECENT_EVENTS, "activity": MAX_ACTIVITY, "vehicles": MAX_VEHICLES,
                           "series_points": MAX_SERIES_POINTS},
            }


def derive_resilience_state(
    *, running: bool, mqtt_connected: Optional[bool], outage: str,
    buffer_depth: Optional[int], replayed_total: int, seconds_since_replay: Optional[float],
) -> Dict[str, str]:
    """Maps observable facts about a hosted gateway run to one lifecycle
    stage. Only claims what the inputs support; see ARCHITECTURE.md
    section 12 for the table.

    `outage` is what the dashboard itself knows about the simulated
    outage it controls: "active" (injected now), "cleared" (injected, then
    cleared, and the publisher has not reconnected yet), or "none". A real
    broker outage (e.g. `docker compose stop mosquitto`) is only ever seen
    as "disconnected", so it is reported as OUTAGE/BUFFERING -- the
    dashboard cannot know whether that broker is back yet."""
    if not running:
        return {"state": "IDLE", "detail": "no gateway run hosted by the dashboard"}
    if mqtt_connected is None or buffer_depth is None:
        return {"state": "UNKNOWN", "detail": "connection state or buffer depth unavailable"}
    if not mqtt_connected:
        if buffer_depth == 0:
            return {"state": "OUTAGE", "detail": "publisher disconnected; nothing buffered yet"}
        if outage == "cleared":
            return {"state": "RECONNECTING",
                    "detail": f"outage cleared; {buffer_depth} events still buffered until the next backoff-gated reconnect"}
        return {"state": "BUFFERING", "detail": f"publisher disconnected; {buffer_depth} events waiting in SQLite"}
    if buffer_depth > 0:
        # "Connected" alone is not evidence of replay: the publisher can
        # report connected while publishes still fail (e.g. reconnect()
        # succeeded at TCP level before the broker was really back), and
        # rows buffered without a detected disconnect wait for the next
        # reconnect/restart (docs/assumptions-and-limitations.md, Phase 9).
        # Only claim REPLAYING when the gateway has just logged a replay batch.
        if seconds_since_replay is not None and seconds_since_replay <= RECENT_REPLAY_SECONDS:
            return {"state": "REPLAYING", "detail": f"connected; {buffer_depth} buffered events still to replay (FIFO)"}
        return {"state": "BUFFERING",
                "detail": f"publisher reports connected, but {buffer_depth} events are buffered and no replay is "
                          "in progress; the gateway replays after it observes a reconnect (or at restart)"}
    if replayed_total > 0:
        recent = seconds_since_replay is not None and seconds_since_replay <= RECENT_REPLAY_SECONDS
        return {"state": "RECOVERED",
                "detail": f"buffer drained; {replayed_total} events replayed" + (" just now" if recent else "")}
    return {"state": "NORMAL", "detail": "connected; buffer empty; publishing live"}
