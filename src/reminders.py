"""Break time and private time reminder manager."""

import logging
import threading
import time
from typing import Callable, Optional

try:
    from .config import ReminderSettings
    from .notifications import send_notification
except ImportError:
    from config import ReminderSettings
    from notifications import send_notification

logger = logging.getLogger(__name__)


class ReminderManager:
    """Tracks work and private-mode durations and fires OS notifications.

    Uses ``time.monotonic()`` so timers are unaffected by system clock changes.
    Call ``check()`` periodically (e.g. every 60 s from the scheduler) to
    evaluate whether a notification should be sent.
    """

    def __init__(
        self,
        settings: ReminderSettings,
        on_break_triggered: Optional[Callable[[], None]] = None,
    ) -> None:
        self._settings = settings
        self._on_break_triggered = on_break_triggered
        self._lock = threading.Lock()

        # Break-time state (protected by _lock)
        self._work_start: float | None = None
        self._last_break_notification: float | None = None
        self._tracking_active: bool = False
        self._on_break: bool = False

        # Private-time state (protected by _lock)
        self._private_start: float | None = None
        self._last_private_notification: float | None = None
        self._private_active: bool = False

    # -- Public API -------------------------------------------------------

    def on_tracking_started(self) -> None:
        """Call when tracking becomes active (resume, wake, app start)."""
        now = time.monotonic()
        with self._lock:
            if not self._tracking_active:
                self._work_start = now
                self._last_break_notification = None
                self._tracking_active = True
                logger.debug("Break timer started")

    def on_tracking_stopped(self) -> None:
        """Call when tracking pauses (pause, AFK, sleep, private)."""
        with self._lock:
            if self._tracking_active:
                self._work_start = None
                self._last_break_notification = None
                self._tracking_active = False
                logger.debug("Break timer reset")

    def on_private_started(self) -> None:
        """Call when private mode is enabled."""
        now = time.monotonic()
        with self._lock:
            if not self._private_active:
                self._private_start = now
                self._last_private_notification = None
                self._private_active = True
                logger.debug("Private timer started")

    def on_private_ended(self) -> None:
        """Call when private mode is disabled."""
        with self._lock:
            if self._private_active:
                self._private_start = None
                self._last_private_notification = None
                self._private_active = False
                logger.debug("Private timer reset")

    def on_break_started(self) -> None:
        """Call when auto-break begins (suppresses further break checks)."""
        with self._lock:
            self._on_break = True
            logger.debug("Break active — suppressing break timer")

    def on_break_ended(self) -> None:
        """Call when auto-break ends (resets work timer for next interval)."""
        with self._lock:
            self._on_break = False
            now = time.monotonic()
            self._work_start = now
            self._last_break_notification = None
            logger.debug("Break ended — work timer reset")

    def check(self) -> None:
        """Evaluate timers and send notifications if thresholds are exceeded."""
        now = time.monotonic()
        self._check_break(now)
        self._check_private(now)

    def update_settings(self, settings: ReminderSettings) -> None:
        """Apply new settings (e.g. from preferences menu)."""
        with self._lock:
            self._settings = settings
        logger.debug("Reminder settings updated")

    # -- Internal ---------------------------------------------------------

    def _check_break(self, now: float) -> None:
        with self._lock:
            if not self._settings.break_reminders_enabled:
                return
            if self._on_break:
                return
            if not self._tracking_active or self._work_start is None:
                return

            interval = self._settings.break_interval_hours * 3600
            elapsed = now - self._work_start

            if elapsed < interval:
                return

            # Determine when the last notification was sent (or use work_start).
            ref = self._last_break_notification or self._work_start
            if now - ref < interval:
                return

            hours = int(elapsed // 3600)
            self._last_break_notification = now

        # Send notification outside the lock to avoid holding it during I/O
        logger.info(f"Break reminder sent ({hours}h elapsed)")
        send_notification(
            "Time for a Break",
            f"You've been working for {hours}h - take a short break!",
        )

    def _check_private(self, now: float) -> None:
        with self._lock:
            if not self._settings.private_reminders_enabled:
                return
            if not self._private_active or self._private_start is None:
                return

            interval = self._settings.private_interval_minutes * 60
            elapsed = now - self._private_start

            if elapsed < interval:
                return

            ref = self._last_private_notification or self._private_start
            if now - ref < interval:
                return

            minutes = int(elapsed // 60)
            self._last_private_notification = now

        # Send notification outside the lock
        logger.info(f"Private time reminder sent ({minutes}m elapsed)")
        send_notification(
            "Private Time Still Active",
            f"Private mode has been on for {minutes}m - tracking is paused.",
        )
