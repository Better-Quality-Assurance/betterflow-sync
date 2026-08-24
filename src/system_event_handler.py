"""System event handlers: sleep/wake, screen lock/unlock, network changes."""

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

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
        # Timestamp set when on_system_sleep fires; consumed on the next
        # on_system_wake to emit a sleep_time event covering the span.
        # Without this the overnight gap shows as "Break" in the daily
        # activity view (server-side aggregator can't tell idle from sleep).
        self._sleep_start: Optional[datetime] = None

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
            # Branch on a local, not a re-read of the shared field: the sleep and
            # screen-lock listeners are separate OS notification threads, and a
            # lock event interleaving here would make the unlocked read below see
            # a stale value, skip ending Private Time at the sleep boundary, and
            # silently mark post-wake work private and uncounted — the exact
            # regression the comment below describes. on_screen_unlock already
            # captures its counterpart this way.
            pre_sleep_private = self._pre_sleep_private
            # Keep the EARLIEST sleep start across a sequence of sleep events
            # without an intervening wake (macOS can fire Display Sleep then
            # System Sleep separately; some lid-close → reopen → reclose
            # paths emit two sleeps without a wake between them). Overwriting
            # would silently truncate the front of the sleep span.
            if self._sleep_start is None:
                self._sleep_start = datetime.now(timezone.utc)
            else:
                logger.debug(
                    "on_system_sleep fired while a prior _sleep_start is still pending "
                    "(no wake yet) — keeping the earlier timestamp"
                )
        # End Private Time at the sleep boundary. Private has no auto-timeout,
        # so a user who enables it and forgets — or whose machine sleeps mid-
        # private — would otherwise stay private across the sleep AND into the
        # next awake session, silently marking real post-wake work as private
        # and uncounted (Raluca, 2026-06-24: a ~20-min private toggle stayed on
        # ~11h overnight and swallowed her evening). Leaving it here records the
        # true enable→sleep span via the normal leave path (private_time event +
        # checkpoint advance); on wake we resume NORMAL tracking, never auto-
        # restoring private.
        if pre_sleep_private:
            try:
                self.sync_engine.set_private_mode(False)
            except Exception as e:
                logger.warning("ending private mode on sleep failed: %s", e)
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

        # Anchor idle detection to this wake instant BEFORE anything else
        # runs. bf-idle-tracker resumes heartbeating its pre-suspend 'afk'
        # event on wake without resetting its start time, so the next
        # check_idle_status() would otherwise backdate idle_start to the
        # last keystroke before the lid closed and re-carve real work time
        # across the suspend. See IdleManager.record_wake.
        self.coordinator.record_wake()
        self.bf.reset_session()
        self.aw.reset_session()
        # Emit the sleep_time event before any early-return paths so even
        # a wake into still-paused / still-on-break states records the
        # span. Captured under the lock to avoid racing a second sleep.
        with self._pause_state_lock:
            user_paused = self._user_paused
            sleep_start = self._sleep_start
            self._sleep_start = None
        if sleep_start is not None:
            try:
                self.sync_engine.send_sleep_event(sleep_start)
            except Exception as e:
                logger.warning("send_sleep_event failed: %s", e)
        if user_paused:
            logger.info("System wake - staying paused (user-initiated pause active)")
            # Re-assert PAUSED with no text. The state is unchanged, so this is
            # a no-op for the icon — but the tray now RENDERS status_text, and
            # the sentence still sitting there is "Sleeping", written on the way
            # down. Returning without this leaves an awake laptop reporting that
            # it is asleep. The cause is now the user's pause, which has no
            # sentence of its own: the generic "Paused" is the true answer.
            self.tray.set_state(TrayState.PAUSED)
            return
        if self.coordinator.is_on_break:
            logger.info("System wake - staying on break")
            # Second reason to stay paused, same stale sentence as the branch
            # above. PAUSED is re-asserted rather than ON_BREAK because the
            # state is not this handler's to change — it only has to stop the
            # tray claiming a cause that has ended.
            self.tray.set_state(TrayState.PAUSED)
            return
        # Private Time is intentionally NOT auto-restored: it was ended at the
        # sleep boundary (on_system_sleep), so a sleep cleanly ends a private
        # session. The user re-enables it if they still want privacy — a
        # forgotten toggle can no longer silently swallow post-wake work.
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
            # Same as the wake path above: "Screen locked" is still stored and
            # now renders, so an unlocked screen would keep reporting itself
            # locked.
            self.tray.set_state(TrayState.PAUSED)
            return
        if self.coordinator.is_on_break:
            logger.info("Screen unlocked - staying on break")
            # Second reason to stay paused, same stale sentence as the branch
            # above. PAUSED is re-asserted rather than ON_BREAK because the
            # state is not this handler's to change — it only has to stop the
            # tray claiming a cause that has ended.
            self.tray.set_state(TrayState.PAUSED)
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

        # A network outage suspends UPLOAD, never capture.
        #
        # This used to call sync_engine.pause(), which means "this window must
        # never be recorded" and enforces that by advancing every checkpoint
        # past it. The work was therefore deleted rather than queued: the events
        # were never fetched, so the offline queue never saw them, which is why
        # outages that lost real time still reported "0 queued". Measured on one
        # machine in one week: 38 network-offline windows, 9 of them longer than
        # the 2-minute lookback that accidentally rescued the rest.
        if is_online:
            logger.info("Network back online - triggering sync to flush queue")
            # Resume unconditionally, NOT under `if paused_by_network`: that flag
            # is cleared by other paths that can run mid-outage (on_system_sleep,
            # and a manual pause via _set_user_paused), so gating on it leaves
            # _upload_suspended latched on for the rest of the process with
            # nothing left to clear it. resume_upload is idempotent — it only
            # logs on a real transition.
            self.sync_engine.resume_upload("network online")
            self.coordinator.paused_by_network = False
            self.coordinator.trigger_sync("network_sync")
        else:
            logger.info("Network offline - suspending upload (capture continues)")
            self.sync_engine.suspend_upload("network offline")
            self.coordinator.paused_by_network = True
            # No text: "Offline" is STATUS_TEXT_STATES[QUEUED] and passing it
            # here would be a second copy of the label to keep in step.
            self.tray.set_state(TrayState.QUEUED)
