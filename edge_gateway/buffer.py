"""
Phase 4: a persistent, ordered, size-bounded local buffer for telemetry
that could not be published live -- SQLite, via Python's own `sqlite3`
module (no new dependency).

Why SQLite instead of a hand-rolled file-backed queue: a plain
append-only file needs *us* to solve atomic writes, crash-safe deletes,
and compaction; SQLite's transactions already solve exactly that, with
one file and no server, which fits this project's "no unjustified
complexity" principle (see ARCHITECTURE.md's "Local buffer uses SQLite"
decision row). The buffer is a plain file (not `:memory:`), so it
survives a gateway process restart: reopening a `TelemetryBuffer` against
the same path picks up whatever a previous run left unsent.

Ordering: `id` is an SQLite `INTEGER PRIMARY KEY AUTOINCREMENT`, which is
monotonic and assigned exactly once per row. Reading rows `ORDER BY id
ASC` therefore always replays events in the exact order they were
enqueued -- this matters for time-series signals (speed, RPM, etc.)
where out-of-order delivery would misrepresent the vehicle's actual
state history.

What "removed" means: this module only ever deletes a row when its
caller (`edge_gateway/gateway.py`) tells it to, via `remove(ids)` --
the buffer itself has no opinion on when a publish counts as
successful. See `mqtt_publisher.py`'s `publish()` docstring and
`gateway.py`'s `_replay_buffered()` for the actual "when is it safe to
delete" rule this project uses (a broker-acknowledged QoS-1 publish, not
just "no exception was raised").
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import List, NamedTuple, Optional, Union

# Roughly minutes-to-tens-of-minutes of outage coverage at this
# simulation's combined ECU rate (10 Hz + 2 Hz + 1 Hz) -- the right scale
# for a demo/portfolio POC to reason about and explain, not an attempt to
# survive a real multi-hour outage. Callers can pass a smaller value
# (tests do) or a larger one.
DEFAULT_MAX_BUFFERED_EVENTS = 5000


class BufferedEvent(NamedTuple):
    """One row read back from the buffer. `id` is the ordering key used
    by both `peek_batch` (read order) and `remove` (what to delete)."""

    id: int
    event_id: str
    topic: str
    payload: bytes
    enqueued_at: float


class TelemetryBuffer:
    """A durable FIFO queue of (topic, payload) pairs, bounded to
    `max_size` rows. When full, the oldest row is dropped to make room
    for the newest -- for telemetry, the most recent vehicle state is
    generally more valuable to eventually deliver than a stale one from
    minutes ago. Every drop is logged and counted (`dropped_count`)."""

    def __init__(
        self,
        db_path: Union[str, Path],
        max_size: int = DEFAULT_MAX_BUFFERED_EVENTS,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._max_size = max_size
        self._logger = logger
        self.dropped_count = 0

        if str(self._db_path) != ":memory:" and self._db_path.parent != Path(""):
            self._db_path.parent.mkdir(parents=True, exist_ok=True)

        # check_same_thread=False: this project's own usage pattern is
        # exactly the case that flag exists for -- run_demo.py constructs
        # the TelemetryBuffer on the main thread but EdgeGateway.run()
        # (enqueue/replay) executes on its own dedicated thread (see
        # run_demo.py), and the main thread only touches the buffer again
        # (count()/close() for the final summary log) after that thread
        # has been joined. SQLite's default check_same_thread=True raises
        # ProgrammingError the moment a *different* thread than the one
        # that opened the connection touches it -- discovered by an
        # actual crash during Phase 4's live run_demo.py verification,
        # not by reasoning about it up front. There is still only ever
        # one thread actively using the connection at a given moment;
        # this only disables sqlite3's same-thread-origin check, it does
        # not add real concurrent access.
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                payload BLOB NOT NULL,
                enqueued_at REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    def enqueue(self, event_id: str, topic: str, payload: bytes) -> None:
        """Appends one event to the tail of the buffer, then trims the
        oldest row(s) if that pushed the buffer over `max_size`."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO pending_telemetry (event_id, topic, payload, enqueued_at) "
                "VALUES (?, ?, ?, ?)",
                (event_id, topic, payload, time.time()),
            )
            dropped = self._trim_to_max_locked()

        if dropped and self._logger is not None:
            self._logger.warning(
                "telemetry buffer full (max %d events) -- dropped %d oldest event(s)",
                self._max_size, dropped,
            )

    def _trim_to_max_locked(self) -> int:
        """Must be called from inside an open `self._conn` transaction.
        Returns how many rows were dropped (0 if under the cap)."""
        (count,) = self._conn.execute("SELECT COUNT(*) FROM pending_telemetry").fetchone()
        overflow = count - self._max_size
        if overflow <= 0:
            return 0
        self._conn.execute(
            "DELETE FROM pending_telemetry WHERE id IN "
            "(SELECT id FROM pending_telemetry ORDER BY id ASC LIMIT ?)",
            (overflow,),
        )
        self.dropped_count += overflow
        return overflow

    def peek_batch(self, limit: int = 100) -> List[BufferedEvent]:
        """Reads up to `limit` events in FIFO order, without removing
        them -- the caller decides what counts as successfully delivered
        and calls `remove()` itself."""
        rows = self._conn.execute(
            "SELECT id, event_id, topic, payload, enqueued_at "
            "FROM pending_telemetry ORDER BY id ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [BufferedEvent(*row) for row in rows]

    def remove(self, ids: List[int]) -> None:
        """Permanently deletes the given rows -- call this only for
        events that were actually, successfully delivered."""
        if not ids:
            return
        with self._conn:
            self._conn.executemany(
                "DELETE FROM pending_telemetry WHERE id = ?", [(row_id,) for row_id in ids],
            )

    def count(self) -> int:
        (count,) = self._conn.execute("SELECT COUNT(*) FROM pending_telemetry").fetchone()
        return count

    def close(self) -> None:
        self._conn.close()
