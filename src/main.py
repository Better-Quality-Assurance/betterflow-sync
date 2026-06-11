"""BetterFlow - Main entry point."""

import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

# Support both relative imports (module) and absolute imports (PyInstaller)
try:
    from .__init__ import __version__
    from .auth import KeychainManager, LoginManager
    from .aw_manager import AWManager
    from .config import Config, setup_logging
    from . import error_reporter
    from .browser_tracker import start_browser_tracker
    from .display_info import start_display_tracker
    from .reminders import ReminderManager
    from .sync import AWClient, BetterFlowClient, OfflineQueue, SyncEngine
    from .sync.http_client import BetterFlowAuthError
    from .system_events import start_system_event_listener
    from .ui.permissions import (
        check_accessibility,
        check_input_monitoring,
        grant_tcc_permissions,
        input_monitoring_active,
    )
    from .ui.tray import TrayIcon, TrayState
    from .update_checker import check_for_update
    from .break_manager import BreakManager
    from .idle_manager import IdleManager
    from .hours_tracker import HoursTracker
    from .update_handler import UpdateHandler
    from .system_event_handler import SystemEventHandler
except ImportError:
    from src import __version__
    from auth import KeychainManager, LoginManager
    from aw_manager import AWManager
    from config import Config, setup_logging
    import error_reporter
    from browser_tracker import start_browser_tracker
    from display_info import start_display_tracker
    from reminders import ReminderManager
    from sync import AWClient, BetterFlowClient, OfflineQueue, SyncEngine
    from sync.http_client import BetterFlowAuthError
    from system_events import start_system_event_listener
    from ui.permissions import (
        check_accessibility,
        check_input_monitoring,
        grant_tcc_permissions,
        input_monitoring_active,
    )
    from ui.tray import TrayIcon, TrayState
    from update_checker import check_for_update
    from break_manager import BreakManager
    from idle_manager import IdleManager
    from hours_tracker import HoursTracker
    from update_handler import UpdateHandler
    from system_event_handler import SystemEventHandler

# Import send_notification at module level so tests can patch src.main.send_notification
try:
    from .notifications import send_notification, clear_notifications
except ImportError:
    from notifications import send_notification, clear_notifications  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# Lock ordering (acquire outer-to-inner, never reverse):
#   _shutdown_lock > _login_lock > _sync_lock > _break_lock
#   > _state_lock > _pause_state_lock > _pending_update_lock
#   > tray.model.lock


def _greeting() -> str:
    """Return a time-of-day greeting."""
    hour = datetime.now().hour
    if hour < 12:
        return "Good morning"
    elif hour < 17:
        return "Good afternoon"
    return "Good evening"


def _day_greeting() -> str:
    """Return a contextual sub-message based on day of week."""
    day = datetime.now().weekday()  # 0=Mon … 6=Sun
    if day == 0:
        return "Have a productive week!"
    elif day == 4:
        return "Happy Friday!"
    elif day >= 5:
        return "Enjoy your weekend!"
    return "Have a productive day!"

# Resolve version string once (handles both str and module forms from PyInstaller)
_VERSION: str = __version__ if isinstance(__version__, str) else __version__.__version__


class SyncCoordinator:
    """Owns the sync scheduler, sync loop, and hours tracking.

    Pulled out of BetterFlowApp so that the app class focuses on
    lifecycle orchestration and event wiring only.
    """

    def __init__(
        self,
        config: Config,
        aw: AWClient,
        bf: BetterFlowClient,
        queue: OfflineQueue,
        sync_engine: SyncEngine,
        tray: TrayIcon,
        aw_manager: AWManager,
        reminder_manager: Optional[ReminderManager] = None,
        error_reporter: Optional["error_reporter.ErrorReporter"] = None,
    ) -> None:
        self.config = config
        self.aw = aw
        self.bf = bf
        self.queue = queue
        self.sync_engine = sync_engine
        self.tray = tray
        self.aw_manager = aw_manager
        self.reminder_manager = reminder_manager
        self.error_reporter = error_reporter

        self.scheduler = BackgroundScheduler()
        self._last_tick: Optional[datetime] = None

        # Consecutive hard sync failures. Reported to the logs channel once it
        # crosses the threshold (transient blips are normal and not reported).
        # Only mutated inside _do_sync, which holds _sync_lock, so no extra lock.
        self._consecutive_sync_failures = 0
        self._SYNC_FAILURE_ALERT_THRESHOLD = 3

        # Flags set by the app layer - protected by _state_lock
        self._logged_in = False
        self._paused_by_network = False
        self._state_lock = threading.Lock()
        self._sync_lock = threading.Lock()

        # Sub-managers (own their locks)
        self.break_mgr = BreakManager(sync_engine, tray, self.scheduler, config, reminder_manager)
        self.idle_mgr = IdleManager(sync_engine, tray, aw, config)
        self.hours = HoursTracker(bf, sync_engine, tray)

        # Optional callback wired by the app for auth-error re-login
        self._on_auth_error: Optional[Callable] = None

        # In-process input watcher reference, wired by the App after
        # construction. Used by `_check_idle_tracker_health` to cross-check
        # the bf-idle-tracker subprocess's AFK output against in-process
        # input observations — the only way to detect that the tracker
        # subprocess is missing its own Input Monitoring grant (a separate
        # TCC subject from the main app, easy to miss in install).
        self._input_watcher: Optional[Any] = None
        # Throttle for the false-AFK warning. Mirrors _last_perm_warn_at
        # exactly so the user gets at most one notification per
        # _PERM_REWARN_INTERVAL while the disagreement persists.
        self._idle_tracker_warn_lock = threading.Lock()
        self._last_idle_tracker_warn_at: Optional[datetime] = None

        # macOS permission-warning throttle. Input Monitoring can be revoked
        # silently (e.g. a new build changes the code signature and macOS drops
        # the TCC grant), which leaves us collecting window time but no
        # keystrokes/clicks — server-side that looks like a jiggler and flags
        # the day as suspicious. We surface a notification, throttled to one per
        # state-change plus a periodic re-warn so it can't spam the user.
        self._perm_lock = threading.Lock()
        self._input_tracking_ok: Optional[bool] = None  # None = not yet checked
        self._last_perm_warn_at: Optional[datetime] = None

        # Liveness heartbeat while paused. The normal heartbeat rides _do_sync,
        # which is skipped when paused (break / lock / manual), so a break longer
        # than the server's 30-min stale-session cleanup gets its session marked
        # "crashed" and tracking doesn't resume on return. Send a lightweight
        # heartbeat from the 60s tick while paused-but-running to keep the
        # session alive. monotonic-throttled so it's at most one per interval.
        self._last_liveness_heartbeat: Optional[float] = None

        # Auth-warn throttle (Emilian, 2026-06-11): after his laptop restart
        # auto-login silently failed and the tray went to WAITING_AUTH without
        # any system notification — he could have worked a full day untracked.
        # Throttled to one warn per OFF transition, then periodically re-warned
        # while WAITING_AUTH persists. Same shape as _maybe_warn_input_tracking
        # so the two pathways look the same to anyone debugging.
        self._auth_warn_lock = threading.Lock()
        self._last_auth_warn_at: Optional[datetime] = None
        self._was_logged_in_for_warn: Optional[bool] = None

    @property
    def logged_in(self) -> bool:
        with self._state_lock:
            return self._logged_in

    @logged_in.setter
    def logged_in(self, value: bool) -> None:
        with self._state_lock:
            self._logged_in = value

    @property
    def paused_by_network(self) -> bool:
        with self._state_lock:
            return self._paused_by_network

    @paused_by_network.setter
    def paused_by_network(self, value: bool) -> None:
        with self._state_lock:
            self._paused_by_network = value

    @property
    def idle_paused(self) -> bool:
        return self.idle_mgr.idle_paused

    def clear_idle_pause(self, send_event: bool = True) -> None:
        self.idle_mgr.clear_idle_pause(send_event=send_event)

    def flush_idle_event(self) -> None:
        self.idle_mgr.flush_idle_event()

    def start(self) -> None:
        """Start the scheduler and queue startup work without blocking UI."""
        # BackgroundScheduler.shutdown() is terminal -- create a fresh one
        # if the previous scheduler was shut down (e.g. after logout/re-login).
        if not self.scheduler.running:
            self.scheduler = BackgroundScheduler()
            self.scheduler.add_job(
                self._do_sync,
                trigger=IntervalTrigger(seconds=self.config.sync.interval_seconds),
                id="sync_job",
                replace_existing=True,
            )
            # Unified 60s tick replaces 5 separate jobs (timer coalescing)
            self.scheduler.add_job(
                self._tick_60s,
                trigger=IntervalTrigger(seconds=60),
                id="tick_60s",
                replace_existing=True,
            )
            # Expire stale queue events daily
            self.scheduler.add_job(
                self._expire_old_queue_events,
                trigger=IntervalTrigger(hours=24),
                id="queue_expire_job",
                replace_existing=True,
            )
            # Refresh weekly/monthly trends every 30 minutes
            self.scheduler.add_job(
                self._fetch_trends,
                trigger=IntervalTrigger(minutes=30),
                id="trends_refresh_job",
                replace_existing=True,
            )
            self.scheduler.start()
            logger.info(
                f"Sync loop started (interval: {self.config.sync.interval_seconds}s)"
            )

        now = datetime.now(timezone.utc)
        self.scheduler.add_job(
            self._do_sync,
            trigger=DateTrigger(run_date=now),
            id="startup_sync",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._fetch_trends,
            trigger=DateTrigger(run_date=now),
            id="startup_trends",
            replace_existing=True,
        )
        # Surface a missing-permission warning soon after launch. Delayed a few
        # seconds so the in-process watchers have attempted their own start and
        # any TCC grant has settled. The 60s tick re-checks thereafter, so the
        # warning clears automatically once the user toggles the permission on.
        self.scheduler.add_job(
            self._check_permissions,
            trigger=DateTrigger(run_date=now + timedelta(seconds=8)),
            id="startup_permissions",
            replace_existing=True,
        )

    def stop(self) -> None:
        """Shut down the scheduler if running."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def reschedule(self, interval_seconds: int) -> None:
        """Change the sync interval on the fly."""
        if self.scheduler.running:
            self.scheduler.reschedule_job(
                "sync_job",
                trigger=IntervalTrigger(seconds=interval_seconds),
            )

    def trigger_sync(self, job_id: str = "immediate_sync") -> None:
        """Schedule a one-off sync (e.g. after wake or network change)."""
        if self.scheduler.running:
            self.scheduler.add_job(self._do_sync, id=job_id, replace_existing=True)

    @property
    def is_on_break(self) -> bool:
        return self.break_mgr.is_on_break

    def start_break(self) -> None:
        self.break_mgr.start_break()

    def end_break(self, silent: bool = False, force: bool = False) -> None:
        self.break_mgr.end_break(silent=silent, force=force)

    def fetch_projects(self) -> None:
        """Fetch available projects from API and set on tray."""
        try:
            response = self.bf.get_projects()
            logger.debug(f"Raw projects response: {response}")
            data = response.get("data", {})
            projects = data.get("projects", [])
            current_project = data.get("current_project")
            # Auto-select when there's exactly one project and none is active
            if not current_project and len(projects) == 1:
                current_project = projects[0]
                logger.info(f"Auto-selected sole project: {current_project.get('name', '<unnamed>')}")
            self.tray.set_projects(projects, current_project=current_project)
            if current_project:
                self.sync_engine.set_current_project(current_project)
            logger.info(f"Loaded {len(projects)} projects (active: {current_project.get('name') if current_project else 'none'})")
        except Exception as e:
            logger.warning(f"Failed to fetch projects: {e}")

    # -- internal ---------------------------------------------------------

    @property
    def sync_in_progress(self) -> bool:
        """Check if a sync is currently running (non-blocking)."""
        return self._sync_lock.locked()

    # Re-warn about disabled input tracking at most this often while it stays off.
    _PERM_REWARN_INTERVAL = timedelta(hours=4)

    def _check_permissions(self) -> None:
        """Surface a visible warning when Input Monitoring is off.

        Input Monitoring is the only tracking permission BetterFlow requires —
        without it we collect active time but zero keystrokes/clicks, which the
        server reads as a jiggler and flags the day as suspicious. App names and
        durations come from NSWorkspace and don't need Accessibility.

        No-op on non-macOS, where the check returns True.
        """
        try:
            has_input = input_monitoring_active()
            granted = has_input
            status = None if has_input else "Input tracking OFF — Fix Permissions"

            high_priority = (
                TrayState.PAUSED, TrayState.PRIVATE, TrayState.ON_BREAK,
                TrayState.ERROR, TrayState.QUEUE_WARNING, TrayState.QUEUED,
                TrayState.WAITING_AUTH,
            )
            with self.tray.model.lock:
                previous_needs_permissions = self.tray.model.needs_permissions
                previous_input_ok = self.tray.model.input_monitoring_ok
                self.tray.model.needs_permissions = not granted
                self.tray.model.input_monitoring_ok = has_input
                current_state = self.tray.model.state
                if not granted:
                    if current_state not in high_priority:
                        self.tray.model.state = TrayState.NEEDS_PERMISSIONS
                        self.tray.model.status_text = status
                        should_update_icon = True
                    else:
                        should_update_icon = False
                else:
                    if current_state == TrayState.NEEDS_PERMISSIONS:
                        self.tray.model.state = TrayState.SYNCING
                        should_update_icon = True
                    else:
                        should_update_icon = False
                should_update_menu = (
                    previous_needs_permissions != self.tray.model.needs_permissions
                    or previous_input_ok != self.tray.model.input_monitoring_ok
                )
            if should_update_icon:
                self.tray._update_icon()
            if should_update_icon or should_update_menu:
                self.tray._update_menu()

            self._maybe_warn_input_tracking(has_input)

            if granted and should_update_icon:
                logger.info("macOS permissions granted — clearing warning")
        except Exception as e:
            logger.warning("Permissions check failed: %s", e)

    def _maybe_warn_input_tracking(self, has_input: bool) -> None:
        """Notify the user when keystroke/click capture is disabled.

        Throttled to one notification per OFF transition, then re-warned every
        _PERM_REWARN_INTERVAL while it remains off so a long session can't hide
        the problem.
        """
        now = datetime.now(timezone.utc)
        with self._perm_lock:
            self._input_tracking_ok = has_input
            if has_input:
                self._last_perm_warn_at = None
                return
            recently_warned = (
                self._last_perm_warn_at is not None
                and (now - self._last_perm_warn_at) < self._PERM_REWARN_INTERVAL
            )
            if recently_warned:
                return
            self._last_perm_warn_at = now

        logger.warning(
            "Input Monitoring permission missing — keystroke/click capture disabled"
        )
        send_notification(
            "Input tracking is off",
            "BetterFlow can't record keystrokes or clicks, so your hours may be "
            "flagged as suspicious. Click the BetterFlow menu bar icon → "
            "Fix Permissions to turn on Input Monitoring.",
        )

    def _maybe_warn_login_required(self, *, source: str) -> None:
        """Notify the user when the session is gone and the app needs a login.

        Mirrors ``_maybe_warn_input_tracking``: one notification at the
        transition into not-logged-in, then a periodic re-warn every
        ``_PERM_REWARN_INTERVAL`` while it stays that way. The amber
        WAITING_AUTH menu bar icon alone is too easy to miss — Emilian's
        2026-06-11 incident report was "if I had not noticed this, I could
        have lost an entire day of work."

        Idempotent: callers may invoke from startup auto-login failure, from
        ``_handle_auth_error`` mid-session, and from the periodic tick — only
        the first call within the rewarn window emits a notification.
        """
        now = datetime.now(timezone.utc)
        with self._auth_warn_lock:
            was_logged_in = self._was_logged_in_for_warn
            self._was_logged_in_for_warn = False

            recently_warned = (
                self._last_auth_warn_at is not None
                and (now - self._last_auth_warn_at) < self._PERM_REWARN_INTERVAL
            )
            # Force a warn on the True → False transition even if we warned
            # recently for an unrelated reason (e.g. a previous boot of the
            # process logged in then expired). Otherwise the user gets one
            # warn per rewarn interval, regardless of state churn in between.
            transitioned_off = was_logged_in is True
            if recently_warned and not transitioned_off:
                return
            self._last_auth_warn_at = now

        logger.warning(
            "Login required (%s) — notifying user; tray sync paused until re-login",
            source,
        )
        send_notification(
            "BetterFlow is not tracking",
            "Your session ended and the app is no longer recording your work. "
            "Click the BetterFlow menu bar icon → Retry Login to sign back in.",
        )

    def _mark_logged_in_for_warn(self) -> None:
        """Reset the auth-warn throttle on a fresh successful login.

        Lets the next transition to not-logged-in re-emit the notification
        immediately, even if the previous warn was within the rewarn window.
        """
        with self._auth_warn_lock:
            self._last_auth_warn_at = None
            self._was_logged_in_for_warn = True

    def _check_auth_warn(self) -> None:
        """Re-warn periodically while the app is still WAITING_AUTH.

        The startup / session-expired notification fires once at the
        transition; this tick keeps it from being a one-shot the user
        misses while heads-down. Throttled via the same rewarn window
        as the input-monitoring warn.
        """
        if self.logged_in:
            return
        if self.tray.model.state != TrayState.WAITING_AUTH:
            return
        self._maybe_warn_login_required(source="periodic")

    # Window the bf-idle-tracker health check uses to decide whether the
    # in-process input watcher has seen "recent" input. Must be shorter than
    # the tracker's --timeout (default 600s = 10 min) so a genuine 10-min
    # AFK period doesn't show as a disagreement, but long enough that a
    # short typing pause doesn't trip the warning.
    _IDLE_TRACKER_RECENT_INPUT_S = 180  # 3 min

    def _check_idle_tracker_health(self) -> None:
        """Detect when bf-idle-tracker is reporting AFK while the in-process
        input watcher is seeing keystrokes — Lucian's 2026-06-11 incident.

        bf-idle-tracker runs in a SEPARATE process under its own TCC subject,
        distinct from the main app. macOS can have Input Monitoring granted
        for BetterFlow.app but NOT for the bundled tracker binary; the user
        toggles ONE switch in System Settings and reasonably assumes both
        are covered, but they're not. The tracker silently sees zero input
        → reports AFK after its 10-min timeout regardless of how much the
        user is typing → the dashboard credits them no active time.

        Detection: the in-process input watcher (which DOES have grant —
        we already check it via _maybe_warn_input_tracking) saw input in
        the last 3 minutes, AND the latest AFK bucket event says 'afk'.
        That combination is only possible if the tracker is blind.

        Notification is throttled to one per rewarn window like every other
        permission-class warning. Best-effort: any error in querying AW
        falls through silently — this is a diagnostic, not a billing path.
        """
        if not self.logged_in:
            return
        if self._input_watcher is None:
            return

        last_input = self._input_watcher.get_last_input_at()
        if last_input is None:
            return  # Watcher hasn't seen anything yet — can't compare.

        now = datetime.now(timezone.utc)
        recent_input_window = timedelta(seconds=self._IDLE_TRACKER_RECENT_INPUT_S)
        if now - last_input > recent_input_window:
            return  # No recent in-process input → no disagreement to flag.

        # Query the AFK bucket for the latest event. Best-effort; AW being
        # transiently unreachable is a different failure mode handled
        # elsewhere.
        try:
            buckets = self.aw.get_afk_buckets()
        except Exception as e:
            logger.debug("idle_tracker_health: AW unreachable, skipping: %s", e)
            return
        if not buckets:
            return  # No AFK bucket yet — tracker hasn't started or hasn't reported.

        # A user who migrated from vanilla ActivityWatch can have BOTH a
        # `bf-idle-tracker` bucket and a stale `aw-watcher-afk` bucket. The
        # stale one's last event is frozen at "afk" forever, so picking the
        # wrong one fires a false-positive notification every time the user
        # types. Prefer the bf-idle-tracker bucket; fall back to picking the
        # one with the most recent event if neither name matches.
        bf_buckets = [b for b in buckets if "bf-idle-tracker" in b.id]
        candidate_buckets = bf_buckets or buckets

        latest = None
        for bucket in candidate_buckets:
            try:
                events = self.aw.get_events(bucket.id, limit=1)
            except Exception as e:
                logger.debug("idle_tracker_health: AFK fetch failed for %s: %s", bucket.id, e)
                continue
            if not events:
                continue
            event = events[0]
            if latest is None or event.timestamp > latest.timestamp:
                latest = event

        if latest is None:
            return  # No AFK events anywhere — tracker hasn't reported yet.

        status = (latest.data or {}).get("status")
        if status != "afk":
            return  # Tracker agrees with the input watcher — nothing to do.

        # Avoid the return-from-AFK transition lag. The user could have been
        # genuinely AFK for 15 min, returned, and typed within the same
        # second that this tick runs — the AFK watcher hasn't posted its
        # "not-afk" transition event yet, but it will within a heartbeat
        # pulsetime. If the latest "afk" event ENDS later than the input
        # observation, there's no actual disagreement: the tracker just
        # hasn't caught up. Require the AFK span to have ended at least
        # 30s before the most recent input before flagging it as broken.
        afk_end = latest.timestamp + timedelta(seconds=float(latest.duration))
        if afk_end > last_input - timedelta(seconds=30):
            return

        # Disagreement detected. Throttled warn.
        with self._idle_tracker_warn_lock:
            recently_warned = (
                self._last_idle_tracker_warn_at is not None
                and (now - self._last_idle_tracker_warn_at) < self._PERM_REWARN_INTERVAL
            )
            if recently_warned:
                return
            self._last_idle_tracker_warn_at = now

        logger.warning(
            "Idle tracker disagrees with input watcher: input observed at %s "
            "but AFK bucket reports 'afk' — bf-idle-tracker likely missing "
            "Input Monitoring permission (separate TCC subject from main app)",
            last_input.isoformat(),
        )
        send_notification(
            "BetterFlow may not be detecting your input",
            "Your activity isn't being recorded as work time. The BetterFlow "
            "idle tracker needs Input Monitoring permission. Click the menu "
            "bar icon → Diagnostics → Fix Permissions and grant access to "
            "bf-idle-tracker as well as BetterFlow.",
        )

    def _tick_60s(self) -> None:
        """Unified 60-second tick - one wakeup instead of five.

        Order: tick_clock first (ghost detection), then idle check (may
        pause sync), then hours refresh, then lightweight checks.

        Each sub-task is wrapped in try/except so one failure can't kill
        the APScheduler job — if the job raises, APScheduler would log
        once and stop re-scheduling, freezing the 60s tick entirely
        (idle detection, hours, permissions, reminders all silently dead).
        Several callees already self-guard but tray.tick_clock() does not.
        """
        for fn, label in (
            (self.tray.tick_clock, "tick_clock"),
            (self._check_idle_status, "idle_check"),
            (self._liveness_heartbeat, "liveness_heartbeat"),
            (self._refresh_hours_today, "hours_refresh"),
            (self._check_permissions, "permissions"),
            (self._check_auth_warn, "auth_warn"),
            (self._check_idle_tracker_health, "idle_tracker_health"),
        ):
            try:
                fn()
            except Exception as e:
                logger.warning("_tick_60s/%s failed: %s", label, e)
        if self.reminder_manager:
            try:
                self.reminder_manager.check()
            except Exception as e:
                logger.warning("_tick_60s/reminders failed: %s", e)

    def _check_idle_status(self) -> None:
        self.idle_mgr.check_idle_status(
            logged_in=self.logged_in,
            is_on_break=self.is_on_break,
            reschedule=self.reschedule,
            trigger_sync=self.trigger_sync,
        )

    # Keep the server session alive at least this often while paused. Well under
    # the server's 30-min stale-session cleanup so a long break never trips it.
    _LIVENESS_HEARTBEAT_INTERVAL = 300  # seconds

    def _liveness_heartbeat(self) -> None:
        """Heartbeat while paused-but-running so the server keeps the session.

        The normal heartbeat rides _do_sync, which early-returns when paused
        (break / screen lock / manual / private), so during a break longer than
        the server's 30-min cleanup the device's last_seen_at goes stale and the
        session is marked 'crashed' — then tracking doesn't resume on return.
        A paused agent is alive, not crashed: send a lightweight heartbeat to
        keep the session open. It only refreshes last_seen_at, never active time.

        Skipped while actively syncing (that path already heartbeats) and while
        offline (no server to reach). Throttled to one per interval.
        """
        if not self.logged_in:
            return
        if self.paused_by_network:
            return  # offline — the heartbeat would just fail; resume on reconnect
        paused = (
            self.sync_engine.is_paused
            or self.sync_engine.is_private
            or self.is_on_break
        )
        if not paused:
            return  # active sync already sends heartbeats

        now = time.monotonic()
        if (
            self._last_liveness_heartbeat is not None
            and now - self._last_liveness_heartbeat < self._LIVENESS_HEARTBEAT_INTERVAL
        ):
            return
        self._last_liveness_heartbeat = now

        auth_err = self.sync_engine.send_heartbeat_now()
        if auth_err is not None:
            self._handle_auth_error(auth_err, source="liveness_heartbeat")

    _DO_SYNC_DEADLINE = 120  # seconds — must exceed request_timeout * (max_retries + 1)

    def _do_sync(self) -> None:
        """Perform a sync cycle."""
        if not self._sync_lock.acquire(blocking=False):
            logger.debug("Sync already in progress, skipping")
            return

        # Watchdog timer: if _do_sync is still running after the deadline,
        # discard pooled connections so the next request creates a fresh TCP
        # connection. threading.Timer uses Event.wait() internally — the timer
        # thread is suspended during macOS sleep, then fires immediately on
        # wake when the kernel resumes userspace threads. This is the backstop
        # for cases where the sleep notification was missed.
        watchdog_cancelled = threading.Event()

        def _watchdog():
            if watchdog_cancelled.is_set():
                return
            logger.error("_do_sync watchdog: sync exceeded %ds — resetting sessions", self._DO_SYNC_DEADLINE)
            if self.error_reporter is not None:
                self.error_reporter.capture(
                    f"Sync hung — exceeded {self._DO_SYNC_DEADLINE}s watchdog deadline",
                    level="error",
                    tags={"component": "sync-watchdog"},
                    fingerprint="sync-watchdog-timeout",
                )
            try:
                self.bf.reset_session()
                self.aw.reset_session()
            except Exception:
                logger.debug("Session reset after watchdog timeout failed", exc_info=True)

        watchdog = threading.Timer(self._DO_SYNC_DEADLINE, _watchdog)
        watchdog.daemon = True
        watchdog.start()
        stats = None
        try:
            if self.sync_engine.is_private:
                self.tray.set_state(TrayState.PRIVATE)
                return

            if self.break_mgr.is_on_break:
                self.tray.set_state(TrayState.ON_BREAK)
                return

            # idle_paused: sync still runs at reduced interval (_IDLE_SYNC_INTERVAL)
            # so no early return here — just update tray state
            is_idle = self.idle_paused

            if self.paused_by_network:
                self.tray.set_state(TrayState.QUEUED, "Offline")
                self.tray.update_stats(queue_size=self.queue.size())
                return

            if self.aw_manager.is_managing:
                self.aw_manager.restart_if_needed()

            if not self.aw.is_running():
                if self.aw_manager.is_managing:
                    logger.warning("ActivityWatch not responding — attempting restart")
                    self.aw_manager.stop()
                    self.aw_manager.start()
                self.tray.set_state(TrayState.ERROR, "ActivityWatch not running")
                return

            stats = self.sync_engine.sync()

            if stats.success or stats.events_sent > 0:
                # A successful (or partial) sync clears the failure streak.
                self._consecutive_sync_failures = 0
                # Partial success: some buckets may fail but data still syncs
                if stats.errors:
                    for err in stats.errors:
                        logger.warning(f"Partial sync: {err}")
                if self.queue.is_near_capacity():
                    pct = int(self.queue.capacity_percent() * 100)
                    self.tray.set_state(TrayState.QUEUE_WARNING, f"Queue {pct}% full")
                    logger.warning(f"Offline queue at {pct}% capacity")
                elif stats.events_queued > 0:
                    self.tray.set_state(TrayState.QUEUED)
                else:
                    if is_idle:
                        self.tray.set_state(TrayState.PAUSED, "Idle")
                    else:
                        self.tray.set_state(TrayState.SYNCING)
                if stats.events_sent > 0:
                    logger.info(f"Sync complete: {stats.events_sent} events synced")
            else:
                for err in stats.errors:
                    logger.warning(f"Sync failed: {err}")
                self.tray.set_state(
                    TrayState.ERROR,
                    stats.errors[0] if stats.errors else "Sync failed",
                )
                self._note_sync_failure(
                    stats.errors[0] if stats.errors else "Sync failed"
                )

            hours = self._fetch_hours_today()
            self.tray.update_stats(
                hours_today=hours if hours is not None else "---",
                last_sync=datetime.now().strftime("%H:%M"),
                queue_size=self.queue.size(),
            )

            if stats.events_sent > 0 or stats.events_queued > 0:
                gaps_info = (
                    f", {stats.gaps_filled} gaps filled"
                    if stats.gaps_filled > 0
                    else ""
                )
                logger.info(
                    f"Sync complete: {stats.events_sent} sent, "
                    f"{stats.events_queued} queued, {stats.events_filtered} filtered"
                    f"{gaps_info}"
                )

        except BetterFlowAuthError as e:
            self._handle_auth_error(e, source="sync")
        except Exception as e:
            logger.exception(f"Sync error: {e}")
            self.tray.set_state(TrayState.ERROR, "Sync error")
            self._note_sync_failure("Sync error", exc=e)
        finally:
            watchdog_cancelled.set()
            watchdog.cancel()
            self._sync_lock.release()

        # Heartbeat runs AFTER _sync_lock is released — no need to hold
        # the lock during a blocking HTTP call that can take 30s on timeout.
        # send_heartbeat_if_due returns the auth error rather than raising,
        # because raising in the post-lock tail would surface as an uncaught
        # exception in the APScheduler thread.
        auth_err = self.sync_engine.send_heartbeat_if_due(stats)
        if auth_err is not None:
            self._handle_auth_error(auth_err, source="heartbeat")

    def _note_sync_failure(self, reason: str, *, exc: Optional[BaseException] = None) -> None:
        """Track a hard sync failure and report once it becomes a streak.

        Called from within _do_sync (holding _sync_lock), so the counter needs
        no extra lock. Auth errors are handled separately and don't count — a
        re-login is expected, not a failure worth alerting on.
        """
        self._consecutive_sync_failures += 1
        if (
            self.error_reporter is not None
            and self._consecutive_sync_failures >= self._SYNC_FAILURE_ALERT_THRESHOLD
        ):
            self.error_reporter.capture(
                f"Sync failing repeatedly ({self._consecutive_sync_failures}×): {reason}",
                level="error",
                exc=exc,
                tags={"component": "sync"},
                context={"consecutive_failures": self._consecutive_sync_failures},
                fingerprint="sync-repeated-failure",
            )

    def _handle_auth_error(self, e: BetterFlowAuthError, *, source: str) -> None:
        """Trigger re-login on a 401/403 from sync or heartbeat."""
        logger.warning(f"Auth error during {source}: {e} — triggering re-login")
        self.logged_in = False
        self.tray.set_state(
            TrayState.WAITING_AUTH, "Session expired, re-login required"
        )
        if self._on_auth_error:
            self._on_auth_error()

    def _fetch_hours_today(self) -> str:
        return self.hours.fetch_hours_today()

    def _refresh_hours_today(self) -> None:
        self.hours.refresh_hours_today(logged_in=self.logged_in)

    def _fetch_trends(self) -> None:
        self.hours.fetch_trends(logged_in=self.logged_in)

    def reset_trends(self) -> None:
        self.hours.reset()

    def _expire_old_queue_events(self) -> None:
        """Remove queue events older than 30 days."""
        try:
            self.queue.expire_old(max_age_days=30)
        except Exception as e:
            logger.debug(f"Failed to expire old queue events: {e}")

class BetterFlowApp:
    """Main application orchestrator.

    Wires components together, handles lifecycle (start / shutdown),
    and routes tray-menu and system events to the appropriate handler.
    """

    def __init__(self):
        """Initialize the application."""
        self.config = Config.load()
        setup_logging(self.config.debug_mode)

        logger.info("BetterFlow starting...")
        logger.info(f"Using API URL: {self.config.api_url}")

        # Note: clear_notifications() is NOT called here or in background
        # startup. NSUserNotificationCenter deadlocks when called before the
        # Cocoa run loop is fully active (main thread) or from a non-main
        # thread in PyInstaller bundles. Stale notifications from a crashed
        # session are harmless and dismissed by macOS automatically.

        logger.info("Initializing components...")
        # Initialize AW process manager
        self.aw_manager = AWManager(
            aw_port=self.config.aw.port,
            afk_timeout=self.config.aw.afk_timeout_minutes * 60,
        )

        # Initialize components
        self.aw = AWClient(
            host=self.config.aw.host,
            port=self.config.aw.port,
        )
        self.bf = BetterFlowClient(
            api_url=self.config.api_url,
            compress=self.config.sync.compress,
        )
        self.queue = OfflineQueue()
        self.keychain = KeychainManager()
        logger.info("Core clients initialized")
        if self.config.privacy.track_display_info:
            self.display_tracker = start_display_tracker()
        else:
            self.display_tracker = None

        # Active browser-tab URL tracker (macOS). Off unless enabled; without it
        # browser events carry no URL and collapse to generic "browsing".
        if self.config.privacy.track_browser_urls:
            self.browser_tracker = start_browser_tracker()
        else:
            self.browser_tracker = None

        # In-process window watcher on macOS (inherits Accessibility permission)
        self.window_watcher = None
        self.input_watcher = None
        if sys.platform == "darwin":
            try:
                from .sync.macos_window_watcher import MacOSWindowWatcher
                from .sync.macos_input_watcher import MacOSInputWatcher
            except ImportError:
                from sync.macos_window_watcher import MacOSWindowWatcher
                from sync.macos_input_watcher import MacOSInputWatcher
            self.window_watcher = MacOSWindowWatcher(self.aw)
            self.input_watcher = MacOSInputWatcher(self.aw)
            self.aw_manager.disable_component("bf-window-tracker")

        logger.info("Creating sync engine...")
        self.sync_engine = SyncEngine(
            aw=self.aw,
            bf=self.bf,
            queue=self.queue,
            config=self.config,
            on_config_updated=self._on_config_updated,
            display_tracker=self.display_tracker,
            browser_tracker=self.browser_tracker,
        )

        logger.info("Sync engine created")
        self.login_manager = LoginManager(self.bf, self.keychain)

        # Tray icon
        self.tray = TrayIcon(
            on_login=self._on_login,
            on_pause=self._on_pause,
            on_resume=self._on_resume,
            on_preferences=self._on_preferences,
            on_logout=self._on_logout,
            on_quit=self._on_quit,
            on_project_change=self._on_project_change,
            on_private_toggle=self._on_private_toggle,
            on_sync_now=self._on_sync_now,
            on_export_logs=self._on_export_logs,
            on_start_break=self._on_start_break,
            on_end_break=self._on_end_break,
            on_cancel_login=self._on_cancel_login,
            on_install_update=self._on_install_update,
            on_check_update=lambda: self.update_handler._periodic_update_check(),
            on_tray_died=self._on_tray_died,
        )
        self.tray.set_config(self.config)

        # Failure reporting to the betterqa-bot logs channel. A no-op until a
        # project-scoped DSN is provided via BETTERFLOW_ERROR_DSN, so dev/test
        # runs never phone home. Install global crash hooks so unhandled
        # exceptions on any thread are reported before the process dies.
        self.error_reporter = error_reporter.from_env(
            release=_VERSION,
            context_provider=self._error_context,
        )
        error_reporter.install_crash_hooks(self.error_reporter)

        # Sync coordinator (created before reminder manager so callback can be injected cleanly)
        self.coordinator = SyncCoordinator(
            config=self.config,
            aw=self.aw,
            bf=self.bf,
            queue=self.queue,
            sync_engine=self.sync_engine,
            tray=self.tray,
            aw_manager=self.aw_manager,
            error_reporter=self.error_reporter,
        )
        self.coordinator._on_auth_error = self._on_session_expired
        self.coordinator._input_watcher = self.input_watcher
        self.coordinator.idle_mgr._on_idle_pause = self._on_idle_pause

        # Reminder manager (created after coordinator for clean callback injection)
        self.reminder_manager = ReminderManager(self.config.reminders)
        self.coordinator.reminder_manager = self.reminder_manager
        self.coordinator.break_mgr.reminder_manager = self.reminder_manager

        # State
        self._shutdown_done = False
        self._shutdown_lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._pause_state_lock = threading.RLock()
        self._login_lock = threading.Lock()
        self._startup_thread: Optional[threading.Thread] = None
        self._system_events_started = False
        self._system_events_lock = threading.Lock()

        # Sub-handlers
        self.update_handler = UpdateHandler(self.tray, self.config, self.coordinator, _VERSION)
        self.sys_events = SystemEventHandler(
            sync_engine=self.sync_engine,
            tray=self.tray,
            coordinator=self.coordinator,
            reminder_manager=self.reminder_manager,
            bf=self.bf,
            aw=self.aw,
            pause_state_lock=self._pause_state_lock,
            shutdown_fn=self._shutdown,
        )
        self.sys_events.update_handler = self.update_handler

    def _set_startup_status(self, message: str) -> None:
        """Keep the tray visible while background startup progresses."""
        self.tray.set_state(TrayState.STARTING, message)

    def _start_watchers(self) -> None:
        """Start in-process watchers after the tracker server is available."""
        if sys.platform == "darwin":
            if not check_accessibility() or not check_input_monitoring():
                logger.info("Missing macOS permissions, attempting TCC grant")
                grant_tcc_permissions()
        if self.window_watcher:
            self.window_watcher.start()
        if self.input_watcher:
            self.input_watcher.start()

    def _ensure_system_event_listener(self) -> None:
        """Start system event listeners once without blocking tray startup."""
        with self._system_events_lock:
            if self._system_events_started:
                return
            self._system_events_started = True
        try:
            from urllib.parse import urlparse

            parsed_api = urlparse(self.config.api_url)
            api_host = parsed_api.hostname or ""
            api_port = parsed_api.port or (443 if parsed_api.scheme == "https" else 80)
            start_system_event_listener(
                on_sleep=self._on_system_sleep,
                on_wake=self._on_system_wake,
                on_shutdown=self._on_system_shutdown,
                on_network_change=self._on_network_change,
                on_screen_lock=self._on_screen_lock,
                on_screen_unlock=self._on_screen_unlock,
                reachability_host=api_host,
                reachability_port=api_port,
            )
        except Exception:
            logger.exception("Failed to start system event listeners")

    def _ensure_update_checks_started(self) -> None:
        self.update_handler.ensure_update_checks_started()

    def _finish_logged_in_startup(
        self,
        state,
        *,
        send_greeting: bool,
        initial_permissions_delay_seconds: float = 5.0,
    ) -> None:
        """Complete post-login startup work after the tray is already visible."""
        if self._shutdown_event.is_set():
            return

        self.coordinator.logged_in = True
        self.coordinator._mark_logged_in_for_warn()
        self.tray.set_user(state.user_email, state.user_name, state.user_role)
        self._set_startup_status("Loading your workspace...")

        try:
            self.sync_engine.fetch_server_config()
        except Exception:
            logger.exception("Failed to fetch server configuration during startup")

        self.coordinator.fetch_projects()
        self._check_stale_session()
        self.coordinator.start()
        self._ensure_update_checks_started()

        if send_greeting:
            first_name = (state.user_name or "").split()[0] if state.user_name else ""
            greeting = f"{_greeting()}, {first_name}!" if first_name else f"{_greeting()}!"
            send_notification(greeting, _day_greeting())

    def _background_startup(self, wizard_login_state=None) -> None:
        """Restore session and services without delaying tray visibility."""
        if not self._login_lock.acquire(timeout=1):
            logger.warning("Startup init skipped: login lock busy")
            return
        try:
            self._set_startup_status("Restoring session...")
            if wizard_login_state and wizard_login_state.logged_in:
                state = wizard_login_state
            else:
                state = self.login_manager.try_auto_login()
            if self._shutdown_event.is_set():
                return

            self._set_startup_status("Starting trackers...")
            self.aw_manager.start()
            if self._shutdown_event.is_set():
                return

            self._start_watchers()
            self._ensure_system_event_listener()

            if state.logged_in:
                self._finish_logged_in_startup(state, send_greeting=True)
            else:
                self.coordinator.logged_in = False
                self.tray.set_state(TrayState.WAITING_AUTH, "Waiting for browser login...")
                self.coordinator._maybe_warn_login_required(source="startup")
                self._ensure_update_checks_started()

            logger.info("Background startup complete")
        finally:
            self._login_lock.release()

    def run(self) -> None:
        """Run the application."""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        # Ignore SIGPIPE to prevent silent death when writing to a socket
        # whose remote end was closed (e.g. network drops mid-request).
        # Python normally sets SIG_IGN at startup, but PyInstaller's
        # bootloader or Rosetta 2 translation can reset it to SIG_DFL.
        if hasattr(signal, "SIGPIPE"):
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)

        # Apply an update staged in a previous session before anything else —
        # a fast local replace + relaunch into the new version. No-op if nothing
        # newer is staged. Relaunches (os._exit) on success.
        self._apply_staged_update_on_launch()

        # macOS: if launched from the DMG, ~/Downloads, or a Gatekeeper
        # translocation path, offer to move into /Applications and relaunch
        # from there. No-op when already installed or on other platforms.
        self._maybe_relocate_to_applications()

        # Self-heal auto-start: if config says it should be on but the OS-level
        # LaunchAgent isn't actually loaded (drift from a prior install,
        # manual launchctl bootout, or migration to a new bundle path),
        # re-bootstrap it. No-op in dev mode.
        if self.config.auto_start:
            try:
                try:
                    from .autostart import ensure_synced as ensure_autostart_synced
                except ImportError:
                    from autostart import ensure_synced as ensure_autostart_synced
                ensure_autostart_synced()
            except Exception as e:
                logger.warning("Auto-start sync failed (non-fatal): %s", e)

        # First-run setup wizard
        wizard_login_state = None
        if not self.config.setup_complete:
            try:
                from .ui.setup_wizard import show_setup_wizard
            except ImportError:
                from ui.setup_wizard import show_setup_wizard

            result = show_setup_wizard(self.config, self.login_manager)
            if not result.completed:
                logger.info("Setup wizard cancelled — exiting")
                return
            self.config.setup_complete = True
            # Ensure auto-start is enabled after first setup
            if not self.config.auto_start:
                try:
                    try:
                        from .autostart import set_auto_start
                    except ImportError:
                        from autostart import set_auto_start
                    if set_auto_start(True):
                        self.config.auto_start = True
                except Exception as e:
                    logger.warning("Auto-start enable failed (non-fatal): %s", e)
            self.config.save()
            if result.logged_in and result.login_state:
                wizard_login_state = result.login_state

        # macOS Input Monitoring is a hard precondition. Unlike the first-run
        # wizard, this gate runs on EVERY launch and keeps showing until the
        # grant is present — if the user revoked it, or a new build's signature
        # dropped it, they get the gate again rather than silently-degraded
        # tracking (active time with no input, which the server flags).
        #
        # macOS only exposes a freshly-granted Input Monitoring permission to a
        # newly-launched process, so the gate relaunches the app to re-check
        # rather than polling in place (which can never see the new grant).
        if not self._ensure_macos_permissions():
            return

        self._set_startup_status("Starting...")
        self._startup_thread = threading.Thread(
            target=self._background_startup,
            args=(wizard_login_state,),
            daemon=True,
            name="startup-thread",
        )
        self._startup_thread.start()

        logger.info("BetterFlow tray starting")
        try:
            self.tray.run_blocking()
        finally:
            self._shutdown()

    def _ensure_macos_permissions(self) -> bool:
        """Block on the permission gate until Input Monitoring is granted.

        Returns True to continue startup, False if the app should exit. On
        non-macOS platforms this is always True (no such permission).

        Input Monitoring is the single required grant. The gate returns
        'granted' (proceed), 'restart' (relaunch so a freshly-toggled grant
        takes effect, then re-check) or 'quit' (user closed it — the app must
        not run without approval).
        """
        if sys.platform != "darwin":
            return True
        try:
            from .ui.permissions import input_monitoring_active
            from .ui.setup_wizard import run_permission_gate
        except ImportError:
            from ui.permissions import input_monitoring_active  # type: ignore[no-redef]
            from ui.setup_wizard import run_permission_gate  # type: ignore[no-redef]

        # prompt=True calls IOHIDRequestAccess, which registers BetterFlow in
        # System Settings > Input Monitoring (so there's a toggle) and surfaces
        # the system prompt — done on every launch until granted.
        if input_monitoring_active(prompt=True):
            return True

        result = run_permission_gate(self.config)
        if result == "granted":
            return True
        if result == "restart":
            logger.info("Relaunching to apply newly granted permissions")
            self._relaunch()
            return False
        logger.info("Tracking permissions not granted — exiting")
        return False

    def _relaunch(self) -> None:
        """Relaunch the app so a freshly granted macOS permission is picked up.

        Never returns — the process exits after spawning its replacement.
        """
        try:
            if getattr(sys, "frozen", False):
                # .../BetterFlow.app/Contents/MacOS/BetterFlow -> the .app bundle
                exe = Path(sys.executable)
                bundle = exe.parents[2] if len(exe.parents) >= 3 else None
                if bundle is not None and bundle.suffix == ".app":
                    self._spawn_deferred_open(bundle)
                else:
                    subprocess.Popen([str(exe)])
            else:
                # Dev mode: re-exec the same interpreter + args.
                os.execv(sys.executable, [sys.executable, *sys.argv])
                return
        except Exception:
            logger.exception("Relaunch failed")
        os._exit(0)

    def _spawn_deferred_open(self, target: Path) -> None:
        """Spawn a detached helper that runs ``open <target>`` only after THIS
        process has exited.

        macOS ``open`` on a still-running app reactivates the current
        (about-to-exit) instance instead of launching a new one, so the caller
        must ``os._exit`` immediately after calling this.
        """
        subprocess.Popen(
            [
                "/bin/sh",
                "-c",
                f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.2; done; "
                f"open {shlex.quote(str(target))}",
            ],
            start_new_session=True,
        )

    def _maybe_relocate_to_applications(self) -> None:
        """On macOS, offer to move a non-installed .app into /Applications.

        A frozen bundle launched from the DMG, ~/Downloads, or a Gatekeeper
        translocation path is not in /Applications, so it never appears under
        Applications/Launchpad and its code-signing identity (and the TCC
        grants tied to it) churn across updates. Offer a one-click move, then
        relaunch from the installed copy. Best-effort: any failure is logged
        and the app keeps running from its current location.
        """
        if sys.platform != "darwin" or not getattr(sys, "frozen", False):
            return
        try:
            exe = Path(sys.executable)
            bundle = exe.parents[2] if len(exe.parents) >= 3 else None
            if bundle is None or bundle.suffix != ".app":
                return
            if str(bundle).startswith("/Applications/"):
                return  # already installed

            dest = Path("/Applications") / bundle.name

            try:
                import tkinter as tk
                from tkinter import messagebox
            except Exception:
                logger.warning("tkinter unavailable; skipping move-to-Applications prompt")
                return

            root = tk.Tk()
            root.withdraw()
            try:
                move = messagebox.askyesno(
                    "Move to Applications?",
                    "BetterFlow works best from your Applications folder.\n\n"
                    "Move it there now? This keeps permissions and automatic "
                    "updates working correctly.",
                )
            finally:
                root.destroy()
            if not move:
                return

            # ditto preserves the Developer ID signature + notarization ticket
            # (shutil.copytree strips the xattrs that carry them). Replace any
            # older copy first so we don't merge two versions.
            if dest.exists():
                subprocess.run(["rm", "-rf", str(dest)], check=True)
            subprocess.run(["ditto", str(bundle), str(dest)], check=True)

            logger.info("Relocated to %s; relaunching from there", dest)
            self._spawn_deferred_open(dest)
            os._exit(0)
        except Exception:
            logger.exception("Move to Applications failed; continuing from current location")

    # -- Event handlers ---------------------------------------------------

    def _try_auto_install(self) -> None:
        self.update_handler.try_auto_install()

    def _apply_staged_update_on_launch(self) -> None:
        """Apply a previously staged update (newer than current) before the UI
        and services start, then relaunch into it. No-op otherwise.

        Loop-safety lives in self_updater.get_staged_update (only strictly-newer
        staged builds are applied, and staging is cleared before applying)."""
        if not self.config.check_updates:
            return
        try:
            try:
                from .self_updater import apply_staged_update
            except ImportError:
                from self_updater import apply_staged_update
            apply_staged_update(_VERSION)
        except Exception:
            logger.exception("Staged update apply on launch failed (continuing startup)")

    def _on_install_update(self, asset_url: str) -> None:
        self.update_handler.on_install_update(asset_url)

    def _check_stale_session(self) -> None:
        """Check if previous session is still active on server (forgot to clock out)."""
        try:
            status = self.bf.get_status()
            session = status.get("data", {}).get("active_session")
            if session and session.get("is_active"):
                started = session.get("started_at", "unknown")
                logger.warning(
                    f"Stale session detected (started {started}) — "
                    "ending previous session before starting new one"
                )
                try:
                    self.bf.end_session("crash_recovery")
                except Exception:
                    logger.debug("Failed to end stale session during crash recovery", exc_info=True)
        except Exception as e:
            logger.debug(f"Stale session check failed: {e}")

    def _on_session_expired(self) -> None:
        """Bridge from `SyncCoordinator._handle_auth_error` into the App's
        notification + relogin pipeline.

        Distinct from the user-clicked `_on_login` so the notification only
        fires on the involuntary path (session died under the user). A user
        who explicitly clicks Login knows what they did; the case Emilian
        flagged was the silent path.
        """
        self.coordinator._maybe_warn_login_required(source="session_expired")
        self._on_login()

    def _on_login(self) -> None:
        """Handle explicit login action from tray.

        If a login flow is already in progress (e.g. from auto-relogin
        after logout), cancel it first so the new attempt can proceed.
        """
        # Cancel any in-progress flow so the lock is released
        self.login_manager.cancel_login()

        def do_browser_login():
            # Serialize login attempts via the lock. The previous
            # locked() pre-check was a TOCTOU race; the timeout here
            # is the correct guard against piling up login threads.
            if not self._login_lock.acquire(timeout=15):
                logger.warning("Could not acquire login lock after cancel")
                self.tray.set_state(TrayState.ERROR, "Login busy, please try again")
                return
            try:
                self.coordinator.logged_in = False
                self.tray.set_state(TrayState.WAITING_AUTH, "Waiting for browser login...")
                state = self.login_manager.login_via_browser()
                if state.logged_in:
                    self._finish_logged_in_startup(
                        state,
                        send_greeting=False,
                        initial_permissions_delay_seconds=1.0,
                    )
                    first_name = (state.user_name or "").split()[0] if state.user_name else ""
                    greeting = f"Welcome back, {first_name}!" if first_name else "Welcome back!"
                    send_notification(greeting, _day_greeting())
                else:
                    self.coordinator.logged_in = False
                    error = state.error or "Login failed"
                    self.tray.set_state(TrayState.ERROR, error)
                    send_notification("Login Failed", f"{error}. Use the tray menu to retry.")
            finally:
                self._login_lock.release()

        threading.Thread(target=do_browser_login, daemon=True, name="login-thread").start()

    def _on_cancel_login(self) -> None:
        """Cancel an in-progress browser login flow."""
        logger.info("Login cancelled by user")
        self.login_manager.cancel_login()
        self.tray.set_state(TrayState.ERROR, "Login cancelled")

    def _on_start_break(self) -> None:
        """Handle user starting a manual break from tray menu."""
        self.coordinator.start_break()

    def _on_end_break(self) -> None:
        """Handle user ending break early from tray menu."""
        self.coordinator.end_break(force=True)

    def _set_user_paused(self, paused: bool) -> None:
        """Flip the user-paused flag under the shared lock."""
        with self._pause_state_lock:
            self.sys_events._user_paused = paused
        self.coordinator.paused_by_network = False
        self.coordinator.clear_idle_pause(send_event=True)

    def _enter_paused_state(self) -> None:
        """Common pre-work shared by pause, private-on, and break-pause:
        flush idle state, cancel any running break, and pause the engine.
        """
        self._set_user_paused(True)
        # End break first (it may call resume internally), then re-pause
        # so the user-initiated pause always wins.
        if self.coordinator.is_on_break:
            self.coordinator.end_break(silent=True, force=True)

    def _on_pause(self) -> None:
        """Handle pause action."""
        self._enter_paused_state()
        self.sync_engine.pause()
        self.tray.set_paused(True)
        self.reminder_manager.on_tracking_stopped()
        send_notification("Tracking Paused", "Your activity is no longer being recorded.", sound=False)
        logger.info("Tracking paused")

    def _on_resume(self) -> None:
        """Handle resume action."""
        self._set_user_paused(False)
        self.sync_engine.resume()
        self.tray.set_paused(False)
        self.reminder_manager.on_tracking_started()
        send_notification("Tracking Resumed", "Your activity is being recorded again.", sound=False)
        logger.info("Tracking resumed")

    def _on_project_change(self, project: Optional[dict]) -> None:
        """Handle project switch from tray."""
        if project:
            logger.info(f"Switched to project: {project['name']}")
        else:
            logger.info("Cleared project selection")
        self.sync_engine.set_current_project(project)

    def _on_private_toggle(self, private: bool) -> None:
        """Handle private time toggle (also serves as pause/resume)."""
        if private:
            logger.info("Private time started — recording paused")
            self._enter_paused_state()
            self.sync_engine.set_private_mode(True)
            self.sync_engine.pause()
            self.tray.set_paused(True)
            self.reminder_manager.on_tracking_stopped()
            self.reminder_manager.on_private_started()
            send_notification("Private Time", "Tracking is paused — your activity is private.", sound=False)
        else:
            logger.info("Private time ended — recording resumed")
            self._set_user_paused(False)
            self.sync_engine.set_private_mode(False)
            self.sync_engine.resume()
            self.tray.set_paused(False)
            self.reminder_manager.on_private_ended()
            self.reminder_manager.on_tracking_started()
            send_notification("Private Time Ended", "Tracking has resumed.", sound=False)

    def _on_idle_pause(self, paused: bool) -> None:
        """Handle idle pause/resume — also pause/resume input watcher and slow window polling."""
        if self.input_watcher:
            if paused:
                # Keep the event tap alive while AFK. Restarting the macOS input
                # watcher after long idle periods has proven unreliable on some
                # installs and can leave the sync engine with no input telemetry.
                logger.info("Input watcher left running (user idle)")
            else:
                if not self.input_watcher.is_running:
                    self.input_watcher.start()
                    logger.info("Input watcher resumed (user active)")
        if self.window_watcher:
            self.window_watcher.set_poll_interval(5.0 if paused else 2.0)
        # Stop/start the break reminder timer so it doesn't fire during AFK
        if self.reminder_manager:
            if paused:
                self.reminder_manager.on_tracking_stopped()
            else:
                self.reminder_manager.on_tracking_started()
        if paused:
            self._try_auto_install()

    def _on_sync_now(self) -> None:
        """Handle sync now action from tray."""
        if not self.coordinator.scheduler.running:
            logger.debug("Sync Now ignored: scheduler not running (not logged in?)")
            return
        logger.info("Manual sync triggered")
        self.coordinator.trigger_sync()

    def _on_system_sleep(self) -> None:
        if self._shutdown_event.is_set():
            return
        self.sys_events.on_system_sleep()

    def _on_system_wake(self) -> None:
        if self._shutdown_event.is_set():
            return
        self.sys_events.on_system_wake()

    def _on_system_shutdown(self) -> None:
        self.sys_events.on_system_shutdown()

    def _on_screen_lock(self) -> None:
        if self._shutdown_event.is_set():
            return
        self.sys_events.on_screen_lock()

    def _on_screen_unlock(self) -> None:
        if self._shutdown_event.is_set():
            return
        self.sys_events.on_screen_unlock()

    def _on_network_change(self, is_online: bool) -> None:
        if self._shutdown_event.is_set():
            return
        self.sys_events.on_network_change(is_online)

    def _on_export_logs(self) -> None:
        """Export logs and redacted config to a zip file on the Desktop."""
        try:
            log_dir = Config.get_log_dir()
            desktop = Path.home() / "Desktop"
            if not desktop.exists():
                desktop = Path.home()
            zip_path = desktop / f"betterflow-logs-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                # Add log files
                for log_file in log_dir.glob("*.log*"):
                    zf.write(log_file, f"logs/{log_file.name}")

                # Add redacted config
                config_file = Config.get_config_file()
                if config_file.exists():
                    import json
                    with open(config_file) as f:
                        cfg = json.load(f)
                    # Redact sensitive fields
                    cfg.pop("device_id", None)
                    zf.writestr("config-redacted.json", json.dumps(cfg, indent=2))

            logger.info(f"Logs exported to {zip_path}")

            # Open the containing folder
            import subprocess
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(zip_path)])
            elif sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(zip_path)])
        except Exception as e:
            logger.error(f"Failed to export logs: {e}")

    def _on_config_updated(self) -> None:
        """Handle server config update — apply AFK timeout to AWManager."""
        self.aw_manager.set_afk_timeout(self.config.aw.afk_timeout_minutes * 60)

    def _on_preferences(self, key: str, value) -> None:
        """Handle a preference change from tray menu."""
        if key == "sync_interval":
            self.config.sync.interval_seconds = value
            self.coordinator.reschedule(value)
        elif key == "hash_titles":
            self.config.privacy.hash_titles = value
        elif key == "domain_only_urls":
            self.config.privacy.domain_only_urls = value
        elif key == "auto_categorize":
            self.config.privacy.auto_categorize = value
        elif key == "track_display_info":
            self.config.privacy.track_display_info = value
        elif key == "auto_start":
            try:
                from .autostart import set_auto_start
            except ImportError:
                from autostart import set_auto_start
            set_auto_start(value)
            self.config.auto_start = value
        elif key == "debug_mode":
            self.config.debug_mode = value
            setup_logging(value)
        elif key == "break_reminders_enabled":
            self.config.reminders.break_reminders_enabled = value
            self.reminder_manager.update_settings(self.config.reminders)
        elif key == "break_interval_hours":
            self.config.reminders.break_interval_hours = value
            self.reminder_manager.update_settings(self.config.reminders)
        elif key == "private_reminders_enabled":
            self.config.reminders.private_reminders_enabled = value
            self.reminder_manager.update_settings(self.config.reminders)
        elif key == "private_interval_minutes":
            self.config.reminders.private_interval_minutes = value
            self.reminder_manager.update_settings(self.config.reminders)
        elif key == "update_channel":
            self.config.update_channel = value
            try:
                check_for_update(
                    _VERSION,
                    channel=value,
                    callback=self._on_update_available,
                )
            except Exception:
                logger.warning("Failed to check for updates after channel change")
        else:
            logger.warning(f"Unknown preference key: {key}")
            return
        self.config.save()
        logger.info(f"Preference changed: {key} = {value}")

    def _on_logout(self) -> None:
        """Handle logout action.

        All work is offloaded to a background thread so the tray callback
        (main thread on macOS) is never blocked for more than a few ms.
        """
        def _do_logout() -> None:
            # Phase 1 — shutdown (lock held).
            # Acquire login lock to prevent a concurrent _on_login thread from
            # starting the coordinator between our stop() and start() calls.
            if not self._login_lock.acquire(blocking=True, timeout=15.0):
                logger.warning("Logout aborted: could not acquire login lock within 15s")
                return
            try:
                # Wait for any in-progress sync to finish (up to 10s)
                deadline = time.monotonic() + 10.0
                while self.coordinator.sync_in_progress and time.monotonic() < deadline:
                    time.sleep(0.1)

                # Clear notifications from previous session
                clear_notifications()

                # Cancel any active break before stopping
                if self.coordinator.is_on_break:
                    self.coordinator.end_break(silent=True, force=True)

                # End server session before stopping
                self.sync_engine.shutdown()
                self.login_manager.logout()
                self.coordinator.logged_in = False
                self.coordinator.reset_trends()
                self.tray.set_user(None)
                self.tray.set_projects([], None)
                logger.info("Logged out")

                self.coordinator.stop()
                self.tray.set_state(TrayState.WAITING_AUTH, "Waiting for browser login...")
            finally:
                # Release lock before the browser auth wait (up to 120s).
                # The tray is now in WAITING_AUTH state so no login button is
                # visible and no concurrent _on_login thread can be triggered.
                self._login_lock.release()

            # Phase 2 — browser auth (lock NOT held; at most one flow can run
            # because the tray state prevents the user from clicking Login again).
            # Guard: if an auth-error callback already triggered a re-login
            # between lock release and here, skip the second browser window.
            if self._shutdown_event.is_set() or self.coordinator.logged_in:
                return
            # Cancel any auth-error relogin that fired between lock release
            # and here — prevents opening a second browser window.
            self.login_manager.cancel_login()
            state = self.login_manager.login_via_browser()
            if self._shutdown_event.is_set():
                return
            if state.logged_in:
                self._finish_logged_in_startup(
                    state,
                    send_greeting=False,
                    initial_permissions_delay_seconds=1.0,
                )
                first_name = (state.user_name or "").split()[0] if state.user_name else ""
                greeting = f"Welcome back, {first_name}!" if first_name else "Welcome back!"
                send_notification(greeting, _day_greeting())
            else:
                self.coordinator.logged_in = False
                error = state.error or "Login failed"
                self.tray.set_state(TrayState.ERROR, error)
                send_notification("Login Failed", f"{error}. Use the tray menu to retry.")

        threading.Thread(target=_do_logout, daemon=True, name="logout-thread").start()

    def _on_quit(self) -> None:
        """Handle quit action."""
        logger.info("Quit requested")
        self._shutdown_event.set()
        self.tray.stop()

    def _on_tray_died(self) -> None:
        """Handle dead tray icon (ghost process).

        When the NSStatusItem is deallocated but the NSApplication run loop
        keeps spinning, we get a ghost process with scheduler jobs still
        firing (sync, break reminders, notifications) but no visible icon.

        We run _shutdown() to clean up resources, then os._exit(1) because
        the main thread is stuck in pystray's run_blocking() which will
        never return from the dead Cocoa event loop.
        """
        logger.critical("Tray icon died — force-exiting to prevent ghost process")
        # Block so the report leaves the machine before os._exit kills the
        # daemon sender thread.
        self.error_reporter.capture(
            "Tray icon died — agent force-exiting (ghost process)",
            level="fatal",
            tags={"component": "tray"},
            fingerprint="tray-died",
            block=True,
        )
        self._shutdown()
        os._exit(1)

    # -- Failure reporting ------------------------------------------------

    def _error_context(self) -> dict:
        """Build the who/what context attached to every error report.

        Reads the current user from the tray model under its lock so a report
        names which user's agent failed.
        """
        ctx: dict = {"app_version": _VERSION}
        try:
            with self.tray.model.lock:
                ctx["user_email"] = self.tray.model.user_email
                ctx["user_name"] = self.tray.model.user_name
                ctx["user_role"] = self.tray.model.user_role
        except Exception as e:
            logger.debug("Could not read user context for error report: %s", e)
        try:
            ctx["device_id"] = self.bf.device_id
        except Exception as e:
            logger.debug("Could not read device_id for error report: %s", e)
        return ctx

    def _signal_handler(self, signum, frame) -> None:
        """Handle shutdown signals.

        Sets the shutdown event and stops the tray to break out of
        run_blocking() immediately. Without the tray.stop() call, a
        SIGINT during a 120s OAuth wait would hang until the browser
        timeout expires. pystray's stop() on macOS posts to the Cocoa
        run loop, making it safe to call here.
        """
        self._shutdown_event.set()
        try:
            self.tray.stop()
        except Exception:
            pass

    # -- Lifecycle --------------------------------------------------------

    def _shutdown(self) -> None:
        """Shutdown the application. Safe to call multiple times."""
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
        # Signal late system-event callbacks (network/sleep/lock) to no-op
        # before they touch torn-down resources like the offline queue.
        self._shutdown_event.set()
        logger.info("Shutting down...")

        # Flush idle event before stopping (otherwise idle period is lost)
        self.coordinator.flush_idle_event()
        clear_notifications()
        self.coordinator.stop()
        self.sync_engine.shutdown()
        if self.window_watcher:
            self.window_watcher.stop()
        if self.input_watcher:
            self.input_watcher.stop()
        if self.display_tracker is not None:
            self.display_tracker.stop()
        if getattr(self, "browser_tracker", None) is not None:
            self.browser_tracker.stop()
        # Clean up macOS notification observers (M5)
        try:
            if sys.platform == "darwin":
                try:
                    from .system_events import cleanup_observers
                except ImportError:
                    from system_events import cleanup_observers
                cleanup_observers()
        except Exception:
            logger.debug("cleanup_observers failed during shutdown", exc_info=True)
        self.aw.close()
        self.bf.close()
        self.queue.close()
        self.aw_manager.stop()

        logger.info("Shutdown complete")

    def __enter__(self) -> "BetterFlowApp":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._shutdown()


class SingleInstanceLock:
    """File-based single-instance lock using advisory locking."""

    def __init__(self):
        self._file = None
        self._path: Optional[str] = None

    def acquire(self) -> bool:
        """Try to acquire the lock. Returns True on success."""
        if self._path is None:
            try:
                self._path = os.path.join(
                    Config.get_config_dir(), ".betterflow.lock"
                )
            except Exception:
                return True  # fail open - don't block startup
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._file = open(self._path, "a+")  # noqa: SIM115
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._file.seek(0)
            self._file.truncate(0)
            self._file.write(str(os.getpid()))
            self._file.flush()
            return True
        except OSError:
            self._file.close()
            self._file = None
            return False

    def release(self) -> None:
        """Release the lock and clean up."""
        if self._file and self._path:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    try:
                        msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError as e:
                        logger.debug("msvcrt unlock failed: %s", e)
                else:
                    import fcntl
                    fcntl.flock(self._file, fcntl.LOCK_UN)
                self._file.close()
                os.unlink(self._path)
            except OSError as e:
                logger.debug("PidLock release raised: %s", e)
            self._file = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False


_instance_lock = SingleInstanceLock()


def main() -> None:
    """Main entry point."""
    if not _instance_lock.acquire():
        print("BetterFlow is already running.")
        sys.exit(0)

    try:
        with BetterFlowApp() as app:
            app.run()
    finally:
        _instance_lock.release()


if __name__ == "__main__":
    main()
