"""Offline queue for storing events when BetterFlow is unreachable."""

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

try:
    from ..config import Config, MAX_QUEUE_SIZE
except ImportError:
    from config import Config, MAX_QUEUE_SIZE

__all__ = [
    "OfflineQueue",
    "QueuedEvent",
    "is_event_storable",
    "is_permanently_unstorable",
    "normalized_project_id",
    "MAX_EVENT_DURATION_SECONDS",
    "EVENT_RETENTION_DAYS",
]

logger = logging.getLogger(__name__)

# The server (internal-tool2 AgentEventController) accepts a single event only
# when its duration is within [0, MAX_EVENT_DURATION_SECONDS]. A longer span — a
# >24h weekend lid-close sleep, or a Private-Time session left on across a
# weekend — 4xx-rejects the WHOLE batch it rides in, and _process_queue's
# whole-batch retry bump then drags its storable neighbours to the drop ceiling
# in lockstep. Evicting the over-long span first keeps every batch clean.
MAX_EVENT_DURATION_SECONDS = 86400  # 24h, mirrors the server-side validator
# Cap on the stored rejection text. Server-controlled and unbounded at the
# source, written to every event in a failing batch.
_MAX_LAST_ERROR_CHARS = 500

# The server's retention window: it rejects an event whose timestamp is older
# than this, so the agent treats such an event as unstorable rather than holding
# it forever. ONE definition, deliberately — this was written four times (three
# `stale_after_days` defaults here plus a bare `timedelta(days=7)` in
# SyncEngine._batch_has_storable_activity), and eviction and the dead-letter
# replay are now a PAIRED loop. A drift between two copies is self-sustaining,
# not merely wrong: widen one and eviction drops a row the replay resurrects
# every cooldown, with `dropped_at` restamped on each pass so the cooldown never
# terminates it. Same reasoning that gave MAX_EVENT_DURATION_SECONDS its name.
EVENT_RETENTION_DAYS = 7


def _parse_timestamp(ts: object) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, or None if it cannot be read.

    Split out from ``_timestamp_within`` because the two questions are NOT the
    same and conflating them deleted data. ``_timestamp_within`` answers "is
    this provably fresh?", and returns False for both "provably old" and "age
    unknown" — correct there, because an unreadable event is unstorable either
    way. Any caller acting IRREVERSIBLY on age needs to tell those apart, so it
    asks this and requires a non-None answer first.
    """
    if not ts or not isinstance(ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _timestamp_within(ts: Optional[str], cutoff: datetime) -> bool:
    """True when ``ts`` (ISO-8601) parses and is at/after ``cutoff``. A missing
    or unparseable timestamp is treated as NOT within (unstorable)."""
    parsed = _parse_timestamp(ts)
    return parsed is not None and parsed >= cutoff


def is_permanently_unstorable(ev: object, *, terminal_cutoff: datetime) -> bool:
    """True only when the payload POSITIVELY SHOWS the server can never accept it.

    The counterpart to ``is_event_storable``, and deliberately NOT its negation.
    "Unstorable right now" and "unstorable forever" are different questions, and
    the first version of the replay's terminal rule conflated them: it acted on
    `not _timestamp_within(...)`, which is False for an unreadable timestamp as
    well as an old one, and marked rows whose age it had no evidence about.

    So every branch here is a value that was READ, never a value that was
    missing. Absence of evidence marks nothing.

    A dead-letter payload is immutable — the only UPDATE on that table sets the
    terminal flag, and ``backfill_status_bucket_ids`` repairs ``queued_events``
    only — so each of these is genuinely terminal rather than merely current:

    - a timestamp that PARSES and is older than ``terminal_cutoff``: the server
      refuses it on retention and always will;
    - a ``duration`` that IS a number and lies outside the accepted bounds: the
      value cannot change, so the 4xx cannot stop;
    - a MISSING ``bucket_id``: unroutable by construction, and nothing can add
      one to a dead-lettered row. This is the one absence that qualifies,
      because the absence IS the disqualifying fact rather than a gap in what we
      know about some other property.

    A missing or unreadable timestamp is NOT terminal: it is the likely shape of
    a serialisation bug and must stay visible and replayable.
    """
    if not isinstance(ev, dict):
        return False  # unparseable: preserve it, it is evidence
    if not ev.get("bucket_id"):
        return True
    duration = ev.get("duration")
    if isinstance(duration, bool):
        return False
    if isinstance(duration, (int, float)) and not (
        0 <= duration <= MAX_EVENT_DURATION_SECONDS
    ):
        return True
    parsed = _parse_timestamp(ev.get("timestamp"))
    return parsed is not None and parsed < terminal_cutoff


def is_event_storable(ev: object, *, stale_cutoff: datetime) -> bool:
    """True when the server would accept this event once online, so the offline
    queue must HOLD it for retry rather than evict it as unstorable poison.

    The single source of truth for "storable", shared by ``evict_unstorable``,
    ``failed_event_summary`` and ``SyncEngine._batch_has_storable_activity`` (so
    the three can never drift). Storable requires ALL of:

    - a ``bucket_id`` to route it. A bucketless span was the 2026-06
      "buckets=unknown" drop; every emitted span now carries one, and legacy
      bucketless status spans queued by <=1.5.95 are given theirs at startup
      (see ``OfflineQueue.backfill_status_bucket_ids``). Keeping the bucket_id
      requirement preserves the prod-proven poison-drop protection rather than
      loosening the rule the fleet already relies on.
    - a ``timestamp`` present, parseable, and within the retention window.
    - a ``duration`` within the server's accepted 0..MAX bounds. An over-long
      span 4xx-rejects the whole batch (the weekend-suspend poison); a
      missing/non-numeric duration is left to the server, not evicted here.
    """
    if not isinstance(ev, dict):
        return False
    if not ev.get("bucket_id"):
        return False
    if not _timestamp_within(ev.get("timestamp"), stale_cutoff):
        return False
    duration = ev.get("duration")
    # bool is an int subclass but is never a valid duration.
    if isinstance(duration, bool):
        return False
    if isinstance(duration, (int, float)):
        return 0 <= duration <= MAX_EVENT_DURATION_SECONDS
    # Missing/non-numeric duration: not our call to evict — leave it to the server.
    return True


def normalized_project_id(value: object) -> Optional[int]:
    """Return a backend-safe project id, or None when the payload should omit it.

    Shared with SyncEngine's stamping path so the queue migration and the live
    stamp can never disagree about which ids the backend will accept.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdecimal():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


@dataclass
class QueuedEvent:
    """An event stored in the offline queue."""

    id: int
    event_data: dict
    created_at: datetime
    retry_count: int = 0

    @classmethod
    def from_row(cls, row: tuple) -> Optional["QueuedEvent"]:
        """Create from database row. Returns None if the row is corrupt."""
        try:
            event_data = json.loads(row[1])
            created_at = datetime.fromisoformat(row[2])
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.error(f"[queue] Corrupt event row id={row[0]}, discarding")
            return None
        return cls(
            id=row[0],
            event_data=event_data,
            created_at=created_at,
            retry_count=row[3],
        )


class OfflineQueue:
    """SQLite-based offline queue for events."""

    def __init__(self, db_path: Optional[Path] = None, max_size: int = MAX_QUEUE_SIZE):
        """Initialize the offline queue.

        Args:
            db_path: Path to SQLite database file
            max_size: Maximum number of events to store
        """
        if db_path is None:
            db_path = Config.get_data_dir() / "offline_queue.db"

        self.db_path = db_path
        self.max_size = max_size
        self._local = threading.local()
        # Track connections for cleanup in close() (M4)
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._closed = False
        self._last_integrity_check: float = time.monotonic()
        self._integrity_check_interval: float = 3600.0  # 1 hour
        self._integrity_lock = threading.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection.

        Recreates the connection if the queue was closed and reopened,
        or if this thread's handle was closed by close().

        The liveness probe runs outside the lock to avoid blocking other
        threads during I/O. Connection creation and registration happen
        under the lock with a second _closed check to prevent races.
        """
        # Fast path: check existing connection outside the lock
        with self._connections_lock:
            if self._closed:
                raise sqlite3.ProgrammingError("OfflineQueue has been closed")
            existing = getattr(self._local, "connection", None)

        if existing is not None:
            try:
                existing.execute("SELECT 1")
                return existing
            except (sqlite3.ProgrammingError, sqlite3.OperationalError):
                # Stale connection — will create a new one below
                if hasattr(self._local, "connection"):
                    del self._local.connection

        # Create connection outside the lock to avoid blocking other threads
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row

        with self._connections_lock:
            if self._closed:
                conn.close()
                raise sqlite3.ProgrammingError("OfflineQueue has been closed")
            self._connections.append(conn)
            self._local.connection = conn
        return self._local.connection

    @contextmanager
    def _cursor(self, *, immediate: bool = False) -> Iterator[sqlite3.Cursor]:
        """Context manager for database cursor.

        ``immediate=True`` opens the transaction with ``BEGIN IMMEDIATE`` BEFORE
        the first statement runs. Required for every read-then-write sequence
        that claims atomicity, because pysqlite's implicit transaction does not
        begin until the first DML statement — so a ``SELECT`` that CHOOSES the
        rows a later ``INSERT``/``DELETE`` acts on runs in autocommit, outside
        the transaction, and is not protected by it.

        Connections are per-thread here, so two threads reach the same file with
        two connections and can both read the same rows before either writes.
        That is reachable in production, not theoretical:
        ``main._acquire_sync_slot`` abandons a wedged cycle after 420s and starts
        a fresh ``_do_sync`` while the zombie thread is still inside
        ``_process_queue``.

        This does not add a new way to fail. Every caller that needs it already
        had to take the same write lock for its ``INSERT``; ``BEGIN IMMEDIATE``
        only takes it earlier, so the contention window shrinks rather than
        grows. A conflicting writer blocks for the connection's busy timeout
        exactly as it did before.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            if immediate:
                cursor.execute("BEGIN IMMEDIATE")
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()

    def _init_db(self) -> None:
        """Initialize the database schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Integrity check — reset on corruption (close connection even on failure)
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                result = conn.execute("PRAGMA integrity_check").fetchone()
                if result[0] != "ok":
                    logger.error(f"SQLite integrity check failed: {result[0]} — resetting database")
                    backup = self.db_path.with_suffix(".db.corrupt")
                    self.db_path.rename(backup)
        except sqlite3.DatabaseError as e:
            logger.error(f"SQLite corruption detected: {e} — resetting database")
            backup = self.db_path.with_suffix(".db.corrupt")
            try:
                self.db_path.rename(backup)
            except OSError:
                self.db_path.unlink(missing_ok=True)

        with self._cursor() as cursor:
            # Enable WAL mode for crash resilience
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS queued_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    retry_count INTEGER DEFAULT 0,
                    last_error TEXT
                )
                """
            )
            # Additive migration for queues created before this column existed.
            # The reason a batch was REJECTED has to be recorded when the send
            # fails and read when the event is finally dropped, and those happen
            # on different sync cycles — remove_failed runs at the top of
            # _process_queue, decoupled from the batch that failed. Nullable and
            # unread by delivery, so an agent that downgrades still works.
            cols = {r[1] for r in cursor.execute("PRAGMA table_info(queued_events)")}
            if "last_error" not in cols:
                cursor.execute("ALTER TABLE queued_events ADD COLUMN last_error TEXT")
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_created_at ON queued_events(created_at)
                """
            )

            # Dead-letter table: events that exhausted their retries are MOVED
            # here rather than hard-deleted. An event only reaches the retry
            # ceiling on a DEFINITIVE 4xx (transient 5xx/timeout deliberately
            # don't increment), which can still be real aw-watcher-afk /
            # aw-watcher-input activity — so a blind DELETE was permanent data
            # loss. Preserving event_data (plus bucket_id, timestamps,
            # retry_count, and dropped_at/last_error) keeps the activity
            # inspectable and replayable instead of gone.
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS dead_letter_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_id INTEGER,
                    event_data TEXT NOT NULL,
                    bucket_id TEXT,
                    created_at TEXT NOT NULL,
                    retry_count INTEGER,
                    last_error TEXT,
                    dropped_at TEXT NOT NULL,
                    terminal INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # Additive migration. `terminal` marks a row the server can never
            # accept again, so the replay can exclude it from its candidate
            # ordering instead of re-examining it every cycle forever. Marking
            # rather than DELETING is deliberate: a mis-marked row costs a
            # missed replay, a wrongly-deleted one costs the activity.
            dl_cols = {r[1] for r in cursor.execute(
                "PRAGMA table_info(dead_letter_events)")}
            if "terminal" not in dl_cols:
                cursor.execute(
                    "ALTER TABLE dead_letter_events "
                    "ADD COLUMN terminal INTEGER NOT NULL DEFAULT 0"
                )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dead_letter_dropped_at
                ON dead_letter_events(dropped_at)
                """
            )

            # Also track sync checkpoints
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_checkpoints (
                    bucket_id TEXT PRIMARY KEY,
                    last_event_id INTEGER,
                    last_timestamp TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            # App category mappings (synced from server, user-overridable)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS app_categories (
                    app_name TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'server',
                    updated_at TEXT NOT NULL
                )
                """
            )

            # Per-event counted active-seconds, persisted so the in-memory
            # time-dedup cache survives a restart. Without this, a restart
            # (or the start-of-day backlog reconcile that re-fetches the whole
            # day) re-counts already-counted events into the local daily total,
            # inflating the tray's "active time". Keyed by (bucket_id, event_id);
            # `day` scopes load/prune to the local day the time belongs to.
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS counted_time (
                    bucket_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    counted_seconds REAL NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (bucket_id, event_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_counted_time_day ON counted_time(day)
                """
            )

    def check_integrity(self) -> bool:
        """Run periodic SQLite integrity check (N2).

        Returns True if database is healthy, False if corruption detected.
        Automatically resets the database on corruption.
        """
        with self._integrity_lock:
            now = time.monotonic()
            if now - self._last_integrity_check < self._integrity_check_interval:
                return True
            # Claim the slot immediately to prevent concurrent threads from
            # also passing the threshold and running duplicate checks.
            self._last_integrity_check = now
        try:
            with self._cursor() as cursor:
                cursor.execute("PRAGMA quick_check")
                result = cursor.fetchone()
                if result[0] != "ok":
                    logger.error(f"SQLite quick_check failed: {result[0]}")
                    with self._integrity_lock:
                        self._last_integrity_check = 0.0  # reset so next call retries
                    return False
            return True
        except sqlite3.DatabaseError as e:
            logger.error(f"SQLite integrity check error: {e}")
            with self._integrity_lock:
                self._last_integrity_check = 0.0  # reset so next call retries
            return False

    def enqueue(self, events: list[dict]) -> int:
        """Add events to the queue.

        Args:
            events: List of event dictionaries

        Returns:
            Number of events added
        """
        if not events:
            return 0

        # If batch is larger than max_size, only keep newest events
        if len(events) > self.max_size:
            events = events[-self.max_size:]
            logger.warning(f"Batch larger than max_size, truncated to {len(events)} events")

        now = datetime.now(timezone.utc).isoformat()
        with self._cursor(immediate=True) as cursor:
            # Atomic: check size + evict + insert in a single transaction.
            # immediate=True is what makes that claim TRUE — the COUNT decides
            # how many rows to evict, so it has to be inside the transaction the
            # eviction runs in. Without it two concurrent enqueues both read the
            # same count, both decide there is room, and the queue overshoots
            # max_size.
            cursor.execute("SELECT COUNT(*) FROM queued_events")
            current_size = cursor.fetchone()[0]
            if current_size + len(events) > self.max_size:
                to_remove = current_size + len(events) - self.max_size
                cursor.execute(
                    """
                    DELETE FROM queued_events
                    WHERE id IN (
                        SELECT id FROM queued_events
                        ORDER BY created_at ASC
                        LIMIT ?
                    )
                    """,
                    (to_remove,),
                )
                logger.warning(f"Queue full, removed {to_remove} oldest events")

            count = len(events)
            cursor.executemany(
                """
                INSERT INTO queued_events (event_data, created_at)
                VALUES (?, ?)
                """,
                [(json.dumps(e), now) for e in events],
            )
            return count

    def dequeue(self, batch_size: int = 100, max_retries: int = 5) -> list[QueuedEvent]:
        """Get a batch of events from the queue (oldest first).

        Skips events that have exceeded max_retries (N7).

        Args:
            batch_size: Maximum number of events to return
            max_retries: Skip events with retry_count >= this value

        Returns:
            List of QueuedEvent objects
        """
        # Periodic integrity check (N2)
        if not self.check_integrity():
            logger.error("[queue] Skipping dequeue due to integrity failure")
            return []
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT id, event_data, created_at, retry_count
                FROM queued_events
                WHERE retry_count < ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (max_retries, batch_size),
            )
            rows = cursor.fetchall()
            result: list[QueuedEvent] = []
            corrupt_ids: list[int] = []
            for row in rows:
                event = QueuedEvent.from_row(tuple(row))
                if event is None:
                    # Corrupt JSON: schedule for removal so it doesn't
                    # permanently shrink every future dequeue batch.
                    # Without this, retry_count never increments for these
                    # rows (only dequeued events go through increment_retry),
                    # so remove_failed never sees them and expire_old is the
                    # only cleanup path (up to 30 days later).
                    corrupt_ids.append(row[0])
                else:
                    result.append(event)
            if corrupt_ids:
                placeholders = ",".join("?" * len(corrupt_ids))
                cursor.execute(
                    f"DELETE FROM queued_events WHERE id IN ({placeholders})",
                    corrupt_ids,
                )
                logger.warning(
                    "[queue] Removed %d corrupt event row(s) during dequeue",
                    len(corrupt_ids),
                )
            return result

    def remove(self, event_ids: list[int]) -> int:
        """Remove events from the queue.

        Args:
            event_ids: List of event IDs to remove

        Returns:
            Number of events removed
        """
        if not event_ids:
            return 0

        with self._cursor() as cursor:
            placeholders = ",".join("?" * len(event_ids))
            cursor.execute(
                f"""
                DELETE FROM queued_events
                WHERE id IN ({placeholders})
                """,
                event_ids,
            )
            return cursor.rowcount

    def increment_retry(
        self, event_ids: list[int], last_error: Optional[str] = None
    ) -> None:
        """Increment retry count for events, recording WHY the send failed.

        Args:
            event_ids: List of event IDs to update
            last_error: The server's own rejection text, when it gave one. Only
                written when supplied, so a later reason-less failure cannot
                erase a definitive 4xx we already captured — the drop happens
                cycles later and that string is the only evidence of the cause.
                Truncated at the WRITE: this is unbounded server-controlled
                text (http_client passes the response body's "message"
                through verbatim) landing on every event in a failing batch,
                and the queue holds up to max_size of them.
        """
        if not event_ids:
            return
        if last_error is not None:
            last_error = str(last_error)[:_MAX_LAST_ERROR_CHARS]

        with self._cursor() as cursor:
            placeholders = ",".join("?" * len(event_ids))
            if last_error is None:
                cursor.execute(
                    f"""
                    UPDATE queued_events
                    SET retry_count = retry_count + 1
                    WHERE id IN ({placeholders})
                    """,
                    event_ids,
                )
            else:
                cursor.execute(
                    f"""
                    UPDATE queued_events
                    SET retry_count = retry_count + 1, last_error = ?
                    WHERE id IN ({placeholders})
                    """,
                    [last_error, *event_ids],
                )

    def failed_event_summary(
        self,
        max_retries: int = 5,
        *,
        now: Optional[datetime] = None,
        stale_after_days: int = EVENT_RETENTION_DAYS,
    ) -> dict:
        """Summarize events at/over the retry ceiling that remove_failed() is
        about to drop. Read-only — does NOT delete.

        Dropping queued events is normally permanent data loss, but not every
        drop is real loss: the server legitimately rejects events that are
        unstorable BY NATURE — older than its retention window (``stale_after_days``,
        the >7d-stale rule) or carrying no ``bucket_id`` (nowhere to route them).
        Those exhaust their retries and get flushed, which is benign. We classify
        each so the caller can warn only on genuine loss (recent + bucketed) and
        log the benign flushes quietly instead of paging ops.

        Returns {count, bucket_ids, oldest, newest, real_loss_count,
        unstorable_count, last_errors} — all zero/empty when clean.

        ``last_errors`` is the sorted set of DISTINCT rejection texts recorded
        by ``increment_retry``, so ``_report_dropped_events`` can name the
        CAUSE rather than only the count. A set rather than one string because
        a single drop cycle routinely carries events from several batches with
        several different rejections, and reporting one of them as "the" cause
        answers the malformed-vs-receiving-side question wrongly and
        confidently.

        ``now`` is injectable for deterministic tests (defaults to UTC now).
        """
        now = now or datetime.now(timezone.utc)
        stale_cutoff = now - timedelta(days=stale_after_days)

        with self._cursor() as cursor:
            cursor.execute(
                "SELECT event_data, last_error FROM queued_events "
                "WHERE retry_count >= ?",
                (max_retries,),
            )
            rows = cursor.fetchall()

        bucket_ids: set[str] = set()
        timestamps: list[str] = []
        real_loss = 0
        unstorable = 0
        # Every DISTINCT reason, not one row's. The predicate here is
        # ``retry_count >= max_retries`` over the WHOLE table, so these rows
        # accumulate across many batches and many cycles — they do NOT share a
        # rejection. There is also no ORDER BY and nothing records WHEN a reason
        # was written, so "the newest" is not computable from what is stored;
        # picking one row's string reports a confident wrong cause for the rest.
        reasons: set[str] = set()
        for row in rows:
            if row[1]:
                reasons.add(str(row[1]))
            try:
                ev = json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                # Can't even parse it → it could never be stored. Benign flush.
                unstorable += 1
                continue
            bid = ev.get("bucket_id")
            if bid:
                bucket_ids.add(str(bid))
            ts = ev.get("timestamp")
            if ts:
                timestamps.append(str(ts))
            # A drop is real loss only if the event was actually storable — same
            # classifier the eviction path uses, so the two never disagree.
            if is_event_storable(ev, stale_cutoff=stale_cutoff):
                real_loss += 1
            else:
                unstorable += 1
        timestamps.sort()
        return {
            "count": len(rows),
            "bucket_ids": sorted(bucket_ids),
            "oldest": timestamps[0] if timestamps else None,
            "newest": timestamps[-1] if timestamps else None,
            "real_loss_count": real_loss,
            "unstorable_count": unstorable,
            "last_errors": sorted(reasons),
        }

    @staticmethod
    def _timestamp_within(ts: Optional[str], cutoff: datetime) -> bool:
        """Retained thin wrapper over the module-level ``_timestamp_within`` for
        callers/tests that reach for it on the class."""
        return _timestamp_within(ts, cutoff)

    def remove_failed(
        self,
        max_retries: int = 5,
        *,
        last_error: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> int:
        """Move events that have exceeded max retries to the dead-letter table.

        Previously this HARD-DELETED exhausted events, permanently losing real
        activity that hit a definitive 4xx (transient 5xx/timeout don't increment
        retry_count, so anything here is a genuine rejection — but still real
        aw-watcher-afk / aw-watcher-input data worth keeping). Now each row is
        MOVED to ``dead_letter_events`` with its event_data, bucket_id (parsed
        out for queryability), created_at, retry_count, an optional last_error,
        and a dropped_at stamp — nothing is silently lost.

        The selecting SELECT, the INSERT and the matching DELETE all run inside
        the SAME ``BEGIN IMMEDIATE`` ``_cursor()`` transaction (committed
        atomically on exit), so a crash can never both drop the events from the
        queue AND fail to record the dead letter. The DELETE targets the exact
        ids that were moved (not the ``>= max_retries`` predicate) so a
        concurrent writer that pushes another event over the ceiling between the
        SELECT and the DELETE can't have it deleted without first being
        dead-lettered.

        ``immediate=True`` is load-bearing for the same reason as in
        ``requeue_storable_dead_letter``: pysqlite opens the implicit
        transaction at the first DML, so the SELECT that CHOOSES the rows used
        to run in autocommit. Two threads then both selected the same event and
        wrote TWO dead-letter rows for it — and the replay resurrects both, so
        the same billable span is delivered twice.

        Args:
            max_retries: Maximum retry attempts
            last_error: Optional context recorded on each dead-lettered row
            now: Injectable clock for deterministic tests (defaults to UTC now),
                matching ``evict_unstorable`` and ``requeue_storable_dead_letter``.
                Load-bearing, not a convenience: ``dropped_at`` is the column the
                replay's cooldown filters on (``WHERE dropped_at <= cutoff``).
                Stamping it from the real clock while a caller injects ``now``
                into the reader makes the two drift apart the moment wall-clock
                passes the fixture's date — every dead-lettered row then sorts
                AFTER the cutoff, the replay selects nothing, and the failure
                looks like a broken feature rather than a stale fixture.

        Returns:
            Number of events moved out of the queue
        """
        now = (now or datetime.now(timezone.utc)).isoformat()
        with self._cursor(immediate=True) as cursor:
            cursor.execute(
                """
                SELECT id, event_data, created_at, retry_count, last_error
                FROM queued_events
                WHERE retry_count >= ?
                """,
                (max_retries,),
            )
            rows = cursor.fetchall()
            if not rows:
                return 0

            dead_rows = []
            for row in rows:
                bucket_id = None
                try:
                    parsed = json.loads(row[1])
                    if isinstance(parsed, dict):
                        bucket_id = parsed.get("bucket_id")
                except (json.JSONDecodeError, TypeError):
                    # Keep the raw event_data regardless; only the queryable
                    # bucket_id column is left NULL for an unparseable payload.
                    bucket_id = None
                # The server's own words win over the agent's generic string.
                # "exceeded max retries (5); definitive rejection" describes what
                # WE did, never why the server refused — and that distinction is
                # the whole difference between a malformed event and a
                # receiving-side change, which have opposite fixes.
                reason = row[4] or last_error
                dead_rows.append(
                    (row[0], row[1], bucket_id, row[2], row[3], reason, now)
                )

            cursor.executemany(
                """
                INSERT INTO dead_letter_events
                    (original_id, event_data, bucket_id, created_at,
                     retry_count, last_error, dropped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                dead_rows,
            )

            ids = [row[0] for row in rows]
            placeholders = ",".join("?" * len(ids))
            cursor.execute(
                f"DELETE FROM queued_events WHERE id IN ({placeholders})",
                ids,
            )
            count = cursor.rowcount
            if count > 0:
                logger.warning(
                    "Moved %d event(s) that exceeded max retries to "
                    "dead_letter_events",
                    count,
                )
            return count

    def evict_unstorable(
        self,
        *,
        now: Optional[datetime] = None,
        stale_after_days: int = EVENT_RETENTION_DAYS,
        last_error: Optional[str] = None,
    ) -> dict:
        """Move events the server can NEVER accept out of the active queue and
        into ``dead_letter_events`` BEFORE they are batched with storable events.

        "Unstorable" is the SAME classification (``is_event_storable``) that
        ``failed_event_summary`` and ``SyncEngine._batch_has_storable_activity``
        use: an event with no ``bucket_id`` (nowhere to route it), a ``timestamp``
        already past the server's retention window (``stale_after_days``), or a
        ``duration`` outside the server's accepted 0..MAX bounds (an over-long
        weekend-suspend span 4xx's the whole batch). The only change is WHEN it's
        applied — proactively, up front — instead of only at the retry ceiling
        after the damage is done.

        Why up front: ``dequeue`` returns events oldest-first, so an unstorable
        event sits at the queue head and is batched with storable events behind
        it. The server 4xx-rejects the whole batch on account of the unstorable
        "poison", and ``_process_queue``'s whole-batch retry bump then increments
        EVERY event in the batch — the storable ones in lockstep with the poison.
        After ``max_retries`` cycles the storable events cross the ceiling and are
        dropped as "real lost activity" (the 2026-07 warnings: N real
        aw-watcher-input/window events dropped alongside "M other unstorable").
        Evicting the unstorable events first keeps every batch storable-only.

        Storable events (``bucket_id`` present AND timestamp within retention) are
        NEVER touched — a transient outage still holds them for retry. Each evicted
        row is MOVED (not hard-deleted) to ``dead_letter_events`` in one
        transaction, so nothing is silently lost.

        Returns a summary shaped like ``failed_event_summary`` so the caller can
        report it through the same path: ``{count, bucket_ids, oldest, newest,
        real_loss_count, unstorable_count}``. Bucketless / past-retention
        evictions are benign flushes (``unstorable``); a recent, routable event
        evicted here can only be over-long-duration — real activity the server
        rejected for size — so it counts as ``real_loss`` and the caller warns.
        All zero/empty when the queue holds nothing unstorable.

        ``now`` is injectable for deterministic tests (defaults to UTC now).
        """
        now = now or datetime.now(timezone.utc)
        stale_cutoff = now - timedelta(days=stale_after_days)
        dropped_at = now.isoformat()

        empty = {
            "count": 0,
            "bucket_ids": [],
            "oldest": None,
            "newest": None,
            "real_loss_count": 0,
            "unstorable_count": 0,
        }

        # immediate=True: same shape as remove_failed — the SELECT chooses which
        # rows get MOVED, so it belongs inside the write transaction. Two
        # concurrent evictions would otherwise both select the same event and
        # write it to dead_letter twice.
        with self._cursor(immediate=True) as cursor:
            cursor.execute(
                "SELECT id, event_data, created_at, retry_count, last_error "
                "FROM queued_events"
            )
            rows = cursor.fetchall()
            if not rows:
                return empty

            dead_rows = []
            evict_ids: list[int] = []
            bucket_ids: set[str] = set()
            timestamps: list[str] = []
            real_loss = 0
            unstorable = 0
            for row in rows:
                rid, raw, created_at, retry_count = row[0], row[1], row[2], row[3]
                # A reason already recorded against this row (a real 4xx the
                # server gave) outranks the eviction's generic string. Without
                # this an event that took a definitive rejection and LATER aged
                # past retention is dead-lettered as "unstorable — evicted",
                # silently overwriting the only evidence of why it failed.
                row_error = row[4]
                ev = None
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        ev = parsed
                except (json.JSONDecodeError, TypeError):
                    # Unparseable payload → can never be stored → evict it.
                    ev = None
                if is_event_storable(ev, stale_cutoff=stale_cutoff):
                    continue
                bucket_id = ev.get("bucket_id") if ev else None
                ts = ev.get("timestamp") if ev else None
                evict_ids.append(rid)
                if bucket_id:
                    bucket_ids.add(str(bucket_id))
                if ts:
                    timestamps.append(str(ts))
                # A recent, routable event reaching eviction can ONLY be failing
                # the duration bound (bucketless / past-retention are the benign
                # flushes). That's real recent activity the server rejected purely
                # for size — e.g. a >24h private_time span whose loss overbills the
                # window via trailing-grace — so surface it as loss (warning),
                # never a quiet "benign flush" info log.
                if bool(bucket_id) and _timestamp_within(ts, stale_cutoff):
                    real_loss += 1
                else:
                    unstorable += 1
                dead_rows.append(
                    (rid, raw, bucket_id, created_at, retry_count,
                     row_error or last_error, dropped_at)
                )

            if not evict_ids:
                return empty

            cursor.executemany(
                """
                INSERT INTO dead_letter_events
                    (original_id, event_data, bucket_id, created_at,
                     retry_count, last_error, dropped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                dead_rows,
            )
            placeholders = ",".join("?" * len(evict_ids))
            cursor.execute(
                f"DELETE FROM queued_events WHERE id IN ({placeholders})",
                evict_ids,
            )
            count = cursor.rowcount

        if count > 0:
            logger.info(
                "Evicted %d unstorable queued event(s) (no bucket / past "
                "retention / over-long duration) to dead_letter before batching",
                count,
            )
        timestamps.sort()
        return {
            "count": count,
            "bucket_ids": sorted(bucket_ids),
            "oldest": timestamps[0] if timestamps else None,
            "newest": timestamps[-1] if timestamps else None,
            "real_loss_count": real_loss,
            "unstorable_count": unstorable,
        }

    def backfill_status_bucket_ids(self, hostname: str) -> int:
        """One-time migration: give legacy status spans (idle/break/private/
        sleep) queued by <=1.5.95 a ``bucket_id`` so they stay storable under
        the bucket-keyed classifier instead of being evicted to dead-letter on
        the first post-upgrade cycle.

        Before betterflow-sync #129 these spans were emitted WITHOUT a
        ``bucket_id``. ``is_event_storable`` requires one, so a legacy span
        sitting in the queue at upgrade time would be classified unstorable and
        evicted — yet the server accepts it (it types the event off
        ``bucket_type``), so that eviction is a lost billing carve-out. This
        rewrites their ``event_data`` to carry the same ``bf-status_<host>`` id
        every current span already gets, making them first-class and
        deliverable.

        Idempotent and narrow: only touches events that have NO ``bucket_id``
        AND whose ``bucket_type`` ends in ``_time`` (the status-span signature),
        so real bucketed activity and anything unparseable is left untouched.
        Must run at startup BEFORE the first ``evict_unstorable``.

        Returns the number of events backfilled.
        """
        bucket_id = f"bf-status_{hostname}"
        with self._cursor() as cursor:
            cursor.execute("SELECT id, event_data FROM queued_events")
            rows = cursor.fetchall()
            updates: list[tuple[str, int]] = []
            for rid, raw in rows:
                try:
                    ev = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(ev, dict) or ev.get("bucket_id"):
                    continue
                bucket_type = ev.get("bucket_type")
                if not (isinstance(bucket_type, str) and bucket_type.endswith("_time")):
                    continue
                ev["bucket_id"] = bucket_id
                updates.append((json.dumps(ev), rid))
            if updates:
                cursor.executemany(
                    "UPDATE queued_events SET event_data = ? WHERE id = ?",
                    updates,
                )
        if updates:
            logger.info(
                "Backfilled bucket_id on %d legacy bucketless status span(s) so "
                "they deliver instead of being evicted on upgrade",
                len(updates),
            )
        return len(updates)

    def sanitize_project_ids(self) -> int:
        """Normalize queued ``project_id`` fields to the backend's integer FK.

        Release 1.5.104 stamped the active project onto SyncEngine-built events.
        If a stale/corrupt project payload put a UUID/string/zero in the queue,
        the backend could keep rejecting an otherwise valid status span until it
        exhausted retries and reported ``buckets=bf-status`` real loss. Queued
        events are account-local and the project id is optional, so the safe
        migration is: keep positive integer ids, coerce digit strings, and omit
        anything else before the first queue drain.

        Returns the number of queued rows rewritten.
        """
        with self._cursor() as cursor:
            cursor.execute("SELECT id, event_data FROM queued_events")
            rows = cursor.fetchall()
            updates: list[tuple[str, int]] = []
            for rid, raw in rows:
                try:
                    ev = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(ev, dict) or "project_id" not in ev:
                    continue
                normalized = normalized_project_id(ev.get("project_id"))
                if normalized is None:
                    ev.pop("project_id", None)
                else:
                    ev["project_id"] = normalized
                updated = json.dumps(ev)
                if updated != raw:
                    updates.append((updated, rid))
            if updates:
                cursor.executemany(
                    "UPDATE queued_events SET event_data = ? WHERE id = ?",
                    updates,
                )
        if updates:
            logger.info(
                "Sanitized project_id on %d queued event(s) before delivery",
                len(updates),
            )
        return len(updates)

    def dead_letter_count(self) -> int:
        """Number of events currently held in the dead-letter table."""
        with self._cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM dead_letter_events")
            return cursor.fetchone()[0]

    def get_dead_letter_events(self, limit: int = 100) -> list[dict]:
        """Return dead-lettered events (newest first) for inspection/replay.

        Read-only helper — does not modify the table.
        """
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT original_id, event_data, bucket_id, created_at,
                       retry_count, last_error, dropped_at
                FROM dead_letter_events
                ORDER BY dropped_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

    # A dead-lettered row must sit at least this long before the replay retries
    # it. Two reasons: (1) it keeps the replay from immediately re-resurrecting a
    # row that ``remove_failed`` JUST dropped for a genuine definitive 4xx —
    # without it, a poison-but-storable batch would drop→resurrect→drop every
    # cycle and re-block the queue head (defeating the head-of-line-block drop).
    # (2) the case the replay exists for — a brief bad-deploy / server-validation
    # window that 4xx-rejected good events — has passed by the time the cooldown
    # elapses, so the retry lands after the transient condition cleared. Longer
    # than the bad-deploy windows seen in the incident history (~1-2h would still
    # get a retry every cooldown until it succeeds or ages out of retention).
    _DEAD_LETTER_REPLAY_COOLDOWN_SECONDS = 1800  # 30 min
    # Replay scan budget. Terminal (past-retention) rows are skipped rather than
    # removed, so the scan has to be able to walk PAST them to reach recoverable
    # rows behind them — a fixed one-page window let 200 of them starve the
    # replay permanently. Page size keeps each query small; the max bounds
    # per-cycle cost on a large table. Parsing is the cost, ~1.5ms per 1000 rows.
    _REPLAY_SCAN_PAGE = 500
    _REPLAY_MAX_SCAN = 20000
    # Extra margin BEYOND retention before a row is MARKED terminal.
    # At retention+margin the server has refused the event for a fortnight and
    # always will, so the row no longer preserves recoverable ACTIVITY - only
    # the fact that some was lost, which the prune WARNING keeps. The margin
    # exists so anything a human might still be asked to inspect survives well
    # past the point it stopped being deliverable.
    _REPLAY_TERMINAL_MARGIN_DAYS = 7

    def _mark_terminal_rows(self, cursor, terminal_ids: list[int]) -> int:
        """Flag dead letters the server can never accept again. Never deletes.

        Called only from the replay scan, inside its transaction. A row reaching
        here has a timestamp that PARSED and is older than retention plus
        ``_REPLAY_TERMINAL_MARGIN_DAYS``, so the server has refused it for a
        fortnight and always will.

        Marking rather than deleting is the whole point. The row keeps its
        payload, its ``last_error`` and its ``dropped_at``, so it stays
        inspectable and stays counted by ``dead_letter_count`` — which is the
        only fleet-visible signal that this device lost activity. Deleting would
        have walked that number back down to zero, and a metric returning to
        zero reads as recovery rather than loss.

        A mis-marked row costs one missed replay attempt. A wrongly-deleted row
        costs the activity. Those are not the same mistake.
        """
        if not terminal_ids:
            return 0
        marked = 0
        for i in range(0, len(terminal_ids), 500):  # keep well under SQLITE_MAX_VARIABLE_NUMBER
            chunk = terminal_ids[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            cursor.execute(
                f"UPDATE dead_letter_events SET terminal = 1 "
                f"WHERE id IN ({placeholders})",
                chunk,
            )
            marked += len(chunk)
        logger.info(
            "Marked %d dead-lettered event(s) terminal (past retention + %dd; "
            "the server will never accept them). They are RETAINED and still "
            "counted — this only takes them out of the replay's candidate scan.",
            marked, self._REPLAY_TERMINAL_MARGIN_DAYS,
        )
        return marked

    def requeue_storable_dead_letter(
        self,
        *,
        limit: int = 200,
        now: Optional[datetime] = None,
        stale_after_days: int = EVENT_RETENTION_DAYS,
        min_dead_age_seconds: Optional[float] = None,
    ) -> dict:
        """Resurrect dead-lettered rows that are STORABLE again — move them back
        into the live queue for another delivery attempt.

        Dead-lettering preserves rejected events (``remove_failed`` /
        ``evict_unstorable``) but nothing re-enqueued them, so real activity that
        hit a transient-looking-definitive 4xx (a server-side validation bug, a
        brief bad-deploy window) sat in ``dead_letter_events`` forever. This is
        the bounded, conservative replay path.

        A row is eligible only if it is storable NOW by the shared
        ``is_event_storable`` — the SAME function (not "the same rule") that
        ``evict_unstorable``, ``failed_event_summary`` and
        ``_batch_has_storable_activity`` call, so the replay can never resurrect
        something eviction would immediately remove again. That requires a
        ``bucket_id`` to route to, a ``timestamp`` still within the server's
        retention window (``stale_after_days``), AND a ``duration`` inside the
        server's accepted 0..``MAX_EVENT_DURATION_SECONDS`` bounds. Rows with no
        bucket, an unparseable payload, an over-long duration, or a timestamp
        that has since aged past retention are genuinely unstorable and are LEFT
        in the dead-letter table — never resurrected into a batch the server
        would only reject again.

        Conservative by construction:
        - **Cooldown** — a row is skipped until it has sat in the dead-letter
          table for ``min_dead_age_seconds`` (default
          ``_DEAD_LETTER_REPLAY_COOLDOWN_SECONDS``). This stops the replay from
          instantly re-resurrecting a row ``remove_failed`` just dropped for a
          genuine definitive rejection (which would re-block the queue head and
          thrash drop→resurrect→drop every cycle), and gives a transient
          server-side condition time to clear before the retry.
        - **Bounded** — at most ``limit`` rows per call (oldest dead-letter
          first), so a large table can't flood one cycle.
        - **Capacity-respecting** — bounded a second time by the live queue's
          remaining headroom (``max_size`` minus current size), so the replay
          can approach ``max_size`` but never cross it. It does NOT reuse
          ``enqueue``'s oldest-eviction: evicting live, never-rejected events to
          make room for already-rejected ones is the wrong trade for billed
          time. Rows that don't fit stay dead-lettered for a later cycle.
        - **MOVE, not copy** — the candidate SELECT, the ``queued_events``
          INSERT and the ``dead_letter_events`` DELETE all run inside ONE
          ``BEGIN IMMEDIATE`` transaction, so a row can never be resurrected
          twice (no double-send), and a crash can't both drop it from the
          dead-letter table and fail to re-enqueue it. The SELECT has to be
          inside it: pysqlite's implicit transaction does not open until the
          first DML, so a plain ``_cursor()`` left the row-choosing read in
          autocommit and two threads could both select the same ids and both
          INSERT a copy. Reachable via ``main._acquire_sync_slot``, which
          abandons a wedged cycle after 420s and starts a fresh ``_do_sync``
          while the zombie is still inside ``_process_queue``.
        - **Fresh retry budget** — the row re-enters with ``retry_count`` reset
          to 0 (the default), so it isn't instantly re-dropped at the ceiling.
        - **Self-limiting churn** — a genuinely-poison row that keeps getting
          re-rejected is retried at most once per cooldown, and only until its
          timestamp ages past the retention window, after which it's classified
          unstorable and left alone. The server also upserts by event id, so a
          resurrected event that was actually already stored is deduped, not
          duplicated.

        Returns ``{examined, requeued, skipped_unstorable}`` — ``examined``
        counts only rows past the cooldown (younger rows aren't looked at).

        ``now`` is injectable for deterministic tests (defaults to UTC now).
        """
        now = now or datetime.now(timezone.utc)
        if min_dead_age_seconds is None:
            min_dead_age_seconds = self._DEAD_LETTER_REPLAY_COOLDOWN_SECONDS
        stale_cutoff = now - timedelta(days=stale_after_days)
        # Anchored to the module constant, NOT stale_after_days — see the
        # marking branch below for why the caller's window must not move it.
        terminal_cutoff = now - timedelta(
            days=EVENT_RETENTION_DAYS + self._REPLAY_TERMINAL_MARGIN_DAYS
        )
        cooldown_cutoff = (now - timedelta(seconds=min_dead_age_seconds)).isoformat()
        requeued_at = now.isoformat()

        # immediate=True: the SELECT below CHOOSES the rows the INSERT/DELETE act
        # on, so it must be inside the write transaction, not ahead of it.
        with self._cursor(immediate=True) as cursor:
            # Capacity first, and read INSIDE this transaction so it can't go
            # stale before the INSERT below acts on it. ``enqueue`` enforces
            # max_size by evicting the OLDEST queued events; this raw-INSERT
            # path bypassed that entirely, and dead_letter_events is never
            # pruned — so on the first post-upgrade drain a device carrying
            # weeks of dead letters resurrects up to ``limit`` rows per cycle
            # straight past max_size, and capacity_percent() pins at 1.0 for
            # good, killing every capacity signal built on it.
            #
            # We deliberately do NOT reuse enqueue()'s oldest-eviction here.
            # That would delete LIVE, never-rejected events to make room for
            # ALREADY-REJECTED ones — and resurrected rows re-enter with a fresh
            # created_at, so they would be the newest rows and the live events
            # would be the ones evicted. Strictly the wrong trade for billed
            # time. Instead the replay takes only the headroom that exists: it
            # can approach max_size but never cross it, and the rows that don't
            # fit stay preserved in dead_letter_events for a later cycle rather
            # than costing anything. (Keeping the capacity read here rather than
            # calling enqueue() also preserves the MOVE atomicity — enqueue()
            # opens its own _cursor()/transaction, which would split the INSERT
            # from the dead-letter DELETE and reintroduce the double-send this
            # function's BEGIN IMMEDIATE exists to close.)
            cursor.execute("SELECT COUNT(*) FROM queued_events")
            headroom = self.max_size - cursor.fetchone()[0]
            if headroom <= 0:
                logger.info(
                    "Dead-letter replay skipped: live queue at capacity "
                    "(%d/%d). Preserved rows stay dead-lettered for a later "
                    "cycle — never dropped to make room.",
                    self.max_size - headroom, self.max_size,
                )
                return {"examined": 0, "requeued": 0, "skipped_unstorable": 0,
                        "marked_terminal": 0}
            effective_limit = min(limit, headroom)

            # Only rows that have sat past the cooldown are candidates. The
            # lexical comparison on ISO-8601 strings is chronological (every
            # dropped_at is a tz-aware UTC isoformat), matching set_checkpoint_forward.
            # Scan PAST terminal rows instead of letting them own the window.
            #
            # This was a single `ORDER BY dropped_at ASC LIMIT effective_limit`.
            # A row whose timestamp has aged past retention can never become
            # storable again — wall-clock only moves forward — and the check
            # below SKIPS such a row without removing it, so it holds its slot
            # in that ordering permanently. Once a device carried `limit` of
            # them the replay examined the same terminal rows every cycle and
            # resurrected nothing; any genuinely recoverable event dead-lettered
            # afterwards then aged out of retention and was lost for real.
            # Measured on the pre-fix code at the default limit=200:
            #     terminal=199 -> requeued 1      terminal=200 -> requeued 0
            # Silent loss of billable activity, on the one path whose entire
            # purpose is to preserve it.
            #
            # Keyset pagination over the same ordering (not OFFSET, which
            # rescans from the start on every page), stopping as soon as enough
            # storable rows are found or the scan budget is spent. The budget
            # bounds per-cycle cost on a large table.
            move_ids: list[int] = []
            new_rows: list[tuple] = []
            terminal_ids: list[int] = []
            examined = 0
            cursor_key = None
            while len(new_rows) < effective_limit and examined < self._REPLAY_MAX_SCAN:
                page = min(self._REPLAY_SCAN_PAGE, self._REPLAY_MAX_SCAN - examined)
                if cursor_key is None:
                    cursor.execute(
                        """
                        SELECT id, event_data, dropped_at FROM dead_letter_events
                        WHERE dropped_at <= ? AND terminal = 0
                        ORDER BY dropped_at ASC, id ASC
                        LIMIT ?
                        """,
                        (cooldown_cutoff, page),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT id, event_data, dropped_at FROM dead_letter_events
                        WHERE dropped_at <= ? AND terminal = 0
                          AND (dropped_at, id) > (?, ?)
                        ORDER BY dropped_at ASC, id ASC
                        LIMIT ?
                        """,
                        (cooldown_cutoff, cursor_key[0], cursor_key[1], page),
                    )
                rows = cursor.fetchall()
                if not rows:
                    break
                cursor_key = (rows[-1][2], rows[-1][0])
                examined += len(rows)
                for row in rows:
                    if len(new_rows) >= effective_limit:
                        break
                    dead_id, raw = row[0], row[1]
                    try:
                        ev = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        # Unparseable -> can never be stored -> leave it
                        # dead-lettered. is_event_storable also returns False
                        # for a non-dict, so this None needs no separate branch.
                        ev = None
                    # THE shared classifier — never a local re-implementation.
                    # A hand-rolled `bucket_id AND timestamp-in-window` pair
                    # (what this was) silently omits the duration bound, so the
                    # exact poison ``evict_unstorable`` had just removed — an
                    # over-long weekend lid-close span — was resurrected a
                    # cooldown later, AFTER that cycle's eviction gate had run.
                    # It then 4xx-rejected the whole batch it rode in, and
                    # _process_queue's whole-batch retry bump dragged its
                    # healthy neighbours to the drop ceiling; the re-eviction
                    # restamped ``dropped_at``, restarting the cooldown, so the
                    # loop repeated every 30 min for the full retention window.
                    if not is_event_storable(ev, stale_cutoff=stale_cutoff):
                        # Mark - never delete - a row the server can never
                        # accept again, so it leaves the candidate ordering
                        # instead of holding its slot forever.
                        #
                        # The predicate must be POSITIVE: parse the timestamp,
                        # require the parse to SUCCEED, then require it to be
                        # old. `not _timestamp_within(...)` is not that test —
                        # it returns False for a missing, null, wrong-typed or
                        # unparseable timestamp too, so "provably ancient" and
                        # "age unreadable" collapse into one branch. Measured on
                        # the first version of this fix: six rows with
                        # unreadable timestamps were marked terminal, including
                        # one captured an hour earlier, because the code had no
                        # evidence of their age at all and acted anyway.
                        #
                        # An unreadable timestamp is the likely shape of a
                        # serialisation bug and must stay visible and
                        # replayable, so it is deliberately NOT marked.
                        #
                        # Anchored to EVENT_RETENTION_DAYS, never to the
                        # caller's stale_after_days: a caller may legitimately
                        # shrink the window for a SKIP (reversible), and must
                        # not thereby widen a durable mark.
                        if is_permanently_unstorable(
                            ev, terminal_cutoff=terminal_cutoff
                        ):
                            terminal_ids.append(dead_id)
                        continue
                    move_ids.append(dead_id)
                    # Re-enter with a fresh created_at and retry_count 0.
                    new_rows.append((raw, requeued_at))

            if examined == 0:
                return {"examined": 0, "requeued": 0, "skipped_unstorable": 0,
                        "marked_terminal": 0}

            marked = self._mark_terminal_rows(cursor, terminal_ids)

            if not move_ids:
                return {
                    "examined": examined,
                    "requeued": 0,
                    "skipped_unstorable": examined,
                    "marked_terminal": marked,
                }

            cursor.executemany(
                "INSERT INTO queued_events (event_data, created_at) VALUES (?, ?)",
                new_rows,
            )
            placeholders = ",".join("?" * len(move_ids))
            cursor.execute(
                f"DELETE FROM dead_letter_events WHERE id IN ({placeholders})",
                move_ids,
            )
            requeued = len(move_ids)

        if requeued > 0:
            logger.info(
                "Resurrected %d storable dead-lettered event(s) back into the "
                "queue for another delivery attempt",
                requeued,
            )
        return {
            "examined": examined,
            "requeued": requeued,
            "skipped_unstorable": examined - requeued,
            "marked_terminal": marked,
        }

    def size(self) -> int:
        """Get the current queue size."""
        with self._cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM queued_events")
            return cursor.fetchone()[0]

    def is_empty(self) -> bool:
        """Check if queue is empty."""
        return self.size() == 0

    def capacity_percent(self) -> float:
        """Get current queue capacity as a percentage (0.0 to 1.0)."""
        return min(self.size() / self.max_size, 1.0)

    def is_near_capacity(self, threshold: float = 0.8) -> bool:
        """Check if queue is approaching capacity.

        Args:
            threshold: Capacity threshold (default 80%)

        Returns:
            True if queue is at or above threshold
        """
        return self.capacity_percent() >= threshold

    def expire_old(self, max_age_days: int = 30) -> int:
        """Remove events older than max_age_days.

        Args:
            max_age_days: Maximum age in days

        Returns:
            Number of events removed
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        with self._cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM queued_events
                WHERE created_at < ?
                """,
                (cutoff.isoformat(),),
            )
            count = cursor.rowcount
            if count > 0:
                logger.info(f"Expired {count} queue events older than {max_age_days} days")
            return count

    def clear(self) -> int:
        """Clear all events from the queue."""
        with self._cursor() as cursor:
            cursor.execute("DELETE FROM queued_events")
            return cursor.rowcount

    # Checkpoint management

    def get_checkpoint(self, bucket_id: str) -> Optional[datetime]:
        """Get the last sync timestamp for a bucket.

        Args:
            bucket_id: The ActivityWatch bucket ID

        Returns:
            Last synced timestamp, or None if never synced
        """
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT last_timestamp FROM sync_checkpoints
                WHERE bucket_id = ?
                """,
                (bucket_id,),
            )
            row = cursor.fetchone()
            if row:
                return datetime.fromisoformat(row[0])
            return None

    def set_checkpoint(
        self, bucket_id: str, timestamp: datetime, event_id: Optional[int] = None
    ) -> None:
        """Set the sync checkpoint for a bucket.

        Args:
            bucket_id: The ActivityWatch bucket ID
            timestamp: Last synced timestamp
            event_id: Optional last event ID
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO sync_checkpoints (bucket_id, last_event_id, last_timestamp, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(bucket_id) DO UPDATE SET
                    last_event_id = excluded.last_event_id,
                    last_timestamp = excluded.last_timestamp,
                    updated_at = excluded.updated_at
                """,
                (bucket_id, event_id, timestamp.isoformat(), now),
            )

    def set_checkpoint_forward(
        self, bucket_id: str, timestamp: datetime, event_id: Optional[int] = None
    ) -> None:
        """Advance a bucket's checkpoint, but only forward (monotonic).

        Identical to ``set_checkpoint`` except the write is a no-op when the
        bucket already has a checkpoint at or after ``timestamp``. The guard is
        a ``WHERE`` on the existing row inside a single statement, so two sync
        threads can't race a checkpoint backward (no read-then-write). Use this
        on the normal per-cycle advance path; reserve the raw ``set_checkpoint``
        for intentional resets (first-sync seed, pause skip-forward).

        The comparison is lexical on the stored ISO-8601 strings. Every writer
        passes a tz-aware UTC datetime (``isoformat()`` -> ``...+00:00``), so the
        fixed-width prefix compares chronologically; sub-second format
        differences only ever bias toward NOT rewinding, which is the safe
        direction.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO sync_checkpoints (bucket_id, last_event_id, last_timestamp, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(bucket_id) DO UPDATE SET
                    last_event_id = excluded.last_event_id,
                    last_timestamp = excluded.last_timestamp,
                    updated_at = excluded.updated_at
                WHERE excluded.last_timestamp > sync_checkpoints.last_timestamp
                """,
                (bucket_id, event_id, timestamp.isoformat(), now),
            )

    def get_all_checkpoints(self) -> dict[str, datetime]:
        """Get all sync checkpoints."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT bucket_id, last_timestamp FROM sync_checkpoints
                """
            )
            return {
                row["bucket_id"]: datetime.fromisoformat(row["last_timestamp"])
                for row in cursor.fetchall()
            }

    # Per-event counted-time persistence (restart-safe time dedup)

    def get_counted_times(self, day: str) -> dict[tuple[str, str], float]:
        """Return all counted active-seconds for a given local day.

        Args:
            day: Local date in ISO format (``YYYY-MM-DD``).

        Returns:
            Dict mapping ``(bucket_id, event_id)`` -> counted_seconds. Used to
            repopulate the in-memory time-dedup cache on startup so replayed
            events are not double-counted into the daily total.
        """
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT bucket_id, event_id, counted_seconds FROM counted_time
                WHERE day = ?
                """,
                (day,),
            )
            return {
                (row["bucket_id"], row["event_id"]): float(row["counted_seconds"])
                for row in cursor.fetchall()
            }

    def set_counted_time(
        self, bucket_id: str, event_id: str, counted_seconds: float, day: str
    ) -> None:
        """Upsert the cumulative counted active-seconds for one event.

        Idempotent: re-counting the same event with the same total is a no-op
        for the daily figure because the caller only adds the positive delta.

        Args:
            bucket_id: ActivityWatch bucket ID.
            event_id: ActivityWatch event ID (stringified).
            counted_seconds: Cumulative seconds counted for this event so far.
            day: Local date the time belongs to (ISO ``YYYY-MM-DD``).
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO counted_time
                    (bucket_id, event_id, day, counted_seconds, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(bucket_id, event_id) DO UPDATE SET
                    counted_seconds = excluded.counted_seconds,
                    day = excluded.day,
                    updated_at = excluded.updated_at
                """,
                (bucket_id, event_id, day, float(counted_seconds), now),
            )

    def prune_counted_time(self, before_day: str) -> int:
        """Delete counted-time rows for days strictly before ``before_day``.

        Keeps the table bounded — only recent days are ever replayed.

        Args:
            before_day: Local date in ISO format; rows with ``day < before_day``
                are removed.

        Returns:
            Number of rows deleted.
        """
        with self._cursor() as cursor:
            cursor.execute(
                "DELETE FROM counted_time WHERE day < ?",
                (before_day,),
            )
            return cursor.rowcount

    # App category management

    def get_category(self, app_name: str) -> Optional[str]:
        """Get the category for an app.

        Args:
            app_name: Application name

        Returns:
            Category string, or None if not mapped
        """
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT category FROM app_categories WHERE app_name = ?",
                (app_name,),
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def get_all_categories(self) -> dict[str, str]:
        """Get all app-to-category mappings.

        Returns:
            Dict mapping app_name -> category
        """
        with self._cursor() as cursor:
            cursor.execute("SELECT app_name, category FROM app_categories")
            return {row[0]: row[1] for row in cursor.fetchall()}

    def set_category(
        self, app_name: str, category: str, source: str = "server"
    ) -> None:
        """Set or update a single app category.

        User overrides (source='user') are never clobbered by server or
        fallback writes.  Priority: user > server > fallback.

        Args:
            app_name: Application name
            category: Category string
            source: 'server', 'user', or 'fallback'
        """
        if not app_name or not category:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO app_categories (app_name, category, source, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(app_name) DO UPDATE SET
                    category = CASE
                        WHEN app_categories.source = 'user' THEN app_categories.category
                        ELSE excluded.category
                    END,
                    source = CASE
                        WHEN app_categories.source = 'user' THEN 'user'
                        ELSE excluded.source
                    END,
                    updated_at = excluded.updated_at
                """,
                (app_name, category, source, now),
            )

    def close(self) -> None:
        """Permanently close all tracked database connections.

        After close(), any call to _get_connection() will raise
        sqlite3.ProgrammingError.  The queue cannot be reopened.
        """
        with self._connections_lock:
            self._closed = True  # inside lock to prevent new connections
            for conn in self._connections:
                try:
                    conn.close()
                except Exception as e:
                    logger.debug("OfflineQueue conn.close() raised: %s", e)
            self._connections.clear()
        # Clear the calling thread's reference. Other threads calling into
        # _get_connection will raise sqlite3.ProgrammingError immediately
        # via the `if self._closed` guard — they do NOT fall through to the
        # stale-handle replacement path (that path only handles open-but-stale
        # handles after a previous close, when the queue is then reopened).
        if hasattr(self._local, "connection"):
            del self._local.connection
