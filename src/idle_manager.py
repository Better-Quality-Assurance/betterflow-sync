"""Idle detection: pause/resume sync based on AFK status."""

import logging
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
        # A healthy AFK watcher heartbeats its current event continuously, so the
        # event's end-time tracks "now". If the latest AFK event ended more than
        # this many seconds ago, the tracker has frozen/crashed/gone blind — its
        # stale 'afk' must NOT be trusted as the live idle state (that is what
        # pins an active user as Idle forever). Generously above any normal
        # heartbeat/poll cadence so only a genuinely dead tracker trips it.
        self._afk_staleness_grace = 120.0

        # In-process input watcher (macOS), injected post-construction by the
        # app after it is created. It holds the MAIN app's Input Monitoring
        # grant, so it is authoritative over the AFK bucket — which comes from
        # bf-idle-tracker, a SEPARATE TCC subject that can be blind (missing
        # the grant) or stuck and report 'afk' while the user is typing. See
        # _has_recent_input(). None on platforms without an in-process watcher
        # (Windows/Linux) or before startup wiring — handled defensively.
        self.input_watcher = None

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

    def _has_recent_input(self, within_seconds: float) -> bool:
        """True if the in-process input watcher observed input within
        `within_seconds`.

        This is the authoritative override for the AFK bucket. bf-idle-tracker
        runs under its own TCC subject and can be blind (no Input Monitoring
        grant) or stuck, emitting 'afk' while the user types — the recurring
        "shows idle while I'm working" report. The in-process watcher DOES hold
        the main app's grant, so positive recent input means the user is NOT
        idle no matter what the AFK bucket claims.

        Conservative by design: no watcher, no observation yet, or any error
        returns False. It can only SUPPRESS a false idle, never fabricate
        activity — so a genuinely idle user is still detected via the AFK path.
        """
        watcher = self.input_watcher
        if watcher is None:
            return False
        try:
            last_input = watcher.get_last_input_at()
        except Exception as e:
            logger.debug("_has_recent_input: input watcher query failed: %s", e)
            return False
        if last_input is None:
            return False
        return (datetime.now(timezone.utc) - last_input) <= timedelta(seconds=within_seconds)

    def _afk_event_age_seconds(self, event) -> Optional[float]:
        """Seconds since the AFK event ended (``timestamp + duration``), or None
        if it can't be computed — a missing/None timestamp or duration (e.g. the
        AW API returned ``{"duration": null}``) is treated as unusable so the
        caller falls back to the OS idle clock rather than raising."""
        try:
            event_end = event.timestamp + timedelta(seconds=event.duration)
            return (datetime.now(timezone.utc) - event_end).total_seconds()
        except (TypeError, AttributeError):
            return None

    def _afk_event_is_current(self, event) -> bool:
        """True if the AFK event still covers ~now.

        The AFK watcher heartbeats its current event (afk OR not-afk), so for a
        healthy tracker the event's end-time tracks now. A frozen/blind/crashed
        tracker stops emitting, leaving an old event as "latest" — typically an
        'afk' whose end is well in the past. Trusting that stale 'afk' is what
        marks an active user Idle indefinitely (no fresh not-afk event ever
        lands to clear it). Returns False for such stale (or malformed) events so
        the caller falls back to the OS idle clock instead.
        """
        age = self._afk_event_age_seconds(event)
        return age is not None and age <= self._afk_staleness_grace

    def _is_in_call(self) -> bool:
        """Whether a call/meeting is active, via the sync engine. Defensive:
        any error (or a sync engine without the method) means 'not in a call'
        so idle detection still works."""
        try:
            return bool(self.sync_engine.is_in_call())
        except Exception:
            return False

    def _is_engaged_without_input(self) -> bool:
        """Whether a non-input engagement context is active — a call/meeting OR
        an active foreground-CPU session (Claude Code / build / render in the
        focused window). Either suppresses the idle pause: the user is engaged
        even without keyboard/mouse input. Defensive: any error means 'not
        engaged' so idle detection still works."""
        if self._is_in_call():
            return True
        try:
            return bool(self.sync_engine.is_active_dev_session())
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
            # Authoritative override: if the in-process input watcher (which
            # holds the main app's Input Monitoring grant) saw input within the
            # idle threshold, the user is active — regardless of the AFK bucket,
            # which comes from the separate-TCC-subject bf-idle-tracker and can
            # be blind/stuck and report 'afk' while the user types. Without this
            # the blind tracker paints live work as Idle until the health-check
            # restart lands (v1.5.50 still showed this). Checked BEFORE the AFK
            # fetch so positive input short-circuits the whole pause decision.
            if self._has_recent_input(self._idle_pause_threshold):
                if was_idle_paused:
                    logger.info(
                        "Recent in-process input - clearing idle pause "
                        "(AFK bucket was blind/stale)"
                    )
                    self.clear_idle_pause(send_event=True)
                    reschedule(self.config.sync.interval_seconds)
                    self.tray.set_state(TrayState.SYNCING)
                    trigger_sync("idle_resume_sync")
                return

            is_afk = False
            afk_duration = 0.0
            idle_start: Optional[datetime] = None

            # Canonical AFK read: prefers BetterFlow's bf-idle-tracker bucket
            # over a stale vanilla aw-watcher-afk bucket frozen at 'afk', and
            # falls back to the newest event across all AFK buckets. (Supersedes
            # the inline bucket-preference from #46 — same intent, centralized in
            # AWClient.get_latest_afk_event.)
            latest = self.aw.get_latest_afk_event()
            if latest is not None and self._afk_event_is_current(latest):
                is_afk = latest.status == "afk"
                afk_duration = latest.duration
                idle_start = latest.timestamp
            else:
                # No AFK event, OR the tracker froze on a stale one (end well in
                # the past while a live tracker would be heartbeating now). A
                # stale 'afk' must never pause an active user — fall back to the
                # OS idle clock, which reflects real keyboard/mouse activity
                # independent of the (possibly dead) bf-idle-tracker. On Windows
                # this is the ONLY safety net (no in-process input watcher).
                if latest is not None:
                    age = self._afk_event_age_seconds(latest)
                    logger.debug(
                        "AFK event not current (age=%s) — tracker likely frozen; "
                        "using OS idle clock instead of its '%s' status",
                        f"{age:.0f}s" if age is not None else "unknown",
                        getattr(latest, "status", "?"),
                    )
                system_idle = self._get_system_idle_seconds()
                if system_idle is not None:
                    is_afk = system_idle >= self._idle_pause_threshold
                    afk_duration = system_idle
                    idle_start = datetime.now(timezone.utc) - timedelta(seconds=system_idle)

            if is_afk and afk_duration >= self._idle_pause_threshold:
                engaged = self._is_engaged_without_input()
                if was_idle_paused:
                    # Already idle-paused, but an engagement context (a call, or
                    # an active foreground-CPU session) has since started while
                    # the user stays AFK. They're now engaged (listening/watching
                    # a meeting, or supervising a running session), so resume —
                    # otherwise the span stays painted Idle for as long as they
                    # don't touch the keyboard. The guard below only blocks
                    # ENTERING idle; this is the symmetric exit when engagement
                    # begins during an existing idle pause (Windows has no
                    # in-process input watcher, so AFK never clears on its own).
                    if engaged:
                        logger.info(
                            "Engagement active while idle-paused — resuming to track it"
                        )
                        self.clear_idle_pause(send_event=True)
                        reschedule(self.config.sync.interval_seconds)
                        self.tray.set_state(TrayState.SYNCING)
                        trigger_sync("engaged_resume_sync")
                    # else: still idle with no engagement — stay paused.
                else:
                    # Don't mark idle while the user is engaged without input —
                    # in a call/meeting (listening/watching) or supervising an
                    # active foreground-CPU session (Claude Code / build / render
                    # in the focused window). The AFK watcher is the only idle
                    # signal on Windows (no in-process input watcher), so a long
                    # engaged span otherwise trips this pause and paints the whole
                    # span Idle even though the user was present throughout.
                    if engaged:
                        logger.debug(
                            "AFK for %ds but engaged without input — not pausing as idle",
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
        """OS-level idle duration (seconds since last keyboard/mouse input).

        macOS: HIDIdleTime via ioreg. Windows: GetLastInputInfo via ctypes —
        this is Windows' safety net against a frozen bf-idle-tracker (Windows
        has no in-process input watcher), giving it the same real-activity
        signal macOS gets. Returns None on Linux / on any error. Implementation
        is shared with SyncEngine via sync.os_idle (single source of the
        platform syscalls)."""
        try:
            from .sync.os_idle import get_system_idle_seconds
        except ImportError:
            from sync.os_idle import get_system_idle_seconds
        return get_system_idle_seconds()
