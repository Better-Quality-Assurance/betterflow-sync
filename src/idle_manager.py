"""Idle detection: pause/resume sync based on AFK status."""

import logging
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class IdleManager:
    """Monitors user idle state and adjusts sync frequency.

    Owns its own _state_lock (leaf lock, no nesting with other locks).
    """

    def __init__(
        self,
        sync_engine,
        tray,
        aw,
        config,
        *,
        on_idle_pause: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self.sync_engine = sync_engine
        self.tray = tray
        self.aw = aw
        self.config = config
        self._on_idle_pause = on_idle_pause

        self._state_lock = threading.Lock()
        self._idle_paused = False
        self._idle_start: Optional[datetime] = None
        self._idle_pause_threshold = config.sync.idle_pause_minutes * 60
        self._IDLE_SYNC_INTERVAL = 300

    @property
    def idle_paused(self) -> bool:
        with self._state_lock:
            return self._idle_paused

    def clear_idle_pause(self, send_event: bool = True) -> None:
        """Clear idle pause state and optionally send the idle_time event."""
        with self._state_lock:
            was_paused = self._idle_paused
            idle_start = self._idle_start
            self._idle_paused = False
            self._idle_start = None
        if was_paused and send_event and idle_start:
            self.sync_engine.send_idle_event(idle_start)
        if was_paused and self._on_idle_pause:
            self._on_idle_pause(False)

    def flush_idle_event(self) -> None:
        """Send idle_time event for current idle period (e.g. on shutdown)."""
        with self._state_lock:
            idle_start = self._idle_start
            self._idle_start = None
            self._idle_paused = False
        if idle_start:
            self.sync_engine.send_idle_event(idle_start)

    def _is_in_call(self) -> bool:
        """Whether a call/meeting is active, via the sync engine. Defensive:
        any error (or a sync engine without the method) means 'not in a call'
        so idle detection still works."""
        try:
            return bool(self.sync_engine.is_in_call())
        except Exception:
            return False

    def check_idle_status(
        self,
        *,
        logged_in: bool,
        is_on_break: bool,
        reschedule: Callable[[int], None],
        trigger_sync: Callable[[str], None],
    ) -> None:
        """Check AFK bucket - pause sync if user idle for threshold+ minutes."""
        try:
            from .ui.tray import TrayState
        except ImportError:
            from ui.tray import TrayState

        if not logged_in:
            return

        with self._state_lock:
            was_idle_paused = self._idle_paused

        if not was_idle_paused:
            if self.sync_engine.is_paused or self.sync_engine.is_private:
                return
            if is_on_break:
                return

        try:
            is_afk = False
            afk_duration = 0.0
            idle_start: Optional[datetime] = None

            afk_buckets = self.aw.get_afk_buckets()
            if afk_buckets:
                events = self.aw.get_events(afk_buckets[0].id, limit=1)
                if events:
                    latest = events[0]
                    is_afk = latest.status == "afk"
                    afk_duration = latest.duration
                    idle_start = latest.timestamp
            else:
                system_idle = self._get_system_idle_seconds()
                if system_idle is not None:
                    is_afk = system_idle >= self._idle_pause_threshold
                    afk_duration = system_idle
                    idle_start = datetime.now(timezone.utc) - timedelta(seconds=system_idle)

            if is_afk and afk_duration >= self._idle_pause_threshold:
                in_call = self._is_in_call()
                if was_idle_paused:
                    # Already idle-paused, but a call has since started while the
                    # user stays AFK. They're now engaged in the meeting
                    # (listening/watching), so resume — otherwise the call span
                    # stays painted Idle for as long as they don't touch the
                    # keyboard. The _is_in_call() guard below only blocks
                    # ENTERING idle; this is the symmetric exit when a call
                    # begins during an existing idle pause (Windows has no
                    # in-process input watcher, so AFK never clears on its own
                    # mid-call).
                    if in_call:
                        logger.info(
                            "Call active while idle-paused — resuming to track the call"
                        )
                        self.clear_idle_pause(send_event=True)
                        reschedule(self.config.sync.interval_seconds)
                        self.tray.set_state(TrayState.SYNCING)
                        trigger_sync("call_resume_sync")
                    # else: still idle with no call — stay paused.
                else:
                    # Don't mark idle while the user is in a call/meeting. In a
                    # meeting they're engaged (listening/watching) even without
                    # keyboard/mouse input. The AFK watcher is the only idle
                    # signal on Windows (no in-process input watcher), so a long
                    # call otherwise trips this pause and paints the whole span
                    # as Idle even though the meeting app was focused throughout.
                    if in_call:
                        logger.debug(
                            "AFK for %ds but a call is active — not pausing as idle",
                            int(afk_duration),
                        )
                        return
                    if idle_start is None:
                        idle_start = datetime.now(timezone.utc)
                    logger.info(
                        f"User idle for {int(afk_duration)}s (>= {self._idle_pause_threshold}s) "
                        f"- backing off sync to {self._IDLE_SYNC_INTERVAL}s"
                    )
                    with self._state_lock:
                        self._idle_paused = True
                        self._idle_start = idle_start
                    reschedule(self._IDLE_SYNC_INTERVAL)
                    self.tray.set_state(TrayState.PAUSED, "Idle")
                    if self._on_idle_pause:
                        self._on_idle_pause(True)
            else:
                if was_idle_paused:
                    logger.info("User active again - restoring normal sync interval")
                    self.clear_idle_pause(send_event=True)
                    reschedule(self.config.sync.interval_seconds)
                    self.tray.set_state(TrayState.SYNCING)
                    trigger_sync("idle_resume_sync")

        except Exception as e:
            logger.warning("Idle check error: %s", e)

    @staticmethod
    def _get_system_idle_seconds() -> Optional[float]:
        """Query macOS HIDIdleTime for system-level idle duration."""
        if sys.platform != "darwin":
            return None
        try:
            import subprocess as _sp
            out = _sp.check_output(
                ["ioreg", "-c", "IOHIDSystem", "-d", "4"],
                timeout=5,
                text=True,
            )
            for line in out.splitlines():
                if "HIDIdleTime" in line and "=" in line:
                    val = line.split("=")[-1].strip()
                    try:
                        return int(val) / 1_000_000_000
                    except ValueError:
                        logger.debug(f"_get_system_idle_seconds: unexpected HIDIdleTime value {val!r}")
                        return None
        except Exception as e:
            logger.debug(f"_get_system_idle_seconds failed: {e}")
        return None
