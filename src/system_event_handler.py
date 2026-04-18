"""System event handlers: sleep/wake, screen lock/unlock, network changes."""

import logging
import threading

try:
    from .notifications import send_notification
except ImportError:
    from notifications import send_notification

logger = logging.getLogger(__name__)


class SystemEventHandler:
    """Routes system events (sleep, wake, lock, network) to appropriate actions.

    Uses _pause_state_lock from the parent app for coordinating pause state.
    """

    def __init__(
        self,
        sync_engine,
        tray,
        coordinator,
        reminder_manager,
        bf,
        aw,
        pause_state_lock: threading.RLock,
        shutdown_fn,
    ) -> None:
        self.sync_engine = sync_engine
        self.tray = tray
        self.coordinator = coordinator
        self.reminder_manager = reminder_manager
        self.bf = bf
        self.aw = aw
        self._pause_state_lock = pause_state_lock
        self._shutdown_fn = shutdown_fn

        # Shared state protected by _pause_state_lock
        self._user_paused = False
        self._pre_sleep_private = False
        self._pre_lock_private = False

        # Update handler reference (set after construction)
        self.update_handler = None

    def on_system_sleep(self) -> None:
        """Handle system sleep / lid close."""
        try:
            from .ui.tray import TrayState
        except ImportError:
            from ui.tray import TrayState

        with self._pause_state_lock:
            self._pre_sleep_private = self.sync_engine.is_private
        self.coordinator.paused_by_network = False
        self.coordinator.clear_idle_pause(send_event=True)
        self.sync_engine.pause()
        self.bf.reset_session()
        self.aw.reset_session()
        self.tray.set_state(TrayState.PAUSED, "Sleeping")
        self.reminder_manager.on_tracking_stopped()
        logger.info("Tracking paused (system sleep)")

    def on_system_wake(self) -> None:
        """Handle system wake from sleep."""
        try:
            from .ui.tray import TrayState
        except ImportError:
            from ui.tray import TrayState

        try:
            from .main import _day_greeting
        except ImportError:
            from main import _day_greeting

        self.bf.reset_session()
        self.aw.reset_session()
        with self._pause_state_lock:
            user_paused = self._user_paused
            pre_sleep_private = self._pre_sleep_private
        if user_paused:
            logger.info("System wake - staying paused (user-initiated pause active)")
            return
        if self.coordinator.is_on_break:
            logger.info("System wake - staying on break")
            return
        if pre_sleep_private:
            logger.info("System wake - restoring private time")
            self.sync_engine.resume()
            self.sync_engine.set_private_mode(True)
            self.tray.set_state(TrayState.PRIVATE)
            return
        self.sync_engine.resume()
        self.tray.set_state(TrayState.SYNCING)
        self.reminder_manager.on_tracking_started()
        logger.info("Tracking resumed (system wake)")
        self.coordinator.trigger_sync("wake_sync")

    def on_system_shutdown(self) -> None:
        """Handle system shutdown / restart."""
        logger.info("System shutdown detected - shutting down")
        self._shutdown_fn()

    def on_screen_lock(self) -> None:
        """Handle screen lock - treat as AFK."""
        try:
            from .ui.tray import TrayState
        except ImportError:
            from ui.tray import TrayState

        with self._pause_state_lock:
            self._pre_lock_private = self.sync_engine.is_private
        logger.info("Screen locked - pausing tracking")
        self.coordinator.clear_idle_pause(send_event=True)
        self.sync_engine.pause()
        self.bf.reset_session()
        self.aw.reset_session()
        self.tray.set_state(TrayState.PAUSED, "Screen locked")
        self.reminder_manager.on_tracking_stopped()
        if self.update_handler and not self.coordinator.is_on_break:
            self.update_handler.try_auto_install()

    def on_screen_unlock(self) -> None:
        """Handle screen unlock - resume tracking."""
        try:
            from .ui.tray import TrayState
        except ImportError:
            from ui.tray import TrayState

        try:
            from .main import _day_greeting
        except ImportError:
            from main import _day_greeting

        with self._pause_state_lock:
            user_paused = self._user_paused
            pre_lock_private = self._pre_lock_private
        if user_paused:
            logger.info("Screen unlocked - staying paused (user-initiated pause active)")
            return
        if self.coordinator.is_on_break:
            logger.info("Screen unlocked - staying on break")
            return
        if pre_lock_private:
            logger.info("Screen unlocked - restoring private time")
            self.sync_engine.resume()
            self.sync_engine.set_private_mode(True)
            self.tray.set_state(TrayState.PRIVATE)
            return
        logger.info("Screen unlocked - resuming tracking")
        self.sync_engine.resume()
        self.tray.set_state(TrayState.SYNCING)
        self.reminder_manager.on_tracking_started()
        self.coordinator.trigger_sync("unlock_sync")
        send_notification("Welcome back!", _day_greeting(), sound=False)

    def on_network_change(self, is_online: bool) -> None:
        """Handle network connectivity change."""
        try:
            from .ui.tray import TrayState
        except ImportError:
            from ui.tray import TrayState

        if is_online:
            logger.info("Network back online - triggering sync to flush queue")
            if self.coordinator.paused_by_network:
                with self._pause_state_lock:
                    user_paused = self._user_paused
                if not user_paused and not self.coordinator.is_on_break:
                    self.sync_engine.resume()
                self.coordinator.paused_by_network = False
            self.coordinator.trigger_sync("network_sync")
        else:
            logger.info("Network offline - pausing sync")
            with self._pause_state_lock:
                already_paused = self._user_paused
            if not already_paused:
                self.sync_engine.pause()
            self.coordinator.paused_by_network = True
            self.tray.set_state(TrayState.QUEUED, "Offline")
