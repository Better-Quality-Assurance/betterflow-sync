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
        self._all_connections: list[sqlite3.Connection] = []
        self._conn_lock = threading.Lock()
        self._closed = False
        self._today: Optional[date] = None
        self._today_seconds: float = 0.0
        self._lock = threading.Lock()

        self._init_db()
        self._load()

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection.

        Recreates the connection if it was closed by close().
        Raises sqlite3.ProgrammingError if close() has been called.
        """
        with self._conn_lock:
            if self._closed:
                raise sqlite3.ProgrammingError("DailyTimeTracker has been closed")

        if hasattr(self._local, "connection"):
            try:
                self._local.connection.execute("SELECT 1")
            except (sqlite3.ProgrammingError, sqlite3.OperationalError):
                del self._local.connection
        if not hasattr(self._local, "connection"):
            conn = sqlite3.connect(str(self._db_path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            # WAL + a generous busy timeout so concurrent writers from multiple
            # threads serialize gracefully instead of raising "database is
            # locked" (observed on Windows under contention). Mirrors OfflineQueue.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            with self._conn_lock:
                if self._closed:
                    conn.close()
                    raise sqlite3.ProgrammingError("DailyTimeTracker has been closed")
                self._all_connections.append(conn)
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

        Only the current local date is kept as the in-memory "today" counter.
        Replayed historical events are persisted to their own date without
        moving the live counter backward.

        Args:
            seconds: Duration in seconds to add.
            event_date: The date this time belongs to.
        """
        if seconds <= 0:
            return

        # Shutdown race: if close() ran while a sync cycle was in flight,
        # _get_connection raises sqlite3.ProgrammingError. Skip silently —
        # the in-flight delta is small (one sync interval) and forcing a
        # spurious sync error on app quit is worse than losing it.
        with self._conn_lock:
            if self._closed:
                return

        current_date = self._get_local_date()
        if event_date != current_date:
            try:
                self._increment_date(event_date, seconds)
            except sqlite3.ProgrammingError:
                logger.debug("DailyTimeTracker closed mid-write — skipping persist")
            return

        rollover_date = None
        rollover_seconds = 0.0
        with self._lock:
            # Check for day rollover
            if self._today != current_date:
                # Capture old day's data for out-of-lock persist
                rollover_date = self._today
                rollover_seconds = self._today_seconds
                # Swap in-memory state immediately
                self._today = current_date
                self._today_seconds = 0.0

            self._today_seconds += seconds

        # All SQLite I/O happens outside the lock
        try:
            if rollover_date is not None:
                self._persist_date(rollover_date, rollover_seconds)
                self._load_new_day(current_date)
            self._persist()
        except sqlite3.ProgrammingError:
            # close() raced with us between the _closed check above and the
            # actual DB write. Same rationale: don't surface as a sync error.
            logger.debug("DailyTimeTracker closed mid-write — skipping persist")

    def get_today_active_time(self) -> timedelta:
        """Get cumulative active time for today.

        Handles day rollover if we've passed midnight since last check.

        Returns:
            timedelta with today's total active time.
        """
        rollover_date = None
        rollover_seconds = 0.0
        with self._lock:
            current_date = self._get_local_date()
            if self._today != current_date:
                rollover_date = self._today
                rollover_seconds = self._today_seconds
                self._today = current_date
                self._today_seconds = 0.0
            result = self._today_seconds

        if rollover_date is not None:
            self._persist_date(rollover_date, rollover_seconds)
            self._load_new_day(current_date)
            # Re-read in case _load_new_day updated the value
            with self._lock:
                result = self._today_seconds

        return timedelta(seconds=result)

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

        # SQLite query outside lock — thread-local connection, no contention
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

    def _persist_date(self, target_date: date, seconds: float) -> None:
        """Persist a specific date's data to SQLite (no lock required).

        Uses MAX semantics so two concurrent _persist() calls (each with a
        monotonically-growing in-memory snapshot) never silently discard the
        higher value.  The in-memory counter is the ground truth; the DB
        value should never exceed it.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO daily_active_time (date, active_seconds, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    active_seconds = MAX(excluded.active_seconds, daily_active_time.active_seconds),
                    updated_at = excluded.updated_at
                """,
                (target_date.isoformat(), seconds, now),
            )

    def _increment_date(self, target_date: date, seconds: float) -> None:
        """Add seconds to a non-live date without changing today's counter."""
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO daily_active_time (date, active_seconds, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    active_seconds = daily_active_time.active_seconds + excluded.active_seconds,
                    updated_at = excluded.updated_at
                """,
                (target_date.isoformat(), seconds, now),
            )

    def _load_new_day(self, new_date: date) -> None:
        """Load existing data for a new day and merge it into memory.

        Must hold _lock for the SQLite read so concurrent add_active_time
        calls can't slip additions between the DB read and the in-memory
        merge (previously a ``max()`` there silently discarded those).

        The merge rule is ADD, not MAX: ``loaded`` is the baseline the DB
        already persisted, and any ``_today_seconds`` accumulated between
        rollover and this call is brand-new work that hasn't been persisted
        yet. Taking the max would lose it.
        """
        with self._lock:
            if self._today != new_date:
                return  # Another rollover raced ahead; nothing to do.
            with self._cursor() as cursor:
                cursor.execute(
                    """
                    SELECT active_seconds FROM daily_active_time
                    WHERE date = ?
                    """,
                    (new_date.isoformat(),),
                )
                row = cursor.fetchone()
                loaded = float(row["active_seconds"]) if row else 0.0
            pending = self._today_seconds
            self._today_seconds = loaded + pending
        logger.info(
            "Day rollover to %s, loaded %.1fs, merged in-memory %.1fs",
            new_date,
            loaded,
            pending,
        )

    def _persist(self) -> None:
        """Save current state to SQLite.

        Snapshots values under _lock to avoid reading stale state
        if a day rollover occurs concurrently.
        """
        with self._lock:
            today = self._today
            seconds = self._today_seconds
        if today is None:
            return
        self._persist_date(today, seconds)

    def close(self) -> None:
        """Close all database connections (from all threads)."""
        with self._conn_lock:
            self._closed = True
            for conn in self._all_connections:
                try:
                    conn.close()
                except Exception as e:
                    logger.debug("DailyTimeTracker conn.close() failed: %s", e)
            self._all_connections.clear()
        if hasattr(self._local, "connection"):
            del self._local.connection
