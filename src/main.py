"""BetterFlow Sync - Main entry point."""

import logging
import signal
import threading
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Support both relative imports (module) and absolute imports (PyInstaller)
try:
    from .config import Config, setup_logging
    from .sync import AWClient, BetterFlowClient, SyncEngine, OfflineQueue
    from .auth import KeychainManager, LoginManager
    from .ui.tray import TrayIcon, TrayState
    from .ui.preferences import show_preferences_window
except ImportError:
    from config import Config, setup_logging
    from sync import AWClient, BetterFlowClient, SyncEngine, OfflineQueue
    from auth import KeychainManager, LoginManager
    from ui.tray import TrayIcon, TrayState
    from ui.preferences import show_preferences_window

logger = logging.getLogger(__name__)


class BetterFlowSyncApp:
    """Main application class."""

    def __init__(self):
        """Initialize the application."""
        self.config = Config.load()
        setup_logging(self.config.debug_mode)

        logger.info("BetterFlow Sync starting...")

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
            on_pause=self._on_pause,
            on_resume=self._on_resume,
            on_preferences=self._on_preferences,
            on_logout=self._on_logout,
            on_quit=self._on_quit,
        )

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

        # Try auto-login first (keychain)
        state = self.login_manager.try_auto_login()

        if not state.logged_in:
            # Need browser auth
            state = self.login_manager.login_via_browser()

        if not state.logged_in:
            logger.error("Not logged in — exiting")
            return

        # Set user on tray
        self.tray.set_user(state.user_email, state.user_name)

        # Start sync loop
        self._start_sync_loop()

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

        logger.info("Shutdown complete")


def main() -> None:
    """Main entry point."""
    app = BetterFlowSyncApp()
    app.run()


if __name__ == "__main__":
    main()
