"""BetterFlow Sync - Main entry point."""

import fcntl
import logging
import os
import signal
import sys
import threading
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Support both relative imports (module) and absolute imports (PyInstaller)
try:
    from .config import Config, setup_logging
    from .sync import AWClient, BetterFlowClient, SyncEngine, OfflineQueue
    from .sync.http_client import BetterFlowAuthError
    from .auth import KeychainManager, LoginManager
    from .ui.tray import TrayIcon, TrayState
    from .aw_manager import AWManager
    from .system_events import start_system_event_listener
except ImportError:
    from config import Config, setup_logging
    from sync import AWClient, BetterFlowClient, SyncEngine, OfflineQueue
    from sync.http_client import BetterFlowAuthError
    from auth import KeychainManager, LoginManager
    from ui.tray import TrayIcon, TrayState
    from aw_manager import AWManager
    from system_events import start_system_event_listener

logger = logging.getLogger(__name__)


class BetterFlowSyncApp:
    """Main application class."""

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

        self.sync_engine = SyncEngine(
            aw=self.aw,
            bf=self.bf,
            queue=self.queue,
            config=self.config,
            on_config_updated=self._on_config_updated,
        )

        self.login_manager = LoginManager(self.bf, self.keychain)

        # Scheduler for sync loop
        self.scheduler = BackgroundScheduler()

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
        )
        self.tray.set_config(self.config)

        # State
        self._running = False
        self._shutdown_event = threading.Event()
        self._logged_in = False
        self._hours_today_seconds = 0
        self._hours_today_cache = "0h 0m"

    def run(self) -> None:
        """Run the application."""
        # Register signal handlers
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
            self._logged_in = True
            # Set user on tray
            self.tray.set_user(state.user_email, state.user_name)

            # Fetch server config (privacy settings, sync intervals, category rules)
            self.sync_engine.fetch_server_config()

            # Fetch projects and set on tray
            self._fetch_projects()

            # Start sync loop
            self._start_sync_loop()
        else:
            self._logged_in = False
            self.tray.set_state(TrayState.WAITING_AUTH, "Waiting for browser login...")

        # Start system event listeners (sleep/wake, shutdown, network)
        start_system_event_listener(
            on_sleep=self._on_system_sleep,
            on_wake=self._on_system_wake,
            on_shutdown=self._on_system_shutdown,
            on_network_change=self._on_network_change,
        )

        # Start tray icon (blocking)
        self._running = True
        logger.info("BetterFlow Sync running")

        try:
            self.tray.run_blocking()
        finally:
            self._shutdown()

    def _start_sync_loop(self) -> None:
        """Start the sync scheduler."""
        # Initial sync
        self._do_sync()

        # Schedule periodic sync
        self.scheduler.add_job(
            self._do_sync,
            trigger=IntervalTrigger(seconds=self.config.sync.interval_seconds),
            id="sync_job",
            replace_existing=True,
        )
        # Keep tray "Hours today" fresh even with longer sync intervals.
        self.scheduler.add_job(
            self._refresh_hours_today,
            trigger=IntervalTrigger(seconds=60),
            id="tray_time_job",
            replace_existing=True,
        )
        self.scheduler.start()
        logger.info(
            f"Sync loop started (interval: {self.config.sync.interval_seconds}s)"
        )

    def _do_sync(self) -> None:
        """Perform a sync cycle."""
        try:
            # Skip sync entirely during private time
            if self.sync_engine.is_private:
                return

            # Health-check managed AW processes, restart if crashed
            if self.aw_manager.is_managing:
                self.aw_manager.restart_if_needed()

            # Check AW first
            if not self.aw.is_running():
                self.tray.set_state(TrayState.ERROR, "ActivityWatch not running")
                return

            # Sync
            stats = self.sync_engine.sync()

            # Update tray
            if stats.success:
                # Check queue capacity first (takes priority over queued state)
                if self.queue.is_near_capacity():
                    pct = int(self.queue.capacity_percent() * 100)
                    self.tray.set_state(TrayState.QUEUE_WARNING, f"Queue {pct}% full")
                    logger.warning(f"Offline queue at {pct}% capacity")
                elif stats.events_queued > 0:
                    self.tray.set_state(TrayState.QUEUED)
                else:
                    self.tray.set_state(TrayState.SYNCING)
            else:
                self.tray.set_state(TrayState.ERROR, stats.errors[0] if stats.errors else "Sync failed")

            # Update stats — fetch hours from API
            hours_today = self._fetch_hours_today()
            self.tray.update_stats(
                hours_today=hours_today,
                last_sync=self._format_time(datetime.now()),
                queue_size=self.queue.size(),
            )

            if stats.events_sent > 0 or stats.events_queued > 0:
                gaps_info = f", {stats.gaps_filled} gaps filled" if stats.gaps_filled > 0 else ""
                logger.info(
                    f"Sync complete: {stats.events_sent} sent, "
                    f"{stats.events_queued} queued, {stats.events_filtered} filtered"
                    f"{gaps_info}"
                )

        except BetterFlowAuthError as e:
            logger.warning(f"Auth error during sync: {e} — triggering re-login")
            self.tray.set_state(TrayState.WAITING_AUTH, "Session expired, re-login required")
            self._on_login()
        except Exception as e:
            logger.exception(f"Sync error: {e}")
            self.tray.set_state(TrayState.ERROR, "Sync error")

    def _fetch_hours_today(self) -> str:
        """Fetch today's tracked hours from API."""
        try:
            status = self.bf.get_status()
            total_seconds = int(status.get("data", {}).get("today_summary", {}).get("total_seconds", 0))
            self._hours_today_seconds = max(0, total_seconds)
            self._hours_today_cache = self._format_hours(self._hours_today_seconds)
            return self._hours_today_cache
        except Exception:
            return self._hours_today_cache

    def _refresh_hours_today(self) -> None:
        """Increment tray hours locally every minute while tracking is active."""
        try:
            if not self._logged_in:
                return
            if self.sync_engine.is_paused or self.sync_engine.is_private:
                return
            if not self.aw.is_running():
                return

            self._hours_today_seconds += 60
            self._hours_today_cache = self._format_hours(self._hours_today_seconds)
            self.tray.update_stats(hours_today=self._hours_today_cache)
        except Exception as e:
            logger.debug(f"Failed to refresh tray hours: {e}")

    def _format_time(self, dt: datetime) -> str:
        """Format time for display."""
        return dt.strftime("%H:%M")

    def _format_hours(self, total_seconds: int) -> str:
        """Format accumulated seconds as `Xh Ym` for tray display."""
        hours = int(total_seconds) // 3600
        minutes = (int(total_seconds) % 3600) // 60
        return f"{hours}h {minutes}m"

    def _on_login(self) -> None:
        """Handle explicit login action from tray."""
        def do_browser_login():
            self._logged_in = False
            self.tray.set_state(TrayState.WAITING_AUTH, "Waiting for browser login...")
            state = self.login_manager.login_via_browser()
            if state.logged_in:
                self._logged_in = True
                self.tray.set_user(state.user_email, state.user_name)
                self.sync_engine.fetch_server_config()
                self._fetch_projects()
                if not self.scheduler.running:
                    self._start_sync_loop()
            else:
                self._logged_in = False
                self.tray.set_state(TrayState.ERROR, state.error or "Login failed")

        threading.Thread(target=do_browser_login, daemon=True).start()

    def _on_pause(self) -> None:
        """Handle pause action."""
        self.sync_engine.pause()
        self.tray.set_paused(True)
        logger.info("Tracking paused")

    def _on_resume(self) -> None:
        """Handle resume action."""
        self.sync_engine.resume()
        self.tray.set_paused(False)
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
        else:
            logger.info("Private time ended — recording resumed")
            self.sync_engine.set_private_mode(False)

    def _on_system_sleep(self) -> None:
        """Handle system sleep / lid close."""
        self.sync_engine.pause()
        self.tray.set_state(TrayState.PAUSED, "Sleeping")
        logger.info("Tracking paused (system sleep)")

    def _on_system_wake(self) -> None:
        """Handle system wake from sleep."""
        self.sync_engine.resume()
        self.tray.set_state(TrayState.SYNCING)
        logger.info("Tracking resumed (system wake)")

        # Trigger immediate catch-up sync
        if self.scheduler.running:
            self.scheduler.add_job(self._do_sync, id="wake_sync", replace_existing=True)

    def _on_system_shutdown(self) -> None:
        """Handle system shutdown / restart."""
        logger.info("System shutdown detected — shutting down")
        self._shutdown()

    def _on_network_change(self, is_online: bool) -> None:
        """Handle network connectivity change."""
        if is_online:
            logger.info("Network back online — triggering sync to flush queue")
            if self.scheduler.running:
                self.scheduler.add_job(self._do_sync, id="network_sync", replace_existing=True)
        else:
            logger.info("Network offline")
            self.tray.set_state(TrayState.QUEUED, "Offline")

    def _on_config_updated(self) -> None:
        """Handle server config update — apply AFK timeout to AWManager."""
        self.aw_manager.set_afk_timeout(self.config.aw.afk_timeout_minutes * 60)

    def _fetch_projects(self) -> None:
        """Fetch available projects from API and set on tray."""
        try:
            response = self.bf.get_projects()
            projects = response.get("projects", [])
            self.tray.set_projects(projects)
            logger.info(f"Loaded {len(projects)} projects")
        except Exception as e:
            logger.warning(f"Failed to fetch projects: {e}")

    def _on_preferences(self, key: str, value) -> None:
        """Handle a preference change from tray menu."""
        if key == "sync_interval":
            self.config.sync.interval_seconds = value
            if self.scheduler.running:
                self.scheduler.reschedule_job(
                    "sync_job",
                    trigger=IntervalTrigger(seconds=value),
                )
        elif key == "hash_titles":
            self.config.privacy.hash_titles = value
        elif key == "domain_only_urls":
            self.config.privacy.domain_only_urls = value
        elif key == "debug_mode":
            self.config.debug_mode = value
            setup_logging(value)

        self.config.save()
        logger.info(f"Preference changed: {key} = {value}")

    def _on_logout(self) -> None:
        """Handle logout action."""
        self.login_manager.logout()
        self._logged_in = False
        self.tray.set_user(None)
        logger.info("Logged out")

        # Stop sync loop
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

        # Re-login via browser
        self.tray.set_state(TrayState.WAITING_AUTH, "Waiting for browser login...")

        def do_relogin():
            state = self.login_manager.login_via_browser()
            if state.logged_in:
                self._logged_in = True
                self.tray.set_user(state.user_email, state.user_name)
                self._start_sync_loop()
            else:
                self._logged_in = False
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

    def _shutdown(self) -> None:
        """Shutdown the application."""
        logger.info("Shutting down...")
        self._running = False

        # Stop scheduler
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

        # End sync session
        self.sync_engine.shutdown()

        # Close connections
        self.aw.close()
        self.bf.close()
        self.queue.close()

        # Stop bundled ActivityWatch
        self.aw_manager.stop()

        logger.info("Shutdown complete")


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
        self._file = open(self._path, "a+")
        try:
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
        app = BetterFlowSyncApp()
        app.run()
    finally:
        _instance_lock.release()


if __name__ == "__main__":
    main()
