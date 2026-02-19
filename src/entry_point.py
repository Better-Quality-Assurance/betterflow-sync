"""Entry point for PyInstaller bundle."""

import sys
import os

# Add the src directory to path for absolute imports
if getattr(sys, 'frozen', False):
    # Running as compiled
    bundle_dir = sys._MEIPASS
    sys.path.insert(0, bundle_dir)
else:
    # Running in development
    src_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, src_dir)

# Now import and run the app
from config import Config, setup_logging
from sync import AWClient, BetterFlowClient, SyncEngine, OfflineQueue
from auth import KeychainManager, LoginManager
from ui.tray import TrayIcon, TrayState
from aw_manager import AWManager

import fcntl
import logging
import signal
import threading
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)


class BetterFlowSyncApp:
    """Main application class."""

    def __init__(self):
        """Initialize the application."""
        self.config = Config.load()
        setup_logging(self.config.debug_mode)

        logger.info("BetterFlow Sync starting...")

        # Initialize AW process manager
        self.aw_manager = AWManager(aw_port=self.config.aw.port)

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
        )

        self.login_manager = LoginManager(self.bf, self.keychain)

        # Scheduler for sync loop
        self.scheduler = BackgroundScheduler()

        # Tray icon
        self.tray = TrayIcon(
            on_login=self._on_login,
            on_pause=self._on_pause,
            on_resume=self._on_resume,
            on_preferences=self._on_preference_changed,
            on_logout=self._on_logout,
            on_quit=self._on_quit,
        )
        self.tray.set_config(self.config)

        # State
        self._running = False
        self._events_today = 0
        self._events_today_date: Optional[datetime] = None
        self._shutdown_event = threading.Event()

    def run(self) -> None:
        """Run the application."""
        # Register signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # Always start ActivityWatch so events are collected from the start
        self.aw_manager.start()

        # Try auto-login
        login_state = self.login_manager.try_auto_login()

        if login_state.logged_in:
            self.tray.set_user(self.login_manager.get_current_user())
            self._start_services()
        else:
            # Show tray with "Login" — user clicks it to open browser
            self.tray.set_state(TrayState.WAITING_AUTH)

        # Start tray icon (blocking)
        self._running = True
        logger.info("BetterFlow Sync running")

        try:
            self.tray.run_blocking()
        finally:
            self._shutdown()

    def _start_services(self) -> None:
        """Fetch server config and begin sync loop (AW already started in run())."""
        self.sync_engine.fetch_server_config()
        self._start_sync_loop()

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
                if stats.events_queued > 0:
                    self.tray.set_state(TrayState.QUEUED)
                else:
                    self.tray.set_state(TrayState.SYNCING)
            else:
                self.tray.set_state(TrayState.ERROR, stats.errors[0] if stats.errors else "Sync failed")

            # Update stats
            self._update_events_today(stats.events_sent)
            self.tray.update_stats(
                events_today=self._events_today,
                last_sync=self._format_time(datetime.now()),
                queue_size=self.queue.size(),
            )

            if stats.events_sent > 0 or stats.events_queued > 0:
                logger.info(
                    f"Sync complete: {stats.events_sent} sent, "
                    f"{stats.events_queued} queued, {stats.events_filtered} filtered"
                )

        except Exception as e:
            logger.exception(f"Sync error: {e}")
            self.tray.set_state(TrayState.ERROR, "Sync error")

    def _update_events_today(self, new_events: int) -> None:
        """Update events today counter."""
        today = datetime.now().date()
        if self._events_today_date != today:
            self._events_today = 0
            self._events_today_date = today
        self._events_today += new_events

    def _format_time(self, dt: datetime) -> str:
        """Format time for display."""
        return dt.strftime("%H:%M")

    def _on_login(self) -> None:
        """Handle login action — open browser auth flow in background thread."""
        def do_browser_login():
            self.tray.set_state(TrayState.WAITING_AUTH)
            state = self.login_manager.login_via_browser()
            if state.logged_in:
                self.tray.set_user(state.user_email, state.user_name)
                self._start_services()
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

    def _on_preference_changed(self, key: str, value) -> None:
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
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        self.tray.set_user(None)
        self.tray.set_state(TrayState.WAITING_AUTH)
        logger.info("Logged out — waiting for re-login")

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


_lock_file = None


def _acquire_lock() -> bool:
    """Ensure only one instance is running. Returns True if lock acquired."""
    global _lock_file
    lock_path = os.path.join(
        Config.get_config_dir(), ".betterflow-sync.lock"
    )
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    _lock_file = open(lock_path, "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_file.write(str(os.getpid()))
        _lock_file.flush()
        return True
    except OSError:
        _lock_file.close()
        _lock_file = None
        return False


def main() -> None:
    """Main entry point."""
    if not _acquire_lock():
        print("BetterFlow Sync is already running.")
        sys.exit(0)

    app = BetterFlowSyncApp()
    app.run()


if __name__ == "__main__":
    main()
