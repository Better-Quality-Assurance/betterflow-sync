"""Offline queue for storing events when BetterFlow is unreachable."""

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from ..config import Config, MAX_QUEUE_SIZE

logger = logging.getLogger(__name__)


@dataclass
class QueuedEvent:
    """An event stored in the offline queue."""

    id: int
    event_data: dict
    created_at: datetime
    retry_count: int = 0

    @classmethod
    def from_row(cls, row: tuple) -> "QueuedEvent":
        """Create from database row."""
        return cls(
            id=row[0],
            event_data=json.loads(row[1]),
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
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, "connection"):
            self._local.connection = sqlite3.connect(str(self.db_path))
            self._local.connection.row_factory = sqlite3.Row
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

        with self._cursor() as cursor:
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

        # Check if we need to make room
        current_size = self.size()
        if current_size + len(events) > self.max_size:
            # Remove oldest events to make room
            to_remove = current_size + len(events) - self.max_size
            self._remove_oldest(to_remove)
            logger.warning(f"Queue full, removed {to_remove} oldest events")

        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO queued_events (event_data, created_at)
                VALUES (?, ?)
                """,
                [(json.dumps(e), now) for e in events],
            )
            return cursor.rowcount

    def dequeue(self, batch_size: int = 100) -> list[QueuedEvent]:
        """Get a batch of events from the queue (oldest first).

        Args:
            batch_size: Maximum number of events to return

        Returns:
            List of QueuedEvent objects
        """
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT id, event_data, created_at, retry_count
                FROM queued_events
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (batch_size,),
            )
            return [QueuedEvent.from_row(tuple(row)) for row in cursor.fetchall()]

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

    def _remove_oldest(self, count: int) -> int:
        """Remove the oldest events from the queue."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM queued_events
                WHERE id IN (
                    SELECT id FROM queued_events
                    ORDER BY created_at ASC
                    LIMIT ?
                )
                """,
                (count,),
            )
            return cursor.rowcount

    def size(self) -> int:
        """Get the current queue size."""
        with self._cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM queued_events")
            return cursor.fetchone()[0]

    def is_empty(self) -> bool:
        """Check if queue is empty."""
        return self.size() == 0

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

    def close(self) -> None:
        """Close the database connection."""
        if hasattr(self._local, "connection"):
            self._local.connection.close()
            del self._local.connection
