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

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

# Support both relative imports (module) and absolute imports (PyInstaller)
try:
    from .__init__ import __version__
    from .auth import KeychainManager, LoginManager
    from .aw_manager import IDLE_BLIND_RESTART_THRESHOLD, AWManager
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
    from aw_manager import IDLE_BLIND_RESTART_THRESHOLD, AWManager
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

        # Let the heartbeat carry agent-health telemetry. We own the AwManager
        # (idle-tracker restart count + AFK/window event ages) and the
        # sync-failure counter, which the SyncEngine does not — so the provider
        # lives here and is handed to the engine.
        self.sync_engine.health_provider = self._build_health_telemetry
        # So a failed logs_requested upload surfaces to the ops ingest (it can't
        # surface via the log itself — that's the file we couldn't fetch).
        self.sync_engine.error_reporter = self.error_reporter
        # Single source of truth for the in-process-AFK flag: the engine publishes
        # its per-cycle decision straight to aw_manager on the path where the
        # decision is made. The 60s _reconcile_inproc_afk_flag below is now only a
        # backstop for cycles that don't reach that decision point (paused/private,
        # AW down, bucket-fetch error). Before this the timer was the ONLY writer —
        # and it silently died for a whole release in Bug A (#76/#78), leaving the
        # flag stale.
        self.sync_engine.inproc_afk_flag_sink = self.aw_manager.set_inproc_afk_active

        # _tick_60s sub-tasks each run under a try/except so one failure can't kill
        # the scheduler — but that also HID Bug A (a reconcile that threw every 60s
        # for a whole release, found only by reading fleet logs). Track consecutive
        # per-task failures and escalate ONCE past a threshold to the ops ingest so
        # the same silent-breakage class surfaces in minutes, not on a log dive.
        self._tick_failure_counts: dict[str, int] = {}
        self._tick_failure_reported: set[str] = set()

        self.scheduler = BackgroundScheduler()
        self._last_tick: Optional[datetime] = None

        # Working-hours capture policy, injected by BetterFlowApp (which owns the
        # trackers) the same way _on_auth_error / on_private_auto_end are. Declared
        # here with a None default ON PURPOSE: the first cut of this referenced
        # BetterFlowApp._apply_capture_policy directly from _tick_60s, but _tick_60s
        # lives on THIS class — so it raised AttributeError while building the
        # sub-task tuple, i.e. before the loop, i.e. outside _run_tick_task's
        # try/except. That killed the whole 60s tick every cycle: not just the
        # capture policy but idle detection, hours refresh, permissions and
        # reminders with it.
        self.apply_capture_policy: Optional[Callable[[], None]] = None

        # Consecutive hard sync failures. Reported to the logs channel once it
        # crosses the threshold (transient blips are normal and not reported).
        # Only mutated inside _do_sync, which holds _sync_lock, so no extra lock.
        self._consecutive_sync_failures = 0
        self._SYNC_FAILURE_ALERT_THRESHOLD = 3

        # Consecutive auth (401/403) failures. A single one is almost always
        # transient — a backend deploy, a momentary token-lookup blip — so
        # logging out on the first one needlessly stops tracking and leaves the
        # user "idle" until they re-login. Only treat the session as lost after
        # this many CONSECUTIVE failures; any successful sync resets the streak.
        # Mutated only inside _do_sync's call chain (sync thread), no extra lock.
        self._consecutive_auth_failures = 0
        self._AUTH_FAILURE_LOGOUT_THRESHOLD = 3

        # Consecutive cycles where the LOCAL ActivityWatch server is unreachable.
        # is_running() already resets+retries the HTTP session internally, so a
        # single failure here means a real stall — but we still debounce before
        # alarming the user with an Error state, and only force-restart the
        # (possibly hung-but-listening) server once it's clearly stuck rather
        # than on one transient blip. Mutated only inside _do_sync (sync thread).
        self._aw_unreachable_streak = 0
        self._AW_UNREACHABLE_ERROR_THRESHOLD = 2

        # Consecutive cycles where AW answered is_running() (/info) but the bucket
        # fetch (/buckets/) failed — a half-hung bf-data-service the is_running()
        # watchdog can't see (Liviu's 2 AM 503 storm: 133 "Failed to get buckets"
        # with zero recovery, cleared only by a manual restart). Separate from
        # _aw_unreachable_streak, which is reset whenever is_running() passes —
        # and is_running() DOES pass here, so reusing it would never escalate.
        self._aw_buckets_failed_streak = 0

        # Flags set by the app layer - protected by _state_lock
        self._logged_in = False
        self._paused_by_network = False
        self._state_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        # Serialises the capture-boundary arm path. arm_capture_boundary() is called
        # from three threads — the boundary worker (re-arm), the sync thread
        # (fetch_server_config → _on_config_updated), and the wake handler — and its
        # compute → remove_job → add_job sequence is non-atomic against the shared
        # mutable config, so an interleaving could leave a restricted user with NO
        # boundary job until the 30-min refetch. Mirrors _apply_capture_policy's
        # _capture_lock, which guards this exact class of transition.
        self._boundary_lock = threading.Lock()
        # True once an arm resolved to "no boundary" for a schedule the 60s self-heal
        # still considers restricted (known+enforced but EDGELESS — allows() never
        # flips within the 8-day walk cap, e.g. a full 00:00-23:59 all-days window or a
        # hand-edited working_days=[]). Without this, the self-heal would see
        # get_job()==None every tick and re-arm+log forever (~1440 lines/day + an 8-day
        # allows() re-walk each time). Set under _boundary_lock in arm_capture_boundary:
        # True when boundary is None, False when a boundary IS armed. Config updates
        # flow through arm, so the flag self-corrects when the schedule changes.
        self._boundary_edgeless = False
        # Wedge self-recovery (Cristian Dragota / sync:67a77a43-787, 2026-06-25):
        # a _do_sync that hangs past the watchdog holds _sync_lock forever, so
        # every later cycle skips and sync freezes while the heartbeat keeps
        # last_seen fresh. _sync_takeover_lock guards the acquire/re-arm decision;
        # a holder stamps _sync_started_at + _sync_holder so a later cycle can
        # tell a healthy in-flight run from a wedged zombie.
        self._sync_takeover_lock = threading.Lock()
        self._sync_started_at: Optional[float] = None
        self._sync_holder: Optional[threading.Lock] = None
        # Monotonic clock of the last successful sync round-trip; surfaced on the
        # heartbeat as sync_stale_seconds so the fleet can flag "alive but not
        # syncing" directly instead of inferring it from upload gaps.
        self._last_successful_sync: Optional[float] = None

        # Sub-managers (own their locks)
        self.break_mgr = BreakManager(sync_engine, tray, self.scheduler, config, reminder_manager)
        self.idle_mgr = IdleManager(sync_engine, tray, aw, config)
        self.hours = HoursTracker(bf, sync_engine, tray)

        # Optional callback wired by the app for auth-error re-login
        self._on_auth_error: Optional[Callable] = None
        # Wired by BetterFlowApp to end Private Time when the auto-end safety cap
        # is hit (the engine/tray/paused-flag teardown lives on the app). Called
        # with the elapsed seconds. None until wired (e.g. in tests).
        self.on_private_auto_end: Optional[Callable[[float], None]] = None

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
        # Count of "afk reported while input active" (blind-tracker) detections
        # this session — the literal "idle but the bucket has events" case.
        # Reported via heartbeat telemetry so the backend can flag the device.
        # Mutated under _idle_tracker_warn_lock. _detections is cumulative (for
        # the captured report's context); _window resets each heartbeat so the
        # reported telemetry is "detected since last heartbeat" — a recovered
        # tracker clears, instead of a cumulative count flagging it all session.
        self._blind_tracker_detections = 0
        self._blind_tracker_window = 0
        # Forced tracker restarts past this (in one session) means the restart
        # isn't converging (orphan tracker / missing Input Monitoring perm) —
        # escalate via a captured report instead of looping silently. Reuse the
        # aw_manager threshold (the same point at which it flags the tracker
        # blind) so the two can't drift out of sync.
        self._RESTART_LOOP_ALERT_THRESHOLD = IDLE_BLIND_RESTART_THRESHOLD
        # Latch so the escalation captures once per session, not on every sync
        # cycle once the (monotonic) restart count crosses the threshold.
        self._restart_loop_escalated = False

        # macOS permission-warning throttle. Input Monitoring can be revoked
        # silently (e.g. a new build changes the code signature and macOS drops
        # the TCC grant), which leaves us collecting window time but no
        # keystrokes/clicks — server-side that looks like a jiggler and flags
        # the day as suspicious. We surface a notification, throttled to one per
        # state-change plus a periodic re-warn so it can't spam the user.
        self._perm_lock = threading.Lock()
        self._input_tracking_ok: Optional[bool] = None  # None = not yet checked
        self._last_perm_warn_at: Optional[datetime] = None

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
        # A fresh login starts with a clean auth-failure streak so a later
        # transient 401 doesn't immediately re-cross the logout threshold.
        if value:
            self._consecutive_auth_failures = 0

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

    def record_wake(self, wake_ts: Optional[datetime] = None) -> None:
        """Anchor idle detection to a fresh system-wake instant.

        Called by SystemEventHandler.on_system_wake so a post-wake idle
        pause can never backdate its idle_start before this timestamp.
        See IdleManager.record_wake for the full rationale.
        """
        self.idle_mgr.record_wake(wake_ts)

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
            # Frequent frontmost-window sampler for the in-process window source.
            # Window focus changes far faster than the 60s sync cycle, so we
            # sample every few seconds to give the reconstructed per-app spans
            # real resolution. Only registered when the (dormant, opt-in) feature
            # is enabled — otherwise ~100% of the fleet would wake every 5s to run
            # a no-op, defeating the unified-60s-tick timer coalescing above. The
            # per-cycle 60s sample in _do_sync still covers a server-side enable
            # until the next restart picks up the persisted flag and adds this job.
            if self.config.sync.in_process_window:
                self.scheduler.add_job(
                    self._sample_window,
                    trigger=IntervalTrigger(seconds=self.WINDOW_SAMPLE_INTERVAL_SECONDS),
                    id="window_sample_job",
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

        # Arm the one-shot boundary trigger so capture stops/starts exactly at the
        # next work_start/work_end edge, not up to 60s late on the interval tick.
        self.arm_capture_boundary()

    def _disarm_capture_boundary(self) -> None:
        """Remove the one-shot boundary job if one is armed. Idempotent — a missing
        job is the normal case (unknown/unrestricted schedule) and not an error.

        Extracted so the several remove/JobLookupError sites read the same. Callers
        that need atomicity against a concurrent arm hold _boundary_lock themselves."""
        try:
            self.scheduler.remove_job("capture_boundary")
        except JobLookupError:
            pass

    def arm_capture_boundary(self) -> None:
        """Schedule a one-shot job at the next working-hours boundary.

        The 60s tick converges capture toward the schedule, but only every 60s, so
        a window that closes at 22:00 kept every in-process recorder running for up
        to a full tick past the edge. sync() already stops UPLOADING at the exact
        instant (a live now() read against the same allows()), so nothing leaves the
        machine — but local RECORDING had a tail, which the enforcement docstrings
        say must not happen. This lands the hard stop/start ON the boundary and
        re-arms for the following one. Fleet-friendly: one extra wakeup per boundary
        crossing, not a fast poll across the whole fleet.

        Public because BetterFlowApp re-arms across the object boundary (server
        config / system wake). Re-armed by _fire_capture_boundary after each edge,
        and self-healed by the 60s tick if a misfire discarded the job.

        Thread-safe: the compute → remove → add sequence runs under _boundary_lock
        because three threads call this (boundary worker, sync thread, wake handler)
        and an interleaving could otherwise leave a restricted user with no armed
        boundary until the 30-min refetch."""
        if self.apply_capture_policy is None or not self.scheduler.running:
            return
        with self._boundary_lock:
            try:
                boundary = self.config.working_hours.next_boundary_after(
                    datetime.now(timezone.utc)
                )
            except Exception:
                logger.exception("Failed to compute next working-hours boundary")
                # Drop any boundary job left armed from an EARLIER (successful)
                # computation: it was scheduled against a now-superseded schedule and
                # could otherwise fire at a stale instant. Leaving the 60s tick as the
                # sole enforcement authority until the next successful arm is the
                # fail-safe reading. (_fire_capture_boundary re-reads allows(now) live,
                # so a stray fire is harmless — but a stale job should not linger.)
                self._disarm_capture_boundary()
                return
            try:
                if boundary is None:
                    # No edge to align to: either allows() is constant (unknown or
                    # unrestricted) OR the schedule is known+enforced but EDGELESS —
                    # allows() never flips within the 8-day walk (full all-day window,
                    # or hand-edited working_days=[]). Drop any job left from a previous
                    # restricted schedule, and remember the edgeless state so the 60s
                    # self-heal doesn't re-arm+log every tick against a schedule that
                    # legitimately arms nothing.
                    self._boundary_edgeless = True
                    self._disarm_capture_boundary()
                    return
                self.scheduler.add_job(
                    self._fire_capture_boundary,
                    trigger=DateTrigger(run_date=boundary),
                    id="capture_boundary",
                    replace_existing=True,
                    # Default misfire_grace_time=1 makes APScheduler DISCARD this
                    # one-shot if the process stalls >1s across the run date (a GIL
                    # pause, App Nap, a missed wake) — it logs once and never fires,
                    # so _fire_capture_boundary's finally never runs and the boundary
                    # stays disarmed until the next config refetch (30 min online,
                    # unbounded offline), silently reverting to the ~60s recording
                    # tail this job exists to kill. None = "fire however late"; a late
                    # fire is safe because _fire_capture_boundary re-reads allows(now)
                    # live. The 60s tick self-heal is the second backstop.
                    misfire_grace_time=None,
                )
                # A real edge exists: clear any prior edgeless state so the self-heal
                # resumes re-arming a genuinely missing (misfire-discarded) job.
                self._boundary_edgeless = False
                logger.info("Capture boundary armed for %s", boundary.isoformat())
            except Exception:
                # The remove/add is now inside the try too: the scheduler can be shut
                # down between the .running check above and here (a real TOCTOU), or a
                # jobstore op can fail. Such an exception must NOT escape — it would
                # land in _fire_capture_boundary's finally (killing re-arm) and in
                # _on_config_updated → fetch_server_config (whose except only catches
                # auth/client errors). Log and return; the 60s tick re-arms.
                logger.exception("Failed to arm capture boundary")

    def _fire_capture_boundary(self) -> None:
        """One-shot boundary job: enforce the policy AT the edge, then re-arm.

        Fail-closed: a re-arm failure can't wedge enforcement because the 60s tick
        still runs the same policy as the backstop."""
        try:
            if self.apply_capture_policy is not None:
                self.apply_capture_policy("boundary")
        finally:
            self.arm_capture_boundary()

    def _self_heal_capture_boundary(self) -> None:
        """60s-tick backstop for a boundary job APScheduler discarded on a misfire
        (a >1s stall across the run date). If the schedule is restricted (an edge
        exists to align to) but no boundary job is armed, re-arm it. arm is
        idempotent (replace_existing), so racing a concurrent arm is harmless."""
        if self.apply_capture_policy is None or not self.scheduler.running:
            return
        wh = self.config.working_hours
        # "Restricted" ⟺ next_boundary_after would return a boundary ⟺ known+enforced
        # (unknown/unrestricted schedules have no edge and legitimately arm nothing).
        if not (wh.known and wh.enforced):
            return
        # A known+enforced schedule can still be EDGELESS (allows() never flips within
        # the 8-day walk: a full 00:00-23:59 all-days window, or a hand-edited
        # working_days=[]). Such a schedule legitimately arms no job, so re-arming here
        # would churn — an 8-day allows() re-walk plus a misleading "job missing" log —
        # every 60s forever. arm_capture_boundary records that state; skip both the
        # re-arm and the log while it holds.
        if getattr(self, "_boundary_edgeless", False):
            return
        if self.scheduler.get_job("capture_boundary") is None:
            logger.info("Capture boundary job missing — re-arming (60s self-heal)")
            self.arm_capture_boundary()

    def stop(self, wait: bool = False) -> None:
        """Shut down the scheduler if running.

        wait=True blocks until any in-flight _do_sync finishes — used by the
        final app shutdown so a scheduled sync can't keep running after the
        offline queue is closed (the 'OfflineQueue has been closed' race,
        2026-06-17). The watchdog/30s drain cap in _do_sync bounds the wait."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=wait)

    def reschedule(self, interval_seconds: int) -> None:
        """Change the sync interval on the fly."""
        if self.scheduler.running:
            self.scheduler.reschedule_job(
                "sync_job",
                trigger=IntervalTrigger(seconds=interval_seconds),
            )

    def trigger_sync(self, job_id: str = "immediate_sync", force_reconcile: bool = False) -> None:
        """Schedule a one-off sync (e.g. after wake or network change).

        force_reconcile re-arms the start-of-day backlog reconcile so the sync
        re-sends any locally-stored events the server never received — used by
        manual "Sync Now" so one click recovers a stuck day without a restart.
        """
        if force_reconcile:
            self.sync_engine.request_backlog_reconcile()
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

    def _check_private_auto_end(self) -> None:
        """Hard safety cap: force-end Private Time after a configured number of
        continuous hours so a forgotten toggle can't silently zero a whole day's
        billable time (Raluca, 2026-06-25, ~11h private). v1.5.79 already ends
        private on sleep; this covers the awake-but-forgotten case. 0 disables.
        The actual teardown runs via the on_private_auto_end callback (wired by
        BetterFlowApp, which owns the engine/tray/paused-flag state)."""
        if self.reminder_manager is None or not self.sync_engine.is_private:
            return
        cap_hours = getattr(self.config.reminders, "private_auto_end_hours", 0) or 0
        if cap_hours <= 0:
            return
        elapsed = self.reminder_manager.private_elapsed_seconds()
        if elapsed is None or elapsed < cap_hours * 3600:
            return
        logger.warning("Private Time auto-ended after %.1fh (safety cap)", elapsed / 3600)
        if self.on_private_auto_end is not None:
            self.on_private_auto_end(elapsed)

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

        # When the in-process AFK source is active it is the SOLE billing source
        # (#70): the agent uploads its own AFK stream and the external
        # bf-idle-tracker bucket is ignored. A blind/stale external tracker then
        # no longer affects recorded work time, so its disagreement with the
        # input watcher is cosmetic. Skip the whole check — alarming the user
        # ("your activity isn't being recorded as work time") would be FALSE, and
        # paging ops produces the recurring blind-tracker error wave for nothing.
        # The aw_manager watchdog already suppresses its own blind detection on
        # the same condition; this closes the remaining un-gated path. When
        # in-process AFK is unavailable (Linux, or the OS idle clock went blind)
        # this flips False and the external tracker matters again — so the check
        # correctly resumes.
        try:
            if self.sync_engine.inproc_afk_active:
                return
        except Exception as e:  # never let a telemetry guard break the tick
            logger.debug("idle_tracker_health: inproc_afk_active check failed: %s", e)

        # Chronic-blind path: the watchdog gave up frequent restarts because
        # bf-idle-tracker stays stale across them — that's a missing Input
        # Monitoring grant (separate TCC subject), not a crash a restart fixes.
        # Re-prompt for permission (throttled) instead of churning forever.
        if getattr(self.aw_manager, "idle_tracker_blind", False):
            self._reprompt_idle_tracker_permission()

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
        # elsewhere. AWClient prefers the BetterFlow-owned idle bucket over
        # stale vanilla ActivityWatch buckets.
        try:
            latest = self.aw.get_latest_afk_event()
        except Exception as e:
            logger.debug("idle_tracker_health: AW unreachable, skipping: %s", e)
            return
        if latest is None:
            return  # No AFK bucket yet — tracker hasn't started or hasn't reported.

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

        # Disagreement detected: the tracker submits 'afk' while the bucket has
        # fresh input — the literal "idle but bucket has events". Count every
        # detection (reported via telemetry) but throttle the warn+capture.
        with self._idle_tracker_warn_lock:
            self._blind_tracker_detections += 1
            self._blind_tracker_window += 1
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
        # Surface as a (non-exception) captured report so it shows up in error
        # tracking, not just local logs. error_reporter has its own dedup window.
        if self.error_reporter is not None:
            self.error_reporter.capture(
                "Idle tracker reporting AFK while input is active (blind tracker)",
                level="warning",
                tags={"component": "idle-tracker"},
                context={
                    "detections": self._blind_tracker_detections,
                    # Age, not the wall-clock timestamp: a precise "last typed at
                    # HH:MM:SS" anchors a high-resolution activity timeline for the
                    # end user in the ops error-ingest, which the privacy model
                    # (hashed titles, domain-only URLs) is meant to avoid. Age
                    # conveys "input was N seconds ago" without that anchor.
                    "last_input_age_seconds": int((now - last_input).total_seconds()),
                },
                fingerprint="idle-tracker-blind",
            )

        # Don't just warn — try to recover. A tracker that reports 'afk' while
        # input is flowing is blind/stuck (or an orphan is fighting it); a
        # restart re-establishes capture and reaps any orphan. Throttled by the
        # same rewarn interval above, so it won't thrash. If the real cause is a
        # missing permission a restart won't fix it, but the notification above
        # tells the user how — and a stuck tracker recovers without a manual
        # app restart (the recurring "shows idle while I'm working" report).
        if self.aw_manager.is_managing:
            try:
                self.aw_manager.restart_idle_tracker(reason="afk reported while input active")
            except Exception:
                logger.warning("restart_idle_tracker failed", exc_info=True)

    def _reprompt_idle_tracker_permission(self) -> None:
        """bf-idle-tracker has stayed unresponsive across repeated restarts. A
        restart can't fix that — the usual cause is a stale or denied Input
        Monitoring grant on the tracker (a separate TCC subject from the main
        app, e.g. after the tracker's signing identity changed). Surface an
        honest, actionable nudge and open the Input Monitoring pane.

        We deliberately do NOT assert "missing permission" as fact: the main app
        often already has the grant, and tracking keeps working via the OS idle
        clock regardless (idle_manager's freshness fallback). So the message
        leads with "still being tracked" and points at the real remedy
        (enable / re-toggle bf-idle-tracker). Throttled to the same rewarn
        window as the disagreement warning so the two never spam.
        """
        now = datetime.now(timezone.utc)
        with self._idle_tracker_warn_lock:
            recently_warned = (
                self._last_idle_tracker_warn_at is not None
                and (now - self._last_idle_tracker_warn_at) < self._PERM_REWARN_INTERVAL
            )
            if recently_warned:
                return
            self._last_idle_tracker_warn_at = now

        try:
            from .ui import permissions
        except ImportError:
            from ui import permissions

        # Whether the MAIN app holds Input Monitoring tells us how to phrase this.
        # If the app itself lacks it, "grant Input Monitoring" is the honest ask.
        # If the app HAS it, the tracker is a distinct subject whose grant is
        # stale/denied — telling the user to "grant Input Monitoring" they already
        # granted reads as broken; "re-toggle bf-idle-tracker" is the real fix.
        try:
            app_has_grant = permissions.input_monitoring_active()
        except Exception:
            logger.debug("Input Monitoring status check failed", exc_info=True)
            app_has_grant = False

        logger.warning(
            "bf-idle-tracker unresponsive across repeated restarts (app Input "
            "Monitoring grant=%s) — opening Input Monitoring so the user can "
            "enable/refresh the tracker; tracking continues via the OS idle clock",
            app_has_grant,
        )
        try:
            if not app_has_grant:
                # The app itself needs the grant — request it (system prompt).
                permissions.input_monitoring_active(prompt=True)
            permissions.open_input_monitoring_settings()
        except Exception:
            logger.debug("Input Monitoring re-prompt failed", exc_info=True)

        if app_has_grant:
            send_notification(
                "BetterFlow idle tracker needs a refresh",
                "The idle tracker stopped responding. Your work time is still "
                "being recorded via the system idle clock. To fix it, open Input "
                "Monitoring and enable bf-idle-tracker — if it already looks "
                "enabled, toggle it off and back on to refresh the permission.",
            )
        else:
            send_notification(
                "BetterFlow needs Input Monitoring",
                "BetterFlow's idle tracker can't see your activity. Tracking "
                "continues via the system idle clock for now. Opening Settings: "
                "please enable BetterFlow and bf-idle-tracker under Input "
                "Monitoring.",
            )

    # Frontmost-window sampling cadence for the in-process window source. Window
    # focus changes far faster than the 60s sync cycle; ~5s keeps the sample log
    # dense enough for real per-app span resolution without meaningful cost
    # (one GetForegroundWindow + psutil name lookup). Only fires work when
    # in_process_window is enabled and the probe is usable.
    WINDOW_SAMPLE_INTERVAL_SECONDS = 5

    def _sample_window(self) -> None:
        """Dedicated fast sampler for the in-process window source. Gated no-op
        unless in_process_window is on and the frontmost-window probe is usable,
        so it costs nothing on the default path."""
        now = datetime.now(timezone.utc)
        # This runs every 5s straight off the scheduler, independently of the
        # trackers, so stopping the tracker processes does NOT stop it: with
        # in_process_window enabled it would keep sampling the frontmost app name
        # and window title into a local buffer all night. Gate it on the same
        # schedule as everything else.
        if not self.config.working_hours.allows(now):
            return
        try:
            self.sync_engine.record_window_sample_if_active(now)
        except Exception as e:
            logger.debug("window sample tick failed: %s", e)

    def _tick_60s(self) -> None:
        """Unified 60-second tick - one wakeup instead of five.

        Order: tick_clock first (ghost detection), then idle check (may
        pause sync), then hours refresh, then lightweight checks.

        Each sub-task is wrapped in try/except so one failure can't kill
        the APScheduler job — if the job raises, APScheduler would log
        once and stop re-scheduling, freezing the 60s tick entirely
        (idle detection, hours, permissions, reminders all silently dead).
        Several callees already self-guard but tray.tick_clock() does not.

        NOTE the tuple below is built BEFORE the loop body runs, so anything that
        raises while *constructing* it (a missing attribute, say) escapes
        _run_tick_task entirely and kills the tick anyway — the exact failure the
        try/except exists to prevent. Every entry must therefore be an attribute
        that provably exists on THIS class. Hence apply_capture_policy is declared
        in __init__ with a None default and filtered out here rather than being
        reached for on another object.
        """
        tasks = [
            # First: a window that closed while the app was running must stop
            # capture promptly, before any sub-task below reads from the trackers.
            (self.apply_capture_policy, "capture_policy"),
            # Re-arm the one-shot boundary if a misfire discarded it (see
            # _self_heal_capture_boundary). Runs right after the policy so a healed
            # boundary re-aligns before the tick's other trackers read state.
            (self._self_heal_capture_boundary, "boundary_selfheal"),
            (self._reconcile_inproc_afk_flag, "inproc_afk_reconcile"),
            (self.tray.tick_clock, "tick_clock"),
            (self._check_idle_status, "idle_check"),
            (self._heartbeat_floor, "heartbeat_floor"),
            (self._refresh_hours_today, "hours_refresh"),
            (self._check_permissions, "permissions"),
            (self._check_auth_warn, "auth_warn"),
            (self._check_idle_tracker_health, "idle_tracker_health"),
            (self._check_private_auto_end, "private_auto_end"),
        ]
        for fn, label in [t for t in tasks if t[0] is not None]:
            self._run_tick_task(fn, label)
        if self.reminder_manager:
            self._run_tick_task(self.reminder_manager.check, "reminders")

    # A _tick_60s sub-task that fails this many cycles in a row is escalated to
    # the ops ingest (a transient blip is below this; chronic breakage is not).
    _TICK_FAILURE_ESCALATE_THRESHOLD = 3

    def _run_tick_task(self, fn: Callable[[], None], label: str) -> None:
        """Run one _tick_60s sub-task. A single failure is logged but never kills
        the scheduler (one dead sub-task must not freeze idle detection, hours,
        permissions, etc). A sub-task that fails repeatedly is escalated ONCE to
        the ops ingest — the missing signal that let Bug A run silently for a
        release. A later success clears the streak and re-arms escalation."""
        try:
            fn()
        except Exception as e:
            logger.warning("_tick_60s/%s failed: %s", label, e)
            self._note_tick_failure(label, e)
            return
        self._clear_tick_failure(label)

    def _note_tick_failure(self, label: str, exc: Exception) -> None:
        n = self._tick_failure_counts.get(label, 0) + 1
        self._tick_failure_counts[label] = n
        if n < self._TICK_FAILURE_ESCALATE_THRESHOLD or label in self._tick_failure_reported:
            return
        self._tick_failure_reported.add(label)
        reporter = getattr(self, "error_reporter", None)
        if reporter is None:
            return
        try:
            reporter.capture(
                f"_tick_60s sub-task '{label}' failed {n} consecutive times — a "
                f"background tick is silently broken",
                level="error",
                tags={"component": "tick-60s", "task": label},
                context={"consecutive_failures": n, "last_error": str(exc)},
                fingerprint=f"tick-60s-{label}",
            )
        except Exception:
            logger.debug("tick-failure escalation report failed", exc_info=True)

    def _clear_tick_failure(self, label: str) -> None:
        if self._tick_failure_counts.pop(label, None):
            self._tick_failure_reported.discard(label)

    def _handle_aw_bucket_failure(self) -> None:
        """AW answers is_running() (/info) but the bucket fetch (/buckets/) keeps
        failing — a half-hung bf-data-service. is_running() can't see this, so the
        normal unreachable watchdog never fires; debounce, then force_restart to
        reclaim the hung server (force_restart rebuilds the whole stack incl. a
        port-held-but-HTTP-dead server). Without this the agent loops 'Failed to
        get buckets' until the user manually restarts — Liviu's 2 AM 503 storm,
        133 failures, ~75 min of tracking lost."""
        self._aw_buckets_failed_streak += 1
        if self._aw_buckets_failed_streak >= self._AW_UNREACHABLE_ERROR_THRESHOLD:
            if self.aw_manager.is_managing:
                logger.warning(
                    "ActivityWatch responding but bucket fetch failing for %d "
                    "cycles — forcing tracker+server restart (hung bf-data-service)",
                    self._aw_buckets_failed_streak,
                )
                self.aw_manager.force_restart(reason="bucket fetch failing (server hung)")
            self.tray.set_state(TrayState.ERROR, "ActivityWatch not responding")
        else:
            logger.info(
                "ActivityWatch bucket fetch failing (%d/%d) — retrying next cycle "
                "before forcing a restart",
                self._aw_buckets_failed_streak, self._AW_UNREACHABLE_ERROR_THRESHOLD,
            )

    def _reconcile_inproc_afk_flag(self) -> None:
        """Keep aw_manager's in-process-AFK flag in step with the sync engine's
        actual per-cycle decision. The flag gates the idle-tracker watchdog and
        the AFK health telemetry; it was set once at startup, so without this it
        could diverge from what the engine actually does (audit finding A)."""
        # This runs on SyncCoordinator (via _tick_60s), so the engine/manager are
        # direct attributes — `self.coordinator` only exists on BetterFlowApp, so
        # the old `self.coordinator.*` threw AttributeError every 60s and this
        # reconcile never ran (the flag it maintains gates the idle-tracker
        # watchdog + AFK telemetry).
        eng = self.sync_engine
        if eng.afk_source is None:
            return
        self.aw_manager.set_inproc_afk_active(eng.inproc_afk_active)

    def _check_idle_status(self) -> None:
        self.idle_mgr.check_idle_status(
            logged_in=self.logged_in,
            is_on_break=self.is_on_break,
            reschedule=self.reschedule,
            trigger_sync=self.trigger_sync,
        )

    # How long the heartbeat may go dormant before the 60s tick forces one.
    # Bounded ABOVE by the server's ~30-min stale-session cleanup (a paused
    # device must beat well inside that or its session is marked 'crashed') and
    # BELOW by the worst acceptable remote-command delivery latency — commands
    # (pause / deregister / min-version / logs_requested) ride the heartbeat.
    # 300s sits comfortably under the cleanup and caps command delivery to
    # idle/paused devices at ~5 min. If ops ever needs faster command delivery,
    # lowering this one number tightens both concerns (harmlessly).
    _HEARTBEAT_FLOOR_INTERVAL = 300  # seconds

    def _heartbeat_floor(self) -> None:
        """Force a heartbeat from the 60s tick once the sync-cadence heartbeat
        has gone dormant — keeping paused sessions alive AND idle devices
        reachable by remote commands.

        The normal heartbeat rides _do_sync (every 5th sync cycle). Two states
        leave it dormant:

        - PAUSED (break / screen lock / manual / private): _do_sync early-returns
          before its heartbeat, so during a break longer than the server's 30-min
          cleanup last_seen_at goes stale and the session is marked 'crashed' —
          tracking then doesn't resume on return. A paused agent is alive.
        - IDLE: _do_sync keeps running but on the 300s reduced interval, so its
          every-5th-cycle heartbeat lands only ~every 25 min. Every server->agent
          command (pause / deregister / minimum_agent_version / logs_requested)
          rides that heartbeat, so on an idle device those take ~25 min to arrive.

        Both reduce to one condition: the last heartbeat (from ANY path) has aged
        past the floor. The engine stamps every heartbeat attempt, so
        seconds_since_last_heartbeat() is the single source of truth — no separate
        throttle here, and no need to special-case paused vs idle. This floor's
        own beat resets that stamp, so it self-throttles to one per interval.
        Active devices heartbeat ~every 150s via the cadence path, keeping the
        stamp fresh, so the floor never fires for them in steady state — it fires
        once at cold start (before the first cadence beat, active or idle alike:
        a single extra beat). Heartbeats only refresh last_seen_at, never
        active/tracked time.

        Skipped only while logged out or offline (no server to reach).
        """
        if not self.logged_in:
            return
        if self.paused_by_network:
            return  # offline — the heartbeat would just fail; resume on reconnect
        # Single source of truth: the engine stamps every heartbeat attempt
        # (cadence path and this floor). A None stamp (no beat yet this process)
        # is treated as stale so a device idle-since-startup still registers.
        since = self.sync_engine.seconds_since_last_heartbeat()
        if since is not None and since < self._HEARTBEAT_FLOOR_INTERVAL:
            return

        auth_err = self.sync_engine.send_heartbeat_now()
        if auth_err is not None:
            self._handle_auth_error(auth_err, source="heartbeat_floor")

    # Must exceed the worst-case wall-clock of one in-cycle network chain so a
    # slow/hung server can't masquerade as a wedged sync. The batch-upload path
    # runs on BaseApiClient.DEFAULT_RETRY_CONFIG (max_retries=2) at a 30s timeout:
    # 3 attempts * 30s + backoff ≈ 94s, plus the heartbeat (~11s) and bookkeeping.
    # 150s leaves margin above that ~105s realistic worst case; only a genuine
    # multi-minute hang trips it. (The old 120s was < the then-129s chain, which
    # false-fired "Sync hung" during the 2026-06-30 outage.) Guarded by
    # tests/test_sync_watchdog_budget.py.
    _DO_SYNC_DEADLINE = 150  # seconds
    # A _do_sync holding _sync_lock longer than this is treated as wedged
    # (deadlock, or a call hung past the watchdog). A new cycle then abandons the
    # stuck holder and re-arms a fresh lock so syncing resumes instead of skipping
    # forever. Must exceed _DO_SYNC_DEADLINE so the watchdog's session-reset gets
    # its chance first.
    _SYNC_WEDGE_CEILING = 420  # seconds

    def _acquire_sync_slot(self) -> Optional[threading.Lock]:
        """Take the sync slot, returning the lock to release later, or None to
        skip this cycle.

        Normally a non-blocking acquire of _sync_lock. But a wedged cycle
        (lock-ordering deadlock, or a call hung past the watchdog) holds the lock
        forever — every later cycle would skip and sync would freeze while the
        heartbeat keeps last_seen fresh (the device looks Active but uploads
        nothing: Cristian Dragota / sync:67a77a43-787, 2026-06-25). The zombie
        thread can't be killed, but it must stop blocking every future sync:
        after _SYNC_WEDGE_CEILING we abandon it and re-arm a fresh lock.

        All bookkeeping runs under _sync_takeover_lock (held briefly, never across
        IO) so the acquire + re-arm decision is atomic. _sync_holder identifies
        the current holder so a zombie's finally can't clear a successor's stamp.
        """
        with self._sync_takeover_lock:
            lock = self._sync_lock
            if lock.acquire(blocking=False):
                self._sync_started_at = time.monotonic()
                self._sync_holder = lock
                return lock

            started = self._sync_started_at
            if started is None or time.monotonic() - started <= self._SYNC_WEDGE_CEILING:
                return None  # a healthy in-flight cycle — just skip this tick

            logger.error(
                "Sync wedged >%ds — abandoning the stuck cycle and re-arming the "
                "lock so syncing can resume",
                self._SYNC_WEDGE_CEILING,
            )
            if self.error_reporter is not None:
                self.error_reporter.capture(
                    f"Sync wedged — held the sync lock >{self._SYNC_WEDGE_CEILING}s; "
                    "re-armed to resume syncing",
                    level="error",
                    tags={"component": "sync-wedge"},
                    fingerprint="sync-wedged",
                )
            fresh = threading.Lock()
            fresh.acquire()
            self._sync_lock = fresh
            self._sync_started_at = time.monotonic()
            self._sync_holder = fresh
            return fresh

    def _select_tray_state(
        self,
        stats,
        near_capacity: bool,
        capacity_pct: int,
        is_idle: bool,
        queue_size: int,
    ) -> "tuple[TrayState, Optional[str]]":
        """Pure decision for the post-sync tray headline — no side effects, so the
        precedence is unit-testable without driving a whole sync. The caller applies
        the result via tray.set_state (None detail is equivalent to omitting it).

        Precedence: suppression > near-capacity > queued > idle > syncing.

        Suppression wins on purpose: the offline queue keeps DRAINING while
        suppressed, so a user who crosses into private hours with leftover backlog
        must still see the headline fact (nothing is being recorded), with the drain
        as a tooltip detail. The drain count is queue_size — the ACTUAL queue depth —
        NOT stats.events_queued, which only counts events (re)queued THIS cycle."""
        if stats.capture_suppressed:
            detail = "Private hours — not recording"
            # Gate on queue_size (the ACTUAL depth we display), NOT stats.events_queued
            # (this cycle's requeues). They disagree at both edges: a suppressed user
            # with a standing backlog but no requeue THIS cycle would otherwise see no
            # drain detail, and a cycle that fully drained an existing backlog would
            # render the nonsensical "(draining 0 queued)".
            if queue_size > 0:
                detail = f"{detail} (draining {queue_size} queued)"
            if near_capacity:
                detail = f"{detail} — queue {capacity_pct}% full"
            return TrayState.PRIVATE_HOURS, detail
        if near_capacity:
            return TrayState.QUEUE_WARNING, f"Queue {capacity_pct}% full"
        if stats.events_queued > 0:
            return TrayState.QUEUED, None
        if is_idle:
            return TrayState.PAUSED, "Idle"
        return TrayState.SYNCING, None

    def _do_sync(self) -> None:
        """Perform a sync cycle."""
        my_lock = self._acquire_sync_slot()
        if my_lock is None:
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
                # Escalate a non-converging restart loop: repeated forced
                # restarts mean the restart isn't fixing it (orphan tracker /
                # missing Input Monitoring permission). Capture so it surfaces
                # instead of looping silently; error_reporter dedup throttles.
                try:
                    restarts = self.aw_manager.stale_restart_count()
                    if (
                        restarts >= self._RESTART_LOOP_ALERT_THRESHOLD
                        and not self._restart_loop_escalated
                        and self.error_reporter is not None
                    ):
                        self.error_reporter.capture(
                            f"Idle-tracker restart loop not converging ({restarts} restarts this session)",
                            level="warning",
                            tags={"component": "idle-tracker"},
                            context={"stale_restarts": restarts},
                            fingerprint="idle-tracker-restart-loop",
                        )
                        # Once per session — the restart count is monotonic, so
                        # without this latch the capture would re-fire every cycle
                        # (only the reporter's dedup window kept it from flooding).
                        self._restart_loop_escalated = True
                except Exception:
                    # WARNING, not debug: if the reporter itself is broken (bad
                    # DSN, etc.) the escalation we built to surface this loop must
                    # not fail silently every cycle.
                    logger.warning("restart-loop escalation check failed", exc_info=True)

            # Outside working hours the trackers are stopped ON PURPOSE, so
            # aw.is_running() is False and the branch below would (a) escalate a
            # deliberate silence to "ActivityWatch not responding" in the tray every
            # single night, and (b) `return` before sync_engine.sync() — which is
            # where the offline queue gets drained, the heartbeat is sent, AND
            # fetch_server_config() retries. That last one is a trap: a device whose
            # first config fetch failed has known=False, so capture is suppressed, so
            # the tracker is down, so sync() is never reached, so the config is never
            # re-fetched — suppressed forever, zero tracking, until someone restarts
            # the app AND the network happens to be up. Fall through instead: sync()
            # has its own suppressed path that skips the AW reads.
            capture_suppressed = not self.config.working_hours.allows(
                datetime.now(timezone.utc)
            )

            if not capture_suppressed and not self.aw.is_running():
                # is_running() already reset+retried the HTTP session, so this
                # is a real stall, not a stale-socket blip. Debounce before
                # escalating: one missed cycle stays silent (it usually
                # self-heals next cycle); only after consecutive failures do we
                # force-restart and surface an Error.
                self._aw_unreachable_streak += 1
                if self._aw_unreachable_streak >= self._AW_UNREACHABLE_ERROR_THRESHOLD:
                    if self.aw_manager.is_managing:
                        logger.warning(
                            "ActivityWatch unreachable for %d cycles — forcing tracker+server restart",
                            self._aw_unreachable_streak,
                        )
                        # force_restart also reclaims a hung-but-listening server
                        # (port held, HTTP dead); a plain stop()+start() only
                        # cycles the watchers and leaves the dead server in place.
                        self.aw_manager.force_restart(reason="server unreachable")
                    self.tray.set_state(TrayState.ERROR, "ActivityWatch not responding")
                else:
                    logger.info(
                        "ActivityWatch unreachable (%d/%d) — retrying next cycle before escalating",
                        self._aw_unreachable_streak, self._AW_UNREACHABLE_ERROR_THRESHOLD,
                    )
                return

            # Reachable (or deliberately down) — clear the streak. A suppressed
            # night must not leave a stale streak that escalates the moment the
            # window reopens.
            self._aw_unreachable_streak = 0

            stats = self.sync_engine.sync()

            if stats.aw_bucket_fetch_failed:
                # AW answers /info but 503s on /buckets/ — is_running() above
                # passed, so only this path can recover the hung server.
                self._handle_aw_bucket_failure()
                return
            self._aw_buckets_failed_streak = 0

            if stats.success or stats.events_sent > 0:
                # A successful (or partial) sync clears the failure streak.
                self._consecutive_sync_failures = 0
                # Stamp the last good round-trip for the staleness telemetry.
                self._last_successful_sync = time.monotonic()
                # A successful authenticated round-trip means the token is fine,
                # so any earlier 401/403 was transient — clear the auth streak.
                self._consecutive_auth_failures = 0
                # Partial success: some buckets may fail but data still syncs
                if stats.errors:
                    for err in stats.errors:
                        logger.warning(f"Partial sync: {err}")
                # Near-capacity is evaluated and LOGGED independently of which
                # state wins the tray below. Suppression correctly owns the tray
                # headline during private hours, but a backlog stuck near the cap
                # overnight (e.g. the network is down while suppressed) still has to
                # surface in fleet monitoring — folding the warning into the tray
                # if/elif hid it whenever suppression won, losing that visibility.
                near_capacity = self.queue.is_near_capacity()
                capacity_pct = (
                    int(self.queue.capacity_percent() * 100) if near_capacity else 0
                )
                if near_capacity:
                    logger.warning(f"Offline queue at {capacity_pct}% capacity")

                state, detail = self._select_tray_state(
                    stats, near_capacity, capacity_pct, is_idle, self.queue.size()
                )
                # set_state(state, None) is identical to set_state(state), so passing
                # the (possibly None) detail through preserves the prior behaviour.
                self.tray.set_state(state, detail)
                if stats.events_sent > 0:
                    logger.info(f"Sync complete: {stats.events_sent} events synced")
            else:
                for err in stats.errors:
                    logger.warning(f"Sync failed: {err}")
                self._set_sync_failure_state(
                    stats.errors[0] if stats.errors else "Sync failed"
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
            self._set_sync_failure_state("Sync error")
            self._note_sync_failure("Sync error", exc=e)
        finally:
            # Clear the wedge stamp only if we're still the current holder — a
            # taken-over zombie reaching here must not wipe its successor's stamp.
            with self._sync_takeover_lock:
                if self._sync_holder is my_lock:
                    self._sync_started_at = None
                    self._sync_holder = None
            watchdog_cancelled.set()
            watchdog.cancel()
            my_lock.release()

        # Heartbeat runs AFTER _sync_lock is released — no need to hold
        # the lock during a blocking HTTP call that can take 30s on timeout.
        # send_heartbeat_if_due returns the auth error rather than raising,
        # because raising in the post-lock tail would surface as an uncaught
        # exception in the APScheduler thread.
        auth_err = self.sync_engine.send_heartbeat_if_due(stats)
        if auth_err is not None:
            self._handle_auth_error(auth_err, source="heartbeat")

    def _set_sync_failure_state(self, error_message: str) -> None:
        """Pick the right tray state for a failed sync.

        No internet should read "Offline", not "Error" — including the common
        case where Wi-Fi is "connected" but has no route (NO_INTERNET), which
        the OS network monitor doesn't always report, so paused_by_network never
        gets set. Probe reachability: unreachable -> Offline (we're still
        tracking and queuing locally); reachable but failed -> a real Error.
        """
        try:
            reachable = self.bf.is_reachable()
        except Exception:
            reachable = False
        if not reachable:
            self.tray.set_state(TrayState.QUEUED, "Offline")
        else:
            self.tray.set_state(TrayState.ERROR, error_message)

    def _build_health_telemetry(self) -> dict:
        """Assemble agent-health telemetry for the heartbeat.

        Combines the AwManager's tracker view (idle-tracker restart count, AFK
        and window event ages) with our own consecutive-sync-failure counter.
        Best-effort: any failure here is swallowed by the caller so it can never
        block the heartbeat. Reading the int counter without a lock is fine — a
        torn read of a single int can't happen in CPython, and a slightly stale
        value is harmless for a telemetry signal.
        """
        # Read-and-reset the per-heartbeat blind-tracker window so the reported
        # value reflects "detected since the last heartbeat" (clears on recovery).
        with self._idle_tracker_warn_lock:
            blind_window = self._blind_tracker_window
            self._blind_tracker_window = 0
        telemetry: dict = {
            "consecutive_sync_failures": self._consecutive_sync_failures,
            "idle_while_active_detections": blind_window,
        }
        # Seconds since the last successful sync round-trip. Lets the server flag
        # "alive but sync stale" (heartbeat fresh, uploads frozen) directly rather
        # than inferring it from upload gaps. Omitted until the first good sync.
        last_ok = self._last_successful_sync
        if last_ok is not None:
            telemetry["sync_stale_seconds"] = int(time.monotonic() - last_ok)
        try:
            telemetry.update(self.aw_manager.health_snapshot())
        except Exception as e:  # noqa: BLE001
            logger.debug("aw_manager.health_snapshot failed: %s", e)
        return telemetry

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
        """Handle a 401/403 from sync or heartbeat — tolerating transient ones.

        Logging out on the FIRST auth error is what froze users at "idle" after
        a backend deploy or a momentary token blip: one 401 → wipe session →
        tracking stops. We instead require several CONSECUTIVE failures before
        treating the session as lost. Until the threshold, we keep the session
        and keep tracking/queuing — the next sync retries, and a success resets
        the streak (see _do_sync). Only a sustained failure (genuine
        revoke/logout) crosses the threshold and prompts re-login.
        """
        self._consecutive_auth_failures += 1
        if self._consecutive_auth_failures < self._AUTH_FAILURE_LOGOUT_THRESHOLD:
            logger.warning(
                "Auth error during %s (%d/%d consecutive): %s — tolerating as "
                "likely transient; keeping session, will retry",
                source,
                self._consecutive_auth_failures,
                self._AUTH_FAILURE_LOGOUT_THRESHOLD,
                e,
            )
            return

        logger.warning(
            "Auth error during %s — %d consecutive failures, session lost: %s",
            source,
            self._consecutive_auth_failures,
            e,
        )
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
            stop_external_when_inproc=self.config.sync.stop_external_afk_tracker,
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

        # Both of these used to be STARTED here — at process init, before login and
        # before the working-hours schedule was known. The browser tracker polls the
        # frontmost browser's active-tab URL (AppleScript on macOS, uiautomation
        # omnibox on Windows); starting it before we know whether this person may be
        # recorded at all is the same fail-open mistake as the old enforced=False
        # default. They are now started by _start_watchers() under the capture
        # policy, and stopped by _stop_watchers() when the window closes.
        self.display_tracker = None
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
            on_show_hours=self._on_show_hours,
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
        self.coordinator.on_private_auto_end = self._auto_end_private
        self.coordinator._input_watcher = self.input_watcher
        # The 60s tick is what notices the window closing at 22:00 while the app is
        # running. BetterFlowApp owns the trackers, so the coordinator gets a handle
        # rather than reaching across to another object (see SyncCoordinator.__init__).
        self.coordinator.apply_capture_policy = self._apply_capture_policy
        # The idle manager uses the same in-process watcher as an authoritative
        # override so a blind/stuck bf-idle-tracker can't paint live work as
        # Idle (it never consulted the input watcher before — only the health
        # check did, which left a race where idle was painted before the
        # blind-tracker restart landed).
        self.coordinator.idle_mgr.input_watcher = self.input_watcher
        self.coordinator.idle_mgr._on_idle_pause = self._on_idle_pause

        # In-process AFK source: the agent generates its own active/idle stream
        # from the OS idle clock (+ the in-process input watcher on macOS) and
        # uploads it as the sole AFK source, so a frozen/blind bf-idle-tracker
        # can't lose billed time. Inert unless config enables it AND the OS idle
        # clock is readable (macOS/Windows; off on Linux). When active, tell the
        # AwManager to stop restarting/alerting on the now-ignored tracker.
        try:
            from .sync.afk_source import AfkSource
        except ImportError:
            from sync.afk_source import AfkSource
        # Register the engagement detectors (created by the sync engine) as
        # supplementary AFK activity sources so an engaged no-input span keeps
        # the uploaded AFK stream not-afk (macOS/Windows):
        #   * call detector — a meeting/huddle with hands off the keyboard used
        #     to upload 'afk' after the timeout and paint the whole call Idle
        #     on the dashboard; only the LOCAL pause was suppressed before
        #     (Ecaterina's 49-minute huddle, 2026-07-15).
        #   * foreground-CPU detector — active build/Claude/render in focus.
        # Inert on Linux, where the OS idle clock — and thus this whole
        # in-process AFK source — is unreadable; there the uploaded call /
        # dev-session spans carry the credit.
        activity_sources = [
            src for src in (
                self.coordinator.sync_engine._call_detector,
                self.coordinator.sync_engine._foreground_detector,
            ) if src is not None
        ]
        afk_source = AfkSource(
            afk_timeout_seconds=self.config.aw.afk_timeout_minutes * 60,
            hostname=self.coordinator.sync_engine._hostname,
            input_watcher=self.input_watcher,
            activity_sources=activity_sources or None,
        )
        self.coordinator.sync_engine.afk_source = afk_source
        self.coordinator.aw_manager.set_inproc_afk_active(
            self.config.sync.in_process_afk and afk_source.available()
        )

        # In-process WINDOW source: the per-app analogue of afk_source. The agent
        # generates its own per-app active-window stream from the OS
        # frontmost-window probe (+ psutil process name) and uploads it as the
        # sole window source, so a bf-window-tracker that launches but captures
        # zero events (the Windows blind-capture failure) can't lose per-app
        # coverage. Ships dormant: inert unless config enables it AND the probe
        # is usable (macOS/Windows; off on Linux without an X11 active-window pid).
        try:
            from .sync.window_source import WindowSource
        except ImportError:
            from sync.window_source import WindowSource
        window_source = WindowSource(
            hostname=self.coordinator.sync_engine._hostname,
        )
        self.coordinator.sync_engine.window_source = window_source
        logger.info(
            "In-process window source: %s",
            "active" if (self.config.sync.in_process_window and window_source.available())
            else "inactive",
        )

        # In-process INPUT source: the keystroke/click/scroll-count analogue of
        # window_source. Counts input inside the agent process (Windows ctypes
        # low-level hooks / macOS CGEventTap) and uploads it as the sole input
        # source, so a device where the external aw-watcher-input hook is blocked
        # (UIPI / AV) and reports ZERO input (Fraud Risk 75) still gets real
        # counts. Ships dormant: inert unless config enables it AND a backend is
        # usable (macOS/Windows; off on Linux). The backend listener thread is
        # started with the other in-process watchers in _start_watchers().
        try:
            from .sync.input_source import InputSource
        except ImportError:
            from sync.input_source import InputSource
        input_source = InputSource(
            hostname=self.coordinator.sync_engine._hostname,
        )
        self.coordinator.sync_engine.input_source = input_source
        self.input_source = input_source
        logger.info(
            "In-process input source: %s",
            "active" if (self.config.sync.in_process_input and input_source.available())
            else "inactive",
        )

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
        self._reconnect_lock = threading.Lock()
        self._reconnect_thread: Optional[threading.Thread] = None
        self._startup_thread: Optional[threading.Thread] = None
        self._system_events_started = False
        self._system_events_lock = threading.Lock()
        # Working-hours capture policy. None = not yet evaluated; the first
        # _apply_capture_policy() call decides. Guarded by _capture_lock because
        # the 60s tick, the post-config-fetch callback and the wake-from-sleep
        # handler all drive it from different threads.
        self._capture_lock = threading.RLock()
        self._capture_allowed: Optional[bool] = None

        # Sub-handlers
        self.update_handler = UpdateHandler(self.tray, self.config, self.coordinator, _VERSION)
        # Let a server-advertised minimum version (heartbeat) push an update:
        # the sync engine sees the floor, the handler stages + applies on idle.
        self.sync_engine.on_update_required = self.update_handler.trigger_remote_update
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
        # Start the in-process input-count backend only when the (dormant, opt-in)
        # feature is enabled — otherwise ~100% of the fleet would install an OS
        # input hook for a source nothing drains. Gated + no-op when off.
        if self.input_source is not None and self.config.sync.in_process_input:
            try:
                self.input_source.start()
            except Exception as e:
                logger.warning("In-process input source start failed: %s", e)

        # The browser-tab URL reader and the display tracker are started HERE, under
        # the capture policy — not in __init__, where they used to run from process
        # start, before login and before any schedule was known. The browser tracker
        # is the single most sensitive recorder we ship (it reads the frontmost
        # browser's active-tab URL via AppleScript on macOS / the uiautomation
        # omnibox on Windows), and it was left running all night by every earlier
        # version of this fix.
        if self.browser_tracker is None and self.config.privacy.track_browser_urls:
            try:
                self.browser_tracker = start_browser_tracker()
            except Exception as e:
                logger.warning("Browser tracker start failed: %s", e)
        if self.display_tracker is None and self.config.privacy.track_display_info:
            try:
                self.display_tracker = start_display_tracker()
            except Exception as e:
                logger.warning("Display tracker start failed: %s", e)
        # Hand the NEW objects to the engine through its setter. Assigning
        # engine.browser_tracker directly writes an attribute nothing reads.
        self.sync_engine.set_enrichment_trackers(
            browser_tracker=self.browser_tracker,
            display_tracker=self.display_tracker,
        )

    def _stop_watchers(self) -> None:
        """Stop EVERY in-process recorder. Mirror of _start_watchers().

        AWManager.stop() only reaches the processes IT spawned. Everything below
        runs inside our own process and would otherwise keep recording after the
        trackers were stopped:

        - window_watcher  : foreground app + window title (macOS)
        - input_watcher   : CGEventTap over every keystroke/click/scroll (macOS)
        - input_source    : in-process input-count backend (macOS tap / Windows hook)
        - browser_tracker : active-tab URL of the frontmost browser (both platforms)
        - display_tracker : attached-display metadata

        browser_tracker and display_tracker were missed by the first cut of this
        fix, so a "suppressed" machine was still reading the employee's browser tabs
        at midnight. On Windows window_watcher/input_watcher are None and
        bf-window-tracker / bf-idle-tracker are covered by AWManager — but
        browser_tracker is NOT, which is why it has to be handled here.

        (The 5s window sampler has no stop(); it is gated instead, in
        SyncCoordinator._sample_window.)
        """
        for watcher, label in (
            (self.window_watcher, "window_watcher"),
            (self.input_watcher, "input_watcher"),
            (self.input_source, "input_source"),
            (self.browser_tracker, "browser_tracker"),
            (self.display_tracker, "display_tracker"),
        ):
            if watcher is None:
                continue
            try:
                watcher.stop()
            except Exception as e:
                logger.warning("Failed to stop %s: %s", label, e)

        # These two are re-created by _start_watchers() when the window reopens.
        # Detach them from the engine through its setter too, so nothing that
        # outlives the stop can poll a stale handle.
        self.browser_tracker = None
        self.display_tracker = None
        self.sync_engine.set_enrichment_trackers(browser_tracker=None, display_tracker=None)

    def _capture_currently_allowed(self) -> bool:
        """Whether capture is permitted right now, per the working-hours schedule."""
        return self.config.working_hours.allows(datetime.now(timezone.utc))

    def _apply_capture_policy(self, reason: str = "tick") -> None:
        """Start or stop ALL local capture according to the working-hours schedule.

        This is the enforcement point. Outside the user's window — and while their
        schedule is still unknown — nothing on this machine records them: the
        tracker processes are down and every in-process recorder is stopped. Not
        "recorded but not billed"; not recorded.

        CONVERGES on the desired state; it does not latch on a transition. An
        earlier cut returned early when `allowed` matched the last decision, which
        meant anything that resurrected a recorder mid-state stayed up forever —
        _on_idle_pause restarting the input watcher at 22:30 was a real instance,
        and a tracker start that failed at window-open was never retried. Re-running
        the desired end state every 60s costs a few no-op calls and closes both.
        """
        allowed = self._capture_currently_allowed()

        # _capture_lock (not the AWManager lock) guards the transition, so the 60s
        # tick, the post-config-fetch callback and the wake handler can't interleave
        # into a half-applied start/stop. AWManager takes its own lock underneath and
        # never calls back into here, so the ordering is safe.
        with self._capture_lock:
            changed = self._capture_allowed is not allowed
            self._capture_allowed = allowed

            if allowed:
                if changed:
                    # Jump the AW/in-process checkpoints over the suppressed gap
                    # BEFORE anything reads from them again — the same thing
                    # pause/resume and private-time already do for their gaps.
                    # Without it the first cycle back sees a checkpoint from 21:59
                    # and a `now` of 07:31 and synthesises ONE event spanning the
                    # whole 9.5h suppressed night (an afk-inproc span; and, with
                    # in_process_input on, a 9.5h input event stamped 21:59 carrying
                    # this morning's keystrokes). We would have suppressed the night
                    # and then manufactured a record of it anyway.
                    try:
                        self.sync_engine._advance_checkpoints_to_now("capture_resume")
                    except Exception as e:
                        logger.warning("Checkpoint advance on capture resume failed: %s", e)
                self.aw_manager.set_capture_suppressed(False, reason)
                self._start_watchers()
                if changed:
                    logger.info("Capture ENABLED (%s)", reason)
            else:
                self._stop_watchers()
                # Stops the tracker processes and refuses every restart path until
                # capture is allowed again.
                self.aw_manager.set_capture_suppressed(True, reason)
                if changed:
                    wh = self.config.working_hours
                    if not wh.known:
                        logger.info(
                            "Capture DISABLED (%s): working-hours schedule not known "
                            "yet — refusing to record until the server sends it",
                            reason,
                        )
                    else:
                        logger.info(
                            "Capture DISABLED (%s): outside working hours %s-%s (days %s)",
                            reason, wh.work_start, wh.work_end, wh.working_days,
                        )

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
        # Pull data immediately instead of waiting for the scheduler's first
        # 60s tick — otherwise the tray sits at "Starting… / 0h 0m" with greyed
        # menus for up to a minute after a successful login. This first sync
        # fetches today's hours and flips the tray to its live state at once.
        self.coordinator.trigger_sync("post_login_sync")
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
            # Do NOT start the trackers unconditionally here. This runs BEFORE
            # login and therefore before fetch_server_config(), so at this point we
            # do not yet know whether this user may be recorded at all. Starting
            # first and asking later is what let a restricted user's machine be
            # recorded at 23:55 on a fresh launch. _apply_capture_policy() starts
            # the trackers only if a KNOWN schedule allows it — which, on a cold
            # start, means a cached schedule from disk; otherwise capture stays off
            # until the config fetch lands and _on_config_updated re-evaluates.
            self._apply_capture_policy("startup")
            if self._shutdown_event.is_set():
                return

            self._ensure_system_event_listener()

            self._apply_startup_login_state(state)

            logger.info("Background startup complete")
        finally:
            self._login_lock.release()

    # How often the background reconnect loop retries auto-login after a
    # transient (server-unreachable) startup failure. The loop runs until the
    # session comes back, the user logs in manually, or the app shuts down.
    _RECONNECT_RETRY_INTERVAL_S = 30.0

    def _apply_startup_login_state(self, state) -> None:
        """Dispatch on the outcome of the startup auto-login.

        Three outcomes, only two of which should ever prompt the user:

        - logged in            → finish startup normally.
        - transient failure    → the stored session is still valid but the
          server was unreachable (e.g. the 2026-07-02 Railway outage). Do NOT
          prompt re-auth — a re-auth flow can't complete while the server is
          down, and the credentials are fine. Show an offline state and retry
          auto-login in the background until connectivity returns.
        - genuine logged-out   → no/invalid credentials: show WAITING_AUTH and
          notify the user to sign back in.
        """
        if state.logged_in:
            self._finish_logged_in_startup(state, send_greeting=True)
            return

        self.coordinator.logged_in = False
        if getattr(state, "transient", False):
            self.tray.set_state(TrayState.QUEUED, "Offline — reconnecting...")
            self._start_reconnect_retry()
        else:
            self.tray.set_state(TrayState.WAITING_AUTH, "Waiting for browser login...")
            self.coordinator._maybe_warn_login_required(source="startup")
        self._ensure_update_checks_started()

    def _start_reconnect_retry(self) -> None:
        """Spawn the background auto-login retry loop (idempotent)."""
        with self._reconnect_lock:
            if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
                return
            self._reconnect_thread = threading.Thread(
                target=self._reconnect_loop, daemon=True, name="auth-reconnect"
            )
            self._reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        """Retry auto-login until the transient outage clears.

        On success, finish startup as if the session had been restored at
        launch. If the credentials turn out to be genuinely invalid (e.g. the
        token was revoked while we were offline), fall back to the real
        re-auth prompt. Interruptible via the shutdown event.
        """
        while not self._shutdown_event.is_set():
            # Interruptible sleep: returns True the moment shutdown is set.
            if self._shutdown_event.wait(self._RECONNECT_RETRY_INTERVAL_S):
                return
            if self.coordinator.logged_in:
                return  # a manual login (or another path) beat us to it
            if not self._login_lock.acquire(timeout=5):
                continue
            try:
                if self._shutdown_event.is_set() or self.coordinator.logged_in:
                    return
                state = self.login_manager.try_auto_login()
                if state.logged_in:
                    logger.info("Auto-login recovered after transient startup failure")
                    # A resumed session, not a cold launch: use the same
                    # "Welcome back" convention as the manual re-login path
                    # (do_browser_login), not the time-of-day launch greeting.
                    self._finish_logged_in_startup(state, send_greeting=False)
                    first_name = (state.user_name or "").split()[0] if state.user_name else ""
                    greeting = f"Welcome back, {first_name}!" if first_name else "Welcome back!"
                    send_notification(greeting, _day_greeting())
                    return
                if not getattr(state, "transient", False):
                    # Credentials are genuinely gone now — prompt re-auth.
                    self.coordinator.logged_in = False
                    self.tray.set_state(
                        TrayState.WAITING_AUTH, "Waiting for browser login..."
                    )
                    self.coordinator._maybe_warn_login_required(source="reconnect")
                    return
                # Still transient — keep the offline state and try again.
            except Exception as e:
                # This thread is the ONLY recovery path after a transient startup
                # outage; an unexpected exception (e.g. a keyring read error)
                # must not kill it silently and strand the user on
                # "Offline — reconnecting..." forever. Log, surface to ops, and
                # keep retrying.
                logger.warning("Reconnect attempt failed unexpectedly: %s", e, exc_info=True)
                if self.error_reporter is not None:
                    try:
                        self.error_reporter.capture(
                            f"Reconnect attempt raised unexpectedly: {e}",
                            level="warning",
                            tags={"component": "reconnect"},
                            fingerprint="reconnect-unexpected-error",
                        )
                    except Exception:
                        logger.debug("reconnect-error report failed", exc_info=True)
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

        # First-run setup wizard.
        #
        # Gate on stored CREDENTIALS, not on the setup_complete flag alone. The
        # flag has been observed to get knocked false across self-update
        # relaunches; keying the wizard on it alone forced already-onboarded
        # users to re-sign-in after every auto-update. So: only show the wizard
        # when the user genuinely isn't set up (no credentials in the keychain).
        # If credentials exist but the flag is false, it was spuriously reset —
        # repair it and continue straight to auto-login below.
        wizard_login_state = None
        has_credentials = False
        try:
            has_credentials = self.keychain.load() is not None
        except Exception as e:
            logger.warning("Could not read stored credentials at startup: %s", e)

        if not self.config.setup_complete and has_credentials:
            logger.info(
                "setup_complete was false but credentials exist — repairing flag "
                "and skipping the setup wizard (likely reset across an update)"
            )
            self.config.setup_complete = True
            self.config.save()

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

        # Windows 11: lift our tray icon out of the overflow flyout onto the
        # taskbar, with no user action. Best-effort; no-op off Windows, in dev,
        # and on Windows 10. Runs async because Explorer only creates our
        # NotifyIconSettings entry once the icon has been shown, which races
        # startup — the worker retries until the entry appears.
        self._promote_windows_tray_async()

        logger.info("BetterFlow tray starting")
        try:
            self.tray.run_blocking()
        finally:
            self._shutdown()

    def _promote_windows_tray_async(self) -> None:
        """Best-effort: promote our Windows 11 tray icon onto the taskbar.

        Spawns a short-lived daemon that retries until Explorer has registered
        our NotifyIconSettings entry, then stops. No-op off Windows / in dev."""
        if sys.platform != "win32" or not getattr(sys, "frozen", False):
            return
        try:
            try:
                from .windows_tray import promote_tray_icon
            except ImportError:
                from windows_tray import promote_tray_icon
        except ImportError as e:
            logger.debug("windows_tray unavailable, skipping promotion: %s", e)
            return

        def _worker() -> None:
            # Bounded retry: the entry appears only after the icon is shown.
            for _ in range(10):
                try:
                    if promote_tray_icon():
                        return
                except Exception as e:
                    # One unexpected error shouldn't abandon the remaining
                    # retries — the entry may still appear once Explorer settles.
                    logger.debug("Tray promotion attempt failed (non-fatal): %s", e)
                time.sleep(1.0)

        threading.Thread(target=_worker, daemon=True, name="win-tray-promote").start()

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

    def _auto_end_private(self, elapsed_seconds: float) -> None:
        """End Private Time the same way the user toggle does, with a message
        making clear WE turned it off (so they re-enable if it was intended)."""
        self._set_user_paused(False)
        self.sync_engine.set_private_mode(False)
        self.sync_engine.resume()
        self.tray.set_paused(False)
        self.reminder_manager.on_private_ended()
        self.reminder_manager.on_tracking_started()
        send_notification(
            "Private Time auto-ended",
            f"Private mode had been on for {elapsed_seconds / 3600:.1f}h, so tracking "
            "was turned back on. Re-enable Private Time if you still need it.",
        )

    def _on_idle_pause(self, paused: bool) -> None:
        """Handle idle pause/resume — also pause/resume input watcher and slow window polling."""
        if self.input_watcher:
            if paused:
                # Keep the event tap alive while AFK. Restarting the macOS input
                # watcher after long idle periods has proven unreliable on some
                # installs and can leave the sync engine with no input telemetry.
                logger.info("Input watcher left running (user idle)")
            elif self._capture_currently_allowed():
                if not self.input_watcher.is_running:
                    self.input_watcher.start()
                    logger.info("Input watcher resumed (user active)")
            else:
                # Do NOT resurrect the event tap outside working hours. This fires
                # on every idle->active transition and also from tray Resume / end
                # of Private Time — so a user touching the keyboard at 22:30 would
                # have re-armed a CGEventTap on every keystroke and click, and it
                # would have stayed armed until the app quit.
                logger.info("Input watcher NOT resumed: capture suppressed")
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
        # Force the start-of-day backlog reconcile: a manual "Sync Now" should
        # push every locally-captured event the server is missing, not just
        # forward-sync from the checkpoint (which is all the periodic sync does).
        self.coordinator.trigger_sync(force_reconcile=True)

    def _on_show_hours(self) -> Optional[str]:
        """Mint a one-time authenticated dashboard URL for the tray.

        Returns the URL the browser should open, or None so the tray falls back
        to the plain /agent/my URL. Network/auth errors propagate to the tray's
        worker, which logs them and uses the fallback.
        """
        url = self.bf.get_web_login_url()
        if url:
            logger.info("Show My Hours: minted authenticated dashboard URL")
        return url

    def _on_system_sleep(self) -> None:
        if self._shutdown_event.is_set():
            return
        self.sys_events.on_system_sleep()

    def _on_system_wake(self) -> None:
        if self._shutdown_event.is_set():
            return
        # Re-evaluate the window BEFORE anything else touches the trackers. A laptop
        # that slept at 21:00 (inside the window) and wakes at 23:00 (outside it)
        # would otherwise come back with every recorder running and keep them up
        # until the next 60s tick noticed.
        self._apply_capture_policy("system wake")
        # A wake can land on the far side of a boundary the sleeping process
        # slept through — recompute the next edge from the new now().
        self.coordinator.arm_capture_boundary()
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
        """Handle server config update — apply AFK timeout, then the working-hours
        capture policy the server just told us about."""
        self.aw_manager.set_afk_timeout(self.config.aw.afk_timeout_minutes * 60)

        # The schedule is already on disk by now: update_from_server() ends with
        # self.save(), which is what lets the NEXT cold start know the window
        # before it can reach the server. No second save here.

        # Re-evaluate immediately rather than waiting up to 60s for the next tick:
        # this is the moment a restricted user's agent first learns it is
        # restricted, and it may already be outside their window.
        self._apply_capture_policy("server config")
        # The schedule may have just changed or first resolved — re-align the
        # one-shot boundary trigger to the new next edge.
        self.coordinator.arm_capture_boundary()

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

        Error reports go to the cross-tenant BetterQA ops ingest (betterqa-bot),
        NOT the tenant's own server — so we deliberately do NOT attach the end
        user's email or name. device_id maps back to a user server-side for an
        admin who needs it, and user_role is enough to route/triage. This keeps
        PII off a shared sink (privacy audit, 2026-06-24).
        """
        ctx: dict = {"app_version": _VERSION}
        try:
            with self.tray.model.lock:
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
        # wait=True so an in-flight scheduled sync completes BEFORE we close the
        # offline queue below — otherwise it dies on a closed SQLite handle.
        self.coordinator.stop(wait=True)
        self.sync_engine.shutdown()
        if self.window_watcher:
            self.window_watcher.stop()
        if self.input_watcher:
            self.input_watcher.stop()
        if getattr(self, "input_source", None) is not None:
            try:
                self.input_source.stop()
            except Exception:
                logger.debug("In-process input source stop failed", exc_info=True)
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
            # Lock byte 0 explicitly. msvcrt.locking() locks bytes at the CURRENT
            # file position, and "a+" leaves the pointer at end-of-file — so once
            # a prior instance has written its PID, a second instance would open
            # at a non-zero offset, lock a DIFFERENT byte, and never conflict. That
            # let two BetterFlow instances run side-by-side on Windows, fighting
            # over the AW server port + input hook -> 0 events for the user.
            # Seeking to 0 first makes every instance contend for the same byte.
            # fcntl.flock ignores the position, so this is a no-op on Unix.
            self._file.seek(0)
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
                        # Unlock the SAME byte 0 we locked in acquire() (the file
                        # pointer is at end-of-file after writing the PID).
                        self._file.seek(0)
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
