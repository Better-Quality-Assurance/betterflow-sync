"""Update lifecycle: check, stage in the background, and apply on launch/idle.

Model (chosen): updates are downloaded ("staged") in the background while the
app runs and applied at a non-disruptive moment:
  - the initial check right after launch applies immediately (catch-on-launch);
  - periodic (every 30 min) checks notify the user once per version, stage
    silently, and apply on the next restart or when the user goes idle.
Applying a staged build is loop-safe (see src/self_updater.get_staged_update).
"""

import logging
import threading
import time
from typing import Optional

try:
    from .update_checker import _version_tuple
except ImportError:
    from update_checker import _version_tuple

try:
    from .notifications import send_notification
except ImportError:
    from notifications import send_notification

logger = logging.getLogger(__name__)


class UpdateHandler:
    """Manages the update check/stage/apply lifecycle for BetterFlowApp.

    Owns _staged_lock and _update_jobs_lock (leaf locks).
    """

    def __init__(self, tray, config, coordinator, version: str) -> None:
        self.tray = tray
        self.config = config
        self.coordinator = coordinator
        self._version = version

        # Version of the currently-staged (downloaded, not yet applied) update.
        self._staged_version: Optional[str] = None
        # Version we last toasted the user about, so the 30-min re-checks notify
        # once per version instead of on every check.
        self._notified_version: Optional[str] = None
        self._staged_lock = threading.Lock()
        self._update_jobs_started = False
        self._update_jobs_lock = threading.Lock()

        # Throttle for server-pushed update checks (heartbeat-driven). The
        # heartbeat fires every ~5 min and keeps reporting the floor until the
        # agent has updated, so without this we'd re-hit GitHub / re-stage every
        # beat. monotonic so a clock change can't wedge it. Seeded to -inf (NOT
        # 0.0) so the FIRST push is never throttled: on a freshly-booted machine
        # monotonic() is small and `now - 0.0 < THROTTLE` would wrongly suppress
        # the first check (passes on a long-uptime box, fails on a fresh runner).
        self._last_remote_update_check = float("-inf")

    # Min gap between heartbeat-driven update checks.
    _REMOTE_UPDATE_THROTTLE = 1800.0  # 30 min

    def _flush_before_update_exit(self) -> None:
        """Best-effort flush of pending state right before the process is
        replaced by an update and relaunched. Shared by the staged-apply and
        manual-install paths."""
        try:
            self.coordinator.flush_idle_event()
        except Exception:
            logger.warning("flush_idle_event() during update exit failed", exc_info=True)
        try:
            self.coordinator.sync_engine.shutdown()
        except Exception:
            logger.warning("sync_engine.shutdown() during update exit failed", exc_info=True)
        # Guarded: coordinator.stop() shuts the scheduler down, and APScheduler
        # can raise there (racy `if scheduler.running` check). An exception must
        # not skip the aw_manager.stop() below — that's the tracker-orphaning
        # regression described next.
        try:
            self.coordinator.stop()
        except Exception:
            logger.warning("coordinator.stop() during update exit failed", exc_info=True)
        # Terminate the bundled trackers BEFORE the updater's hard os._exit(0).
        # coordinator.stop() only stops the scheduler; without this the
        # self-update relaunch left bf-idle-tracker orphaned, so the new
        # instance started a second one and the two fought over the AFK bucket
        # (recurring "idle frozen / hours undercounted after update").
        try:
            self.coordinator.aw_manager.stop()
        except Exception:
            logger.warning("aw_manager.stop() during update exit failed", exc_info=True)

    def ensure_update_checks_started(self) -> None:
        """Kick off update checks once after the tray is already visible."""
        if not self.config.check_updates:
            return
        with self._update_jobs_lock:
            if self._update_jobs_started:
                return
            self._update_jobs_started = True

        try:
            from .update_checker import check_for_update
        except ImportError:
            from update_checker import check_for_update

        from apscheduler.triggers.interval import IntervalTrigger

        try:
            # Initial check is at launch, so apply immediately if an update is
            # found (catch-on-launch). Periodic checks stage for next restart.
            check_for_update(
                self._version,
                channel=self.config.update_channel,
                callback=lambda v, u, a=None: self._on_update_available(v, u, a, apply_now=True),
            )
            if self.coordinator.scheduler.running:
                self.coordinator.scheduler.add_job(
                    self._periodic_update_check,
                    trigger=IntervalTrigger(minutes=30),
                    id="update_check_job",
                    replace_existing=True,
                )
        except Exception:
            logger.exception("Failed to start update checker")

    def try_auto_install(self) -> None:
        """Apply a staged update if one is ready (called when the user is idle)."""
        if not self.config.auto_install_updates:
            return
        with self.tray.model.lock:
            if self.tray.model.update_in_progress:
                return
        # No-op if nothing valid is staged.
        self._apply_staged_update()

    def _on_update_available(
        self, version: str, url: str, asset_url: Optional[str] = None, apply_now: bool = False
    ) -> None:
        """Handle an available update: notify the user (once per version) and
        stage it (and maybe apply)."""
        logger.info(f"Update available: v{version} | {url} (asset: {asset_url})")
        self.tray.set_update_available(version, url, asset_url)

        # Notify ONCE per version, at detection — so the user always learns an
        # update is available, including on the otherwise-silent auto-apply path,
        # and the 30-min re-checks don't re-toast the same version.
        if version != self._notified_version:
            self._notified_version = version
            if not asset_url:
                msg = f"Version {version} is available."
            elif not self.config.auto_install_updates:
                msg = f"Version {version} is available. Click 'Install & Restart' in the menu."
            else:
                msg = f"Version {version} is available and will be installed automatically."
            send_notification("BetterFlow Update", msg)

        if not asset_url or not self.config.auto_install_updates:
            return

        # Already downloaded this version — it applies on idle/restart; don't
        # re-download it on every 30-min check (only re-stage a newer version).
        with self._staged_lock:
            already_staged = self._staged_version == version
        if already_staged:
            if apply_now:
                self._apply_staged_update()
            return

        self._stage_and_maybe_apply(version, asset_url, apply_now)

    def _stage_and_maybe_apply(self, version: str, asset_url: str, apply_now: bool) -> None:
        """Download the update in the background, then apply or defer."""
        try:
            from .self_updater import stage_update_async
        except ImportError:
            from self_updater import stage_update_async

        def _on_staged(ok: bool) -> None:
            if not ok:
                logger.debug("Staging update v%s failed; will retry on next check", version)
                return
            with self._staged_lock:
                self._staged_version = version
            if apply_now or self.coordinator.idle_paused:
                self._apply_staged_update()
            # Otherwise it's staged and applies on the next restart/idle. The user
            # was already notified once at detection, so no second toast here.

        stage_update_async(asset_url, version, on_complete=_on_staged)

    def _apply_staged_update(self) -> None:
        """Apply the staged update (no-op if none is staged). Relaunches on success."""
        with self.tray.model.lock:
            if self.tray.model.update_in_progress:
                return
            self.tray.model.update_in_progress = True
        self.tray._update_menu()

        def on_progress(status: str) -> None:
            self.tray.set_update_progress(status)

        def _run() -> None:
            try:
                from .self_updater import apply_staged_update
            except ImportError:
                from self_updater import apply_staged_update
            applied = apply_staged_update(
                self._version, on_progress=on_progress, on_pre_exit=self._flush_before_update_exit
            )
            # On success the process relaunches and never returns here. If it
            # didn't apply (nothing staged, or apply failed), clear the flag.
            if not applied:
                with self.tray.model.lock:
                    self.tray.model.update_in_progress = False
                self.tray._update_menu()

        threading.Thread(target=_run, name="staged-update-apply", daemon=True).start()

    def _periodic_update_check(self) -> None:
        """Re-check for updates (called every 30 min by the scheduler)."""
        if not self.config.check_updates:
            return
        with self.tray.model.lock:
            if self.tray.model.update_in_progress:
                return

        try:
            from .update_checker import check_for_update
        except ImportError:
            from update_checker import check_for_update

        try:
            # Periodic checks stage silently (apply_now=False) — non-disruptive
            # mid-session; the staged build applies on next restart or idle.
            check_for_update(
                self._version,
                channel=self.config.update_channel,
                callback=self._on_update_available,
            )
        except Exception:
            logger.debug("Periodic update check failed", exc_info=True)

    def trigger_remote_update(self, target_version: str) -> None:
        """Server (via the heartbeat's minimum_agent_version) says the fleet
        should be at least `target_version` and this agent is below it. Kick an
        off-cycle update check so the latest build stages now and applies on the
        next idle/restart — reaching the fleet in minutes instead of waiting up
        to 30 min for the periodic check. Non-disruptive (no mid-session restart).

        Guards: respects check_updates; skips if a build >= target is already
        staged (it'll apply on idle); and throttles to one check per
        _REMOTE_UPDATE_THROTTLE so the recurring heartbeat can't re-download.
        """
        if not self.config.check_updates:
            return

        # Already have it (or newer) downloaded — nothing to fetch; it applies
        # on the next idle/restart via the staged-update path.
        with self._staged_lock:
            staged = self._staged_version
        if staged is not None:
            try:
                if _version_tuple(staged) >= _version_tuple(target_version):
                    return
            except (ValueError, TypeError):
                pass

        now = time.monotonic()
        with self._update_jobs_lock:
            if now - self._last_remote_update_check < self._REMOTE_UPDATE_THROTTLE:
                return
            self._last_remote_update_check = now

        logger.info(
            "Server pushed update floor %s (agent %s) — staging latest build",
            target_version,
            self._version,
        )
        self._periodic_update_check()

    def on_install_update(self, asset_url: str) -> None:
        """Manual 'Install & Restart': download + apply immediately.

        The tray's click handler already set update_in_progress=True before
        calling this, so we don't re-guard or re-set it here.
        """
        try:
            from .self_updater import apply_update_async
        except ImportError:
            from self_updater import apply_update_async

        def on_progress(status: str) -> None:
            self.tray.set_update_progress(status)

        def on_complete(success: bool) -> None:
            if not success:
                with self.tray.model.lock:
                    self.tray.model.update_in_progress = False
                self.tray._update_menu()
                send_notification("Update Failed", "Self-update failed. Try again later.")

        apply_update_async(
            asset_url,
            on_progress=on_progress,
            on_complete=on_complete,
            on_pre_exit=self._flush_before_update_exit,
        )
