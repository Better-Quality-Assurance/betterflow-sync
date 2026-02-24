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
    from .ui.preferences import show_preferences_window
    from .aw_manager import AWManager
    from .system_events import start_system_event_listener
except ImportError:
    from config import Config, setup_logging
    from sync import AWClient, BetterFlowClient, SyncEngine, OfflineQueue
    from sync.http_client import BetterFlowAuthError
    from auth import KeychainManager, LoginManager
    from ui.tray import TrayIcon, TrayState
    from ui.preferences import show_preferences_window
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

        # State
        self._running = False
        self._shutdown_event = threading.Event()

    def run(self) -> None:
        """Run the application."""
        # Register signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # First-run setup wizard
        wizard_login_state = None
        if not self.config.setup_complete:
            from .ui.setup_wizard import show_setup_wizard

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
            # Set user on tray
            self.tray.set_user(state.user_email, state.user_name)

            # Fetch server config (privacy settings, sync intervals, category rules)
            self.sync_engine.fetch_server_config()

            # Fetch projects and set on tray
            self._fetch_projects()

            # Start sync loop
            self._start_sync_loop()
        else:
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
                logger.info(
                    f"Sync complete: {stats.events_sent} sent, "
                    f"{stats.events_queued} queued, {stats.events_filtered} filtered"
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
            total_seconds = status.get("data", {}).get("today_summary", {}).get("total_seconds", 0)
            hours = int(total_seconds) // 3600
            minutes = (int(total_seconds) % 3600) // 60
            return f"{hours}h {minutes}m"
        except Exception:
            return self._hours_today_cache if hasattr(self, '_hours_today_cache') else "0h 0m"

    def _format_time(self, dt: datetime) -> str:
        """Format time for display."""
        return dt.strftime("%H:%M")

    def _on_login(self) -> None:
        """Handle explicit login action from tray."""
        def do_browser_login():
            self.tray.set_state(TrayState.WAITING_AUTH, "Waiting for browser login...")
            state = self.login_manager.login_via_browser()
            if state.logged_in:
                self.tray.set_user(state.user_email, state.user_name)
                self.sync_engine.fetch_server_config()
                self._fetch_projects()
                if not self.scheduler.running:
                    self._start_sync_loop()
            else:
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

    def _on_preferences(self) -> None:
        """Handle preferences action."""

        def on_save(config: Config) -> None:
            self.config = config
            config.save()

            # Update sync interval if changed
            if self.scheduler.running:
                self.scheduler.reschedule_job(
                    "sync_job",
                    trigger=IntervalTrigger(seconds=config.sync.interval_seconds),
                )

            logger.info("Preferences saved")

        # Show in new thread to not block tray
        threading.Thread(
            target=lambda: show_preferences_window(self.config, on_save),
            daemon=True,
        ).start()

    def _on_logout(self) -> None:
        """Handle logout action."""
        self.login_manager.logout()
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
                self.tray.set_user(state.user_email, state.user_name)
                self._start_sync_loop()
            else:
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

        # Release single-instance lock
        _release_lock()

        logger.info("Shutdown complete")


_lock_file = None


def _acquire_lock() -> bool:
    """Ensure only one instance is running. Returns True if lock acquired."""
    global _lock_file
    lock_path = os.path.join(
        Config.get_config_dir(), ".betterflow-sync.lock"
    )
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    _lock_file = open(lock_path, "a+")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_file.seek(0)
        _lock_file.truncate(0)
        _lock_file.write(str(os.getpid()))
        _lock_file.flush()
        return True
    except OSError:
        _lock_file.close()
        _lock_file = None
        return False


def _release_lock() -> None:
    """Release the single-instance lock."""
    global _lock_file
    if _lock_file:
        try:
            lock_path = _lock_file.name
            fcntl.flock(_lock_file, fcntl.LOCK_UN)
            _lock_file.close()
            os.unlink(lock_path)
        except Exception:
            pass
        _lock_file = None


def main() -> None:
    """Main entry point."""
    if not _acquire_lock():
        print("BetterFlow Sync is already running.")
        sys.exit(0)

    app = BetterFlowSyncApp()
    app.run()


if __name__ == "__main__":
    main()
