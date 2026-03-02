"""Daily time tracker for tracking active work time per day."""

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

try:
    from ..config import Config
except ImportError:
    from config import Config

__all__ = ["DailyTimeTracker"]

logger = logging.getLogger(__name__)


class DailyTimeTracker:
    """Tracks cumulative active time per day, persisted to SQLite.

    Only "active" events (engaged work) count toward the daily total.
    The tracker survives app restarts and handles day rollovers at midnight
    in the local timezone.

    Usage:
        tracker = DailyTimeTracker()
        tracker.add_active_time(45.5, date.today())
        total = tracker.get_today_active_time()
    """

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize the tracker.

        Args:
            db_path: Path to SQLite database file. Defaults to data dir.
        """
        if db_path is None:
            db_path = Config.get_data_dir() / "daily_time.db"

        self._db_path = db_path
        self._local = threading.local()
        self._today: Optional[date] = None
        self._today_seconds: float = 0.0
        self._lock = threading.Lock()

        self._init_db()
        self._load()

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, "connection"):
            self._local.connection = sqlite3.connect(str(self._db_path))
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
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        with self._cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_active_time (
                    date TEXT PRIMARY KEY,
                    active_seconds REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _load(self) -> None:
        """Load today's data from SQLite on init."""
        today = self._get_local_date()
        self._today = today

        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT active_seconds FROM daily_active_time
                WHERE date = ?
                """,
                (today.isoformat(),),
            )
            row = cursor.fetchone()
            if row:
                self._today_seconds = float(row["active_seconds"])
            else:
                self._today_seconds = 0.0

        logger.debug(
            f"Loaded daily time for {today}: {self._today_seconds:.1f}s"
        )

    def _get_local_date(self) -> date:
        """Get the current local date.

        Returns the date in the local timezone, which determines when
        the daily counter resets (at local midnight).
        """
        return datetime.now().date()

    def add_active_time(self, seconds: float, event_date: date) -> None:
        """Add active time for a given date.

        If the date is different from the currently tracked day, resets
        and starts tracking the new day.

        Args:
            seconds: Duration in seconds to add.
            event_date: The date this time belongs to.
        """
        if seconds <= 0:
            return

        with self._lock:
            # Check for day rollover
            if self._today != event_date:
                self._reset_for_new_day(event_date)

            self._today_seconds += seconds
            self._persist()

    def get_today_active_time(self) -> timedelta:
        """Get cumulative active time for today.

        Handles day rollover if we've passed midnight since last check.

        Returns:
            timedelta with today's total active time.
        """
        with self._lock:
            self._check_day_rollover()
            return timedelta(seconds=self._today_seconds)

    def get_active_time_for_date(self, target_date: date) -> timedelta:
        """Get active time for a specific date.

        Args:
            target_date: The date to query.

        Returns:
            timedelta with the total active time for that date.
        """
        with self._lock:
            # If querying today, return in-memory value for consistency
            if target_date == self._today:
                return timedelta(seconds=self._today_seconds)

            with self._cursor() as cursor:
                cursor.execute(
                    """
                    SELECT active_seconds FROM daily_active_time
                    WHERE date = ?
                    """,
                    (target_date.isoformat(),),
                )
                row = cursor.fetchone()
                if row:
                    return timedelta(seconds=float(row["active_seconds"]))
                return timedelta(seconds=0)

    def _reset_for_new_day(self, new_date: date) -> None:
        """Reset counter for new day.

        Persists any existing data before switching to the new date.

        Args:
            new_date: The new date to track.
        """
        logger.info(
            f"Day rollover: {self._today} ({self._today_seconds:.1f}s) -> {new_date}"
        )
        self._today = new_date

        # Load any existing data for the new date
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT active_seconds FROM daily_active_time
                WHERE date = ?
                """,
                (new_date.isoformat(),),
            )
            row = cursor.fetchone()
            if row:
                self._today_seconds = float(row["active_seconds"])
            else:
                self._today_seconds = 0.0

    def _check_day_rollover(self) -> None:
        """Check if we need to roll over to a new day."""
        current_date = self._get_local_date()
        if self._today != current_date:
            self._reset_for_new_day(current_date)

    def _persist(self) -> None:
        """Save current state to SQLite."""
        if self._today is None:
            return

        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO daily_active_time (date, active_seconds, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    active_seconds = excluded.active_seconds,
                    updated_at = excluded.updated_at
                """,
                (self._today.isoformat(), self._today_seconds, now),
            )

    def close(self) -> None:
        """Close the database connection."""
        if hasattr(self._local, "connection"):
            self._local.connection.close()
            del self._local.connection
