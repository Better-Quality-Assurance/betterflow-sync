"""BetterFlow Sync - Main entry point."""

import logging
import os
import signal
import sys
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Support both relative imports (module) and absolute imports (PyInstaller)
try:
    from .__init__ import __version__
    from .auth import KeychainManager, LoginManager
    from .aw_manager import AWManager
    from .config import Config, setup_logging
    from .display_info import start_display_tracker
    from .reminders import ReminderManager
    from .sync import AWClient, BetterFlowClient, OfflineQueue, SyncEngine
    from .sync.http_client import BetterFlowAuthError
    from .system_events import start_system_event_listener
    from .ui.tray import TrayIcon, TrayState
    from .update_checker import check_for_update
except ImportError:
    from src import __version__
    from auth import KeychainManager, LoginManager
    from aw_manager import AWManager
    from config import Config, setup_logging
    from display_info import start_display_tracker
    from reminders import ReminderManager
    from sync import AWClient, BetterFlowClient, OfflineQueue, SyncEngine
    from sync.http_client import BetterFlowAuthError
    from system_events import start_system_event_listener
    from ui.tray import TrayIcon, TrayState
    from update_checker import check_for_update

logger = logging.getLogger(__name__)


class SyncCoordinator:
    """Owns the sync scheduler, sync loop, and hours tracking.

    Pulled out of BetterFlowSyncApp so that the app class focuses on
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
    ) -> None:
        self.config = config
        self.aw = aw
        self.bf = bf
        self.queue = queue
        self.sync_engine = sync_engine
        self.tray = tray
        self.aw_manager = aw_manager
        self.reminder_manager = reminder_manager

        self.scheduler = BackgroundScheduler()
        self._hours_today_seconds = 0
        self._hours_today_cache = "0h 0m"
        self._last_tick: Optional[datetime] = None
        self._trends_cache: dict[str, str] = {
            "hours_this_week": "---",
            "hours_this_month": "---",
            "daily_avg_this_week": "---",
        }

        # Flags set by the app layer
        self.logged_in = False
        self.paused_by_network = False
        self._sync_in_progress = False

        # Optional callback wired by the app for auth-error re-login
        self._on_auth_error: Optional[Callable] = None

    def start(self) -> None:
        """Run the initial sync and start the periodic scheduler."""
        # BackgroundScheduler.shutdown() is terminal -- create a fresh one
        # if the previous scheduler was shut down (e.g. after logout/re-login).
        if not self.scheduler.running:
            self.scheduler = BackgroundScheduler()

        self._do_sync()
        self._fetch_trends()

        self.scheduler.add_job(
            self._do_sync,
            trigger=IntervalTrigger(seconds=self.config.sync.interval_seconds),
            id="sync_job",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._refresh_hours_today,
            trigger=IntervalTrigger(seconds=60),
            id="tray_time_job",
            replace_existing=True,
        )
        if self.reminder_manager:
            self.scheduler.add_job(
                self.reminder_manager.check,
                trigger=IntervalTrigger(seconds=60),
                id="reminder_check_job",
                replace_existing=True,
            )
        # Expire stale queue events daily
        self.scheduler.add_job(
            self._expire_old_queue_events,
            trigger=IntervalTrigger(hours=24),
            id="queue_expire_job",
            replace_existing=True,
        )
        # Refresh app categories every 6 hours
        self.scheduler.add_job(
            self.fetch_categories,
            trigger=IntervalTrigger(hours=6),
            id="category_refresh_job",
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

    def fetch_projects(self) -> None:
        """Fetch available projects from API and set on tray."""
        try:
            response = self.bf.get_projects()
            projects = response.get("projects", [])
            self.tray.set_projects(projects)
            logger.info(f"Loaded {len(projects)} projects")
        except Exception as e:
            logger.warning(f"Failed to fetch projects: {e}")

    def fetch_categories(self) -> None:
        """Fetch app-to-category mappings from server and sync to local DB."""
        try:
            response = self.bf.get_categories()
            mappings = response.get("categories", {})
            self.queue.sync_categories(mappings)
            self.sync_engine.invalidate_category_cache()
            logger.info(f"Synced {len(mappings)} app categories")
        except Exception as e:
            logger.warning(f"Failed to fetch categories: {e}")

    # -- internal ---------------------------------------------------------

    def _do_sync(self) -> None:
        """Perform a sync cycle."""
        try:
            self._sync_in_progress = True

            if self.sync_engine.is_private:
                return

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

            if stats.success:
                if self.queue.is_near_capacity():
                    pct = int(self.queue.capacity_percent() * 100)
                    self.tray.set_state(TrayState.QUEUE_WARNING, f"Queue {pct}% full")
                    logger.warning(f"Offline queue at {pct}% capacity")
                elif stats.events_queued > 0:
                    self.tray.set_state(TrayState.QUEUED)
                else:
                    self.tray.set_state(TrayState.SYNCING)
            else:
                self.tray.set_state(
                    TrayState.ERROR,
                    stats.errors[0] if stats.errors else "Sync failed",
                )

            # Use local active time tracking (more accurate for engaged work)
            active_time = self.sync_engine.get_today_active_time()
            self.tray.set_active_time(active_time)
            self.tray.update_stats(
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
            logger.warning(f"Auth error during sync: {e} — triggering re-login")
            self.tray.set_state(
                TrayState.WAITING_AUTH, "Session expired, re-login required"
            )
            if self._on_auth_error:
                self._on_auth_error()
        except Exception as e:
            logger.exception(f"Sync error: {e}")
            self.tray.set_state(TrayState.ERROR, "Sync error")
        finally:
            self._sync_in_progress = False

    def _refresh_hours_today(self) -> None:
        """Refresh tray hours from local active time tracker."""
        try:
            if not self.logged_in:
                return
            if self.paused_by_network:
                return
            if self.sync_engine.is_paused or self.sync_engine.is_private:
                return

            # Use local active time tracker (already handles engagement detection)
            active_time = self.sync_engine.get_today_active_time()
            self.tray.set_active_time(active_time)
        except Exception as e:
            logger.debug(f"Failed to refresh tray hours: {e}")

    def _fetch_trends(self) -> None:
        """Fetch weekly/monthly trend data from server."""
        try:
            if not self.logged_in:
                return
            response = self.bf.get_trends()
            data = response.get("data", {})
            self._trends_cache = {
                "hours_this_week": self._format_hours(int(data.get("week_total_seconds", 0))),
                "hours_this_month": self._format_hours(int(data.get("month_total_seconds", 0))),
                "daily_avg_this_week": self._format_hours(int(data.get("week_daily_avg_seconds", 0))),
            }
            self.tray.update_stats(**self._trends_cache)
        except Exception as e:
            logger.debug(f"Failed to fetch trends: {e}")

    def reset_trends(self) -> None:
        """Reset cached trends to placeholder values."""
        self._trends_cache = {
            "hours_this_week": "---",
            "hours_this_month": "---",
            "daily_avg_this_week": "---",
        }
        self.tray.update_stats(**self._trends_cache)

    def _expire_old_queue_events(self) -> None:
        """Remove queue events older than 30 days."""
        try:
            self.queue.expire_old(max_age_days=30)
        except Exception as e:
            logger.debug(f"Failed to expire old queue events: {e}")

    @staticmethod
    def _format_hours(total_seconds: int) -> str:
        """Format accumulated seconds as `Xh Ym` for tray display."""
        hours = int(total_seconds) // 3600
        minutes = (int(total_seconds) % 3600) // 60
        return f"{hours}h {minutes}m"

class BetterFlowSyncApp:
    """Main application orchestrator.

    Wires components together, handles lifecycle (start / shutdown),
    and routes tray-menu and system events to the appropriate handler.
    """

    def __init__(self):
        """Initialize the application."""
        self.config = Config.load()
        setup_logging(self.config.debug_mode)

        logger.info("BetterFlow Sync starting...")
        logger.info(f"Using API URL: {self.config.api_url}")

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
        if self.config.privacy.track_display_info:
            self.display_tracker = start_display_tracker()
        else:
            self.display_tracker = None

        self.sync_engine = SyncEngine(
            aw=self.aw,
            bf=self.bf,
            queue=self.queue,
            config=self.config,
            on_config_updated=self._on_config_updated,
            display_tracker=self.display_tracker,
        )

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
        )
        self.tray.set_config(self.config)

        # Reminder manager
        self.reminder_manager = ReminderManager(self.config.reminders)

        # Sync coordinator
        self.coordinator = SyncCoordinator(
            config=self.config,
            aw=self.aw,
            bf=self.bf,
            queue=self.queue,
            sync_engine=self.sync_engine,
            tray=self.tray,
            aw_manager=self.aw_manager,
            reminder_manager=self.reminder_manager,
        )
        self.coordinator._on_auth_error = self._on_login

        # State
        self._shutdown_done = False
        self._shutdown_event = threading.Event()
        self._user_paused = False

    def run(self) -> None:
        """Run the application."""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

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
            self.config.save()
            if result.logged_in and result.login_state:
                wizard_login_state = result.login_state

        # Try auto-login first (keychain), unless wizard already logged in
        if wizard_login_state and wizard_login_state.logged_in:
            state = wizard_login_state
        else:
            state = self.login_manager.try_auto_login()

        # Start bundled ActivityWatch
        self.aw_manager.start()

        if state.logged_in:
            self.coordinator.logged_in = True
            self.tray.set_user(state.user_email, state.user_name)
            self.sync_engine.fetch_server_config()
            self.coordinator.fetch_projects()
            self.coordinator.fetch_categories()
            self._check_stale_session()
            self._check_accessibility_permission()
            self.coordinator.start()
        else:
            self.coordinator.logged_in = False
            self.tray.set_state(TrayState.WAITING_AUTH, "Waiting for browser login...")

        # Start system event listeners (non-critical: don't let failures prevent tray)
        try:
            start_system_event_listener(
                on_sleep=self._on_system_sleep,
                on_wake=self._on_system_wake,
                on_shutdown=self._on_system_shutdown,
                on_network_change=self._on_network_change,
                on_screen_lock=self._on_screen_lock,
                on_screen_unlock=self._on_screen_unlock,
            )
        except Exception:
            logger.exception("Failed to start system event listeners")

        # Check for updates in background
        try:
            if self.config.check_updates:
                check_for_update(
                    __version__,
                    channel=self.config.update_channel,
                    callback=self._on_update_available,
                )
        except Exception:
            logger.exception("Failed to start update checker")

        logger.info("BetterFlow Sync running")
        try:
            self.tray.run_blocking()
        finally:
            self._shutdown()

    # -- Event handlers ---------------------------------------------------

    def _check_accessibility_permission(self) -> None:
        """On macOS, check if ActivityWatch can read window titles.

        If no window buckets are found after AW is running, it likely means
        Accessibility permission hasn't been granted to aw-watcher-window.
        """
        if sys.platform != "darwin":
            return
        try:
            import time
            time.sleep(3)  # Give AW a moment to start watchers
            window_buckets = self.aw.get_window_buckets()
            if not window_buckets:
                logger.warning(
                    "No window tracking detected — ActivityWatch may need "
                    "Accessibility permission in System Settings > Privacy & Security"
                )
                try:
                    from .notifications import send_notification
                except ImportError:
                    from notifications import send_notification
                send_notification(
                    "BetterFlow Sync",
                    "Grant Accessibility permission to ActivityWatch in "
                    "System Settings > Privacy & Security for window tracking.",
                )
        except Exception as e:
            logger.debug(f"Accessibility check failed: {e}")

    def _on_update_available(self, version: str, url: str) -> None:
        """Handle update available notification."""
        logger.info(f"Update available: v{version} — {url}")
        try:
            from .notifications import send_notification
        except ImportError:
            from notifications import send_notification
        send_notification(
            "BetterFlow Sync Update",
            f"Version {version} is available. Click to download.",
        )

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
                    pass
        except Exception as e:
            logger.debug(f"Stale session check failed: {e}")

    def _on_login(self) -> None:
        """Handle explicit login action from tray."""
        def do_browser_login():
            self.coordinator.logged_in = False
            self.tray.set_state(TrayState.WAITING_AUTH, "Waiting for browser login...")
            state = self.login_manager.login_via_browser()
            if state.logged_in:
                self.coordinator.logged_in = True
                self.tray.set_user(state.user_email, state.user_name)
                self.sync_engine.fetch_server_config()
                self.coordinator.fetch_projects()
                self.coordinator.fetch_categories()
                if not self.coordinator.scheduler.running:
                    self.coordinator.start()
            else:
                self.coordinator.logged_in = False
                self.tray.set_state(TrayState.ERROR, state.error or "Login failed")

        threading.Thread(target=do_browser_login, daemon=True).start()

    def _on_pause(self) -> None:
        """Handle pause action."""
        self._user_paused = True
        self.coordinator.paused_by_network = False
        self.sync_engine.pause()
        self.tray.set_paused(True)
        self.reminder_manager.on_tracking_stopped()
        logger.info("Tracking paused")

    def _on_resume(self) -> None:
        """Handle resume action."""
        self._user_paused = False
        self.coordinator.paused_by_network = False
        self.sync_engine.resume()
        self.tray.set_paused(False)
        self.reminder_manager.on_tracking_started()
        logger.info("Tracking resumed")

    def _on_project_change(self, project: Optional[dict]) -> None:
        """Handle project switch from tray."""
        if project:
            logger.info(f"Switched to project: {project['name']}")
        else:
            logger.info("Cleared project selection")
        self.sync_engine.set_current_project(project)

    def _on_private_toggle(self, private: bool) -> None:
        """Handle private time toggle."""
        if private:
            logger.info("Private time started — recording paused")
            self.sync_engine.set_private_mode(True)
            self.reminder_manager.on_tracking_stopped()
            self.reminder_manager.on_private_started()
        else:
            logger.info("Private time ended — recording resumed")
            self.sync_engine.set_private_mode(False)
            self.reminder_manager.on_private_ended()
            self.reminder_manager.on_tracking_started()

    def _on_sync_now(self) -> None:
        """Handle sync now action from tray."""
        logger.info("Manual sync triggered")
        self.coordinator.trigger_sync()

    def _on_system_sleep(self) -> None:
        """Handle system sleep / lid close."""
        self.coordinator.paused_by_network = False
        self.sync_engine.pause()
        self.tray.set_state(TrayState.PAUSED, "Sleeping")
        self.reminder_manager.on_tracking_stopped()
        logger.info("Tracking paused (system sleep)")

    def _on_system_wake(self) -> None:
        """Handle system wake from sleep."""
        # Drop stale TCP connections from sleep (N3)
        self.bf.reset_session()
        if self._user_paused:
            logger.info("System wake — staying paused (user-initiated pause active)")
            return
        self.sync_engine.resume()
        self.tray.set_state(TrayState.SYNCING)
        self.reminder_manager.on_tracking_started()
        logger.info("Tracking resumed (system wake)")
        self.coordinator.trigger_sync("wake_sync")

    def _on_system_shutdown(self) -> None:
        """Handle system shutdown / restart."""
        logger.info("System shutdown detected — shutting down")
        self._shutdown()

    def _on_screen_lock(self) -> None:
        """Handle screen lock — treat as AFK."""
        logger.info("Screen locked — pausing tracking")
        self.sync_engine.pause()
        self.tray.set_state(TrayState.PAUSED, "Screen locked")
        self.reminder_manager.on_tracking_stopped()

    def _on_screen_unlock(self) -> None:
        """Handle screen unlock — resume tracking."""
        if self._user_paused:
            logger.info("Screen unlocked — staying paused (user-initiated pause active)")
            return
        logger.info("Screen unlocked — resuming tracking")
        self.sync_engine.resume()
        self.tray.set_state(TrayState.SYNCING)
        self.reminder_manager.on_tracking_started()
        self.coordinator.trigger_sync("unlock_sync")

    def _on_network_change(self, is_online: bool) -> None:
        """Handle network connectivity change."""
        if is_online:
            logger.info("Network back online — triggering sync to flush queue")
            if self.coordinator.paused_by_network:
                self.sync_engine.resume()
                self.coordinator.paused_by_network = False
            self.coordinator.trigger_sync("network_sync")
        else:
            # Don't interrupt an in-flight sync (N4) - let it complete or timeout
            if self.coordinator._sync_in_progress:
                logger.info("Network offline — sync in progress, will pause after completion")
                self.coordinator.paused_by_network = True
                self.tray.set_state(TrayState.QUEUED, "Offline")
            else:
                logger.info("Network offline — pausing sync immediately")
                self.sync_engine.pause()
                self.coordinator.paused_by_network = True
                self.tray.set_state(TrayState.QUEUED, "Offline")

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
            check_for_update(
                __version__,
                channel=value,
                callback=self._on_update_available,
            )
        self.config.save()
        logger.info(f"Preference changed: {key} = {value}")

    def _on_logout(self) -> None:
        """Handle logout action."""
        # End server session before stopping
        self.sync_engine.shutdown()
        self.login_manager.logout()
        self.coordinator.logged_in = False
        self.coordinator.reset_trends()
        self.tray.set_user(None)
        logger.info("Logged out")

        self.coordinator.stop()

        self.tray.set_state(TrayState.WAITING_AUTH, "Waiting for browser login...")

        def do_relogin():
            state = self.login_manager.login_via_browser()
            if state.logged_in:
                self.coordinator.logged_in = True
                self.tray.set_user(state.user_email, state.user_name)
                self.coordinator.start()
            else:
                self.coordinator.logged_in = False
                self._on_quit()

        threading.Thread(target=do_relogin, daemon=True).start()

    def _on_quit(self) -> None:
        """Handle quit action."""
        logger.info("Quit requested")
        self._shutdown_event.set()
        self.tray.stop()

    def _signal_handler(self, signum, frame) -> None:
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}")
        self._shutdown_event.set()
        self.tray.stop()

    # -- Lifecycle --------------------------------------------------------

    def _shutdown(self) -> None:
        """Shutdown the application. Safe to call multiple times."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        logger.info("Shutting down...")

        self.coordinator.stop()
        self.sync_engine.shutdown()
        if self.display_tracker is not None:
            self.display_tracker.stop()
        self.aw.close()
        self.bf.close()
        self.queue.close()
        self.aw_manager.stop()

        logger.info("Shutdown complete")

    def __enter__(self) -> "BetterFlowSyncApp":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._shutdown()


class SingleInstanceLock:
    """File-based single-instance lock using advisory locking."""

    def __init__(self):
        self._file = None
        self._path = os.path.join(
            Config.get_config_dir(), ".betterflow-sync.lock"
        )

    def acquire(self) -> bool:
        """Try to acquire the lock. Returns True on success."""
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
        if self._file:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    try:
                        msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl
                    fcntl.flock(self._file, fcntl.LOCK_UN)
                self._file.close()
                os.unlink(self._path)
            except OSError:
                pass
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
        print("BetterFlow Sync is already running.")
        sys.exit(0)

    try:
        with BetterFlowSyncApp() as app:
            app.run()
    finally:
        _instance_lock.release()


if __name__ == "__main__":
    main()
