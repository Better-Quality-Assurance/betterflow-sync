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

__all__ = ["OfflineQueue", "QueuedEvent"]

logger = logging.getLogger(__name__)


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
        except (json.JSONDecodeError, TypeError):
            logger.error(f"[queue] Corrupt event row id={row[0]}, discarding")
            return None
        return cls(
            id=row[0],
            event_data=event_data,
            created_at=datetime.fromisoformat(row[2]),
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
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        """Context manager for database cursor."""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
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
                    retry_count INTEGER DEFAULT 0
                )
                """
            )
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
                    dropped_at TEXT NOT NULL
                )
                """
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
        with self._cursor() as cursor:
            # Atomic: check size + evict + insert in a single transaction
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

    def increment_retry(self, event_ids: list[int]) -> None:
        """Increment retry count for events.

        Args:
            event_ids: List of event IDs to update
        """
        if not event_ids:
            return

        with self._cursor() as cursor:
            placeholders = ",".join("?" * len(event_ids))
            cursor.execute(
                f"""
                UPDATE queued_events
                SET retry_count = retry_count + 1
                WHERE id IN ({placeholders})
                """,
                event_ids,
            )

    def failed_event_summary(
        self,
        max_retries: int = 5,
        *,
        now: Optional[datetime] = None,
        stale_after_days: int = 7,
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
        unstorable_count} — all zero/empty when clean.

        ``now`` is injectable for deterministic tests (defaults to UTC now).
        """
        now = now or datetime.now(timezone.utc)
        stale_cutoff = now - timedelta(days=stale_after_days)

        with self._cursor() as cursor:
            cursor.execute(
                "SELECT event_data FROM queued_events WHERE retry_count >= ?",
                (max_retries,),
            )
            rows = cursor.fetchall()

        bucket_ids: set[str] = set()
        timestamps: list[str] = []
        real_loss = 0
        unstorable = 0
        for row in rows:
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
            # A drop is real loss only if it was actually storable: it has a
            # bucket to route to AND its timestamp is within the retention window.
            recent = bid and self._timestamp_within(ts, stale_cutoff)
            if recent:
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
        }

    @staticmethod
    def _timestamp_within(ts: Optional[str], cutoff: datetime) -> bool:
        """True when ``ts`` (ISO-8601) parses and is at/after ``cutoff``. A
        missing or unparseable timestamp is treated as NOT within (unstorable)."""
        if not ts:
            return False
        try:
            parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed >= cutoff

    def remove_failed(
        self, max_retries: int = 5, *, last_error: Optional[str] = None
    ) -> int:
        """Move events that have exceeded max retries to the dead-letter table.

        Previously this HARD-DELETED exhausted events, permanently losing real
        activity that hit a definitive 4xx (transient 5xx/timeout don't increment
        retry_count, so anything here is a genuine rejection — but still real
        aw-watcher-afk / aw-watcher-input data worth keeping). Now each row is
        MOVED to ``dead_letter_events`` with its event_data, bucket_id (parsed
        out for queryability), created_at, retry_count, an optional last_error,
        and a dropped_at stamp — nothing is silently lost.

        The INSERT and the matching DELETE run inside the SAME ``_cursor()``
        transaction (committed atomically on exit), so a crash can never both
        drop the events from the queue AND fail to record the dead letter. The
        DELETE targets the exact ids that were moved (not the ``>= max_retries``
        predicate) so a concurrent writer that pushes another event over the
        ceiling between the SELECT and the DELETE can't have it deleted without
        first being dead-lettered.

        Args:
            max_retries: Maximum retry attempts
            last_error: Optional context recorded on each dead-lettered row

        Returns:
            Number of events moved out of the queue
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT id, event_data, created_at, retry_count
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
                dead_rows.append(
                    (row[0], row[1], bucket_id, row[2], row[3], last_error, now)
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
