"""
Phase 7: the Edge Gateway's operational metrics -- a small, in-process
counter object, not a monitoring platform. No new dependency, no metrics
server, no persistence: counters live for one `EdgeGateway` instance (one
gateway run) and are exposed as an immutable snapshot that the gateway
logs as a structured JSON line (see `EdgeGateway.run()` and run_demo.py).

What each counter means (unchanged from the plain attributes Phases 3-4
kept directly on EdgeGateway -- this module only gives them one home):

- processed         a live telemetry event whose publish the broker
                    acknowledged (QoS-1 PUBACK -- see mqtt_publisher.py)
- rejected          a decoded event dropped by validation (out of range)
- publish_failures  a live publish that was not acknowledged
- buffered          an event written to the SQLite buffer after a failed
                    live publish (today always equal to publish_failures;
                    kept separate because they are different facts)
- replayed          a buffered event later published and acknowledged

`dropped` (buffer overflow) is deliberately NOT a counter here: the one
authoritative count is `TelemetryBuffer.dropped_count`, incremented inside
the same SQLite transaction that deletes the rows. Counting it twice would
invite the two numbers to disagree, so a snapshot simply *reads* it -- see
`GatewayMetrics.snapshot(dropped=...)`.

Correlation: every snapshot carries the gateway's `session_id`, the
run-level correlation ID that also appears on every gateway log line and
in every published payload. Per-event identity (`event_id`) is never a
metrics dimension -- it stays on the individual log lines and payloads.

Thread safety: today every increment happens on the gateway's own thread,
while snapshots may be read from another (run_demo.py's main thread, or a
test). A single lock makes each increment and each snapshot atomic, so a
snapshot is always internally consistent regardless of who calls it.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Dict


@dataclass(frozen=True)
class MetricsSnapshot:
    """An immutable, point-in-time copy of one gateway run's counters.
    `as_log_fields()` gives the flat, JSON-serializable dict used as
    `extra=` on a structured log line."""

    session_id: str
    processed: int
    rejected: int
    publish_failures: int
    buffered: int
    replayed: int
    dropped: int

    def as_log_fields(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class GatewayMetrics:
    """Mutable counters for one gateway run. Only `EdgeGateway` should
    call the `record_*` methods; everything else reads via `snapshot()`
    or the read-only properties."""

    session_id: str
    _processed: int = field(default=0, init=False)
    _rejected: int = field(default=0, init=False)
    _publish_failures: int = field(default=0, init=False)
    _buffered: int = field(default=0, init=False)
    _replayed: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    # --- increments (one per semantic event; see the module docstring) ---

    def record_processed(self) -> None:
        with self._lock:
            self._processed += 1

    def record_rejected(self) -> None:
        with self._lock:
            self._rejected += 1

    def record_publish_failure(self) -> None:
        with self._lock:
            self._publish_failures += 1

    def record_buffered(self) -> None:
        with self._lock:
            self._buffered += 1

    def record_replayed(self, count: int = 1) -> None:
        if count < 0:
            raise ValueError("replayed count cannot be negative")
        with self._lock:
            self._replayed += count

    # --- reads ---

    @property
    def processed(self) -> int:
        return self._processed

    @property
    def rejected(self) -> int:
        return self._rejected

    @property
    def publish_failures(self) -> int:
        return self._publish_failures

    @property
    def buffered(self) -> int:
        return self._buffered

    @property
    def replayed(self) -> int:
        return self._replayed

    def snapshot(self, *, dropped: int) -> MetricsSnapshot:
        """`dropped` is read from the buffer by the caller (the gateway)
        rather than counted here -- see the module docstring."""
        with self._lock:
            return MetricsSnapshot(
                session_id=self.session_id,
                processed=self._processed,
                rejected=self._rejected,
                publish_failures=self._publish_failures,
                buffered=self._buffered,
                replayed=self._replayed,
                dropped=int(dropped),
            )
