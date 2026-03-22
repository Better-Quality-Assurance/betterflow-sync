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
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection.

        Recreates the connection if the queue was closed and reopened,
        or if this thread's handle was closed by close().
        """
        need_new = not hasattr(self._local, "connection")
        if not need_new:
            # Check if existing connection was closed
            try:
                self._local.connection.execute("SELECT 1")
            except (sqlite3.ProgrammingError, sqlite3.OperationalError):
                del self._local.connection
                need_new = True
        if need_new:
            with self._connections_lock:
                if self._closed:
                    raise sqlite3.ProgrammingError("OfflineQueue has been closed")
                conn = sqlite3.connect(str(self.db_path))
                conn.row_factory = sqlite3.Row
                self._connections.append(conn)
                self._local.connection = conn  # assign inside lock
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

    def check_integrity(self) -> bool:
        """Run periodic SQLite integrity check (N2).

        Returns True if database is healthy, False if corruption detected.
        Automatically resets the database on corruption.
        """
        now = time.monotonic()
        if now - self._last_integrity_check < self._integrity_check_interval:
            return True
        try:
            with self._cursor() as cursor:
                cursor.execute("PRAGMA quick_check")
                result = cursor.fetchone()
                if result[0] != "ok":
                    logger.error(f"SQLite quick_check failed: {result[0]}")
                    return False
            self._last_integrity_check = now  # only advance on success
            return True
        except sqlite3.DatabaseError as e:
            logger.error(f"SQLite integrity check error: {e}")
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
            return [
                e for e in (QueuedEvent.from_row(tuple(row)) for row in cursor.fetchall())
                if e is not None
            ]

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

    def remove_failed(self, max_retries: int = 5) -> int:
        """Remove events that have exceeded max retries.

        Args:
            max_retries: Maximum retry attempts

        Returns:
            Number of events removed
        """
        with self._cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM queued_events
                WHERE retry_count >= ?
                """,
                (max_retries,),
            )
            count = cursor.rowcount
            if count > 0:
                logger.warning(f"Removed {count} events that exceeded max retries")
            return count

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
        return self.size() / self.max_size

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

    def sync_categories(self, mappings: dict[str, str]) -> None:
        """Bulk replace server-sourced categories, preserving user overrides.

        Args:
            mappings: Dict mapping app_name -> category from server
        """
        if not mappings:
            # Empty server response means "no opinion" - leave existing entries.
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cursor:
            # Clear server and fallback entries; user overrides survive because
            # they have source='user' and the DELETE only targets the other two.
            cursor.execute(
                "DELETE FROM app_categories WHERE source IN ('server', 'fallback')"
            )
            # Insert new server categories.  Any remaining conflict is a 'user'
            # row, which we preserve via DO NOTHING.
            cursor.executemany(
                """
                INSERT INTO app_categories (app_name, category, source, updated_at)
                VALUES (?, ?, 'server', ?)
                ON CONFLICT(app_name) DO NOTHING
                """,
                [(app, cat, now) for app, cat in mappings.items()],
            )

    def close(self) -> None:
        """Close all tracked database connections.

        After close(), any thread calling _get_connection() will create
        a fresh connection (the old closed handles are removed from tracking).
        """
        with self._connections_lock:
            self._closed = True  # inside lock to prevent new connections
            for conn in self._connections:
                try:
                    conn.close()
                except Exception:
                    pass
            self._connections.clear()
        # Clear the calling thread's reference; other threads will get
        # a sqlite3.ProgrammingError on next use of their stale handle,
        # which _get_connection detects via hasattr and replaces.
        if hasattr(self._local, "connection"):
            del self._local.connection
