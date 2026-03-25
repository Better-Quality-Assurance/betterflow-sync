"""Break management: start/end auto-breaks, countdown timer."""

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apscheduler.schedulers.background import BackgroundScheduler

try:
    from .notifications import send_notification
except ImportError:
    from notifications import send_notification

logger = logging.getLogger(__name__)


class BreakManager:
    """Manages auto-break lifecycle: start, end, countdown.

    Owns _break_lock (leaf lock, no nesting with other locks).
    """

    def __init__(
        self,
        sync_engine,
        tray,
        scheduler: "BackgroundScheduler",
        config,
        reminder_manager=None,
    ) -> None:
        self.sync_engine = sync_engine
        self.tray = tray
        self.scheduler = scheduler
        self.config = config
        self.reminder_manager = reminder_manager

        self._break_lock = threading.Lock()
        self._on_break = False
        self._break_start: datetime | None = None
        self._pre_break_paused = False
        self._pre_break_private = False

    @property
    def is_on_break(self) -> bool:
        """Thread-safe read of break state."""
        with self._break_lock:
            return self._on_break

    def start_break(self) -> None:
        """Begin an auto-break: pause sync, amber tray, schedule auto-resume."""
        try:
            from .ui.tray import TrayState
        except ImportError:
            from ui.tray import TrayState

        from apscheduler.triggers.date import DateTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        # Read engine state BEFORE acquiring _break_lock to preserve lock ordering
        pre_break_private = self.sync_engine.is_private
        with self._break_lock:
            if self._on_break:
                return
            already_paused = self.sync_engine.is_paused
            if already_paused:
                return
            self._on_break = True
            self._break_start = datetime.now(timezone.utc)
            self._pre_break_paused = False
            self._pre_break_private = pre_break_private

        self.sync_engine.pause()
        duration = self.config.reminders.break_duration_minutes

        with self.tray.model.lock:
            self.tray.model.on_break = True
            self.tray.model.break_minutes_left = duration
        self.tray.set_state(TrayState.ON_BREAK)

        if self.reminder_manager:
            self.reminder_manager.on_break_started()

        run_date = datetime.now(timezone.utc) + timedelta(minutes=duration)
        if self.scheduler.running:
            self.scheduler.add_job(
                self.end_break,
                trigger=DateTrigger(run_date=run_date),
                id="auto_break_end",
                replace_existing=True,
            )
            self.scheduler.add_job(
                self._update_break_countdown,
                trigger=IntervalTrigger(seconds=60),
                id="break_countdown",
                replace_existing=True,
            )

        send_notification(
            "Break Time",
            f"Tracking paused for {duration} minutes. Enjoy your break!",
        )
        logger.info(f"Auto-break started ({duration}m)")

    def end_break(self, silent: bool = False, force: bool = False) -> None:
        """End the auto-break: resume sync, restore pre-break tray state."""
        try:
            from .ui.tray import TrayState
        except ImportError:
            from ui.tray import TrayState

        with self._break_lock:
            if not self._on_break:
                return
            if not self._break_start:
                logger.warning("end_break: _break_start is None (state corruption)")
                if not force:
                    return
            if not force and self._break_start:
                elapsed = (datetime.now(timezone.utc) - self._break_start).total_seconds()
                if elapsed < 60:
                    logger.warning(
                        f"Ignoring premature end_break after {elapsed:.0f}s "
                        f"(minimum 60s, silent={silent})"
                    )
                    return
            self._on_break = False
            break_start = self._break_start
            self._break_start = None
            pre_break_paused = self._pre_break_paused
            pre_break_private = self._pre_break_private

        if break_start:
            self.sync_engine.send_break_event(break_start)

        with self.tray.model.lock:
            self.tray.model.on_break = False
            self.tray.model.break_minutes_left = 0

        if pre_break_private:
            self.sync_engine.resume()
            self.sync_engine.set_private_mode(True)
            self.tray.set_state(TrayState.PRIVATE)
        elif pre_break_paused:
            self.tray.set_state(TrayState.PAUSED)
        else:
            self.sync_engine.resume()
            self.tray.set_state(TrayState.SYNCING)

        if self.reminder_manager:
            self.reminder_manager.on_break_ended()
            if not pre_break_paused and not pre_break_private:
                self.reminder_manager.on_tracking_started()

        if self.scheduler.running:
            for job_id in ("auto_break_end", "break_countdown"):
                try:
                    self.scheduler.remove_job(job_id)
                except Exception:
                    logger.debug("Could not remove break job %s (already gone)", job_id)

        if not silent:
            send_notification(
                "Break Over",
                "Tracking resumed - welcome back!",
            )
        logger.info("Auto-break ended")

    def _update_break_countdown(self) -> None:
        """Decrement the break minutes-left counter for tray display."""
        with self.tray.model.lock:
            if self.tray.model.break_minutes_left > 0:
                self.tray.model.break_minutes_left -= 1
        self.tray._update_menu()
