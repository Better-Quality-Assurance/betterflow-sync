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
        # Timestamp set when on_screen_lock fires; consumed on the next
        # on_screen_unlock to emit a lock_time event covering the span (its
        # own bucket_type, NOT sleep_time with a tag — see
        # sync_engine.send_lock_event's docstring for why a shared type can't
        # work here). Before this, a lock called sync_engine.pause() and
        # uploaded NOTHING — credit already stopped, but the server could not
        # tell a lock apart from a crashed or quit agent.
        self._lock_start: Optional[datetime] = None
        # True while a lock span was open when the system fell asleep (closed
        # early at the sleep boundary below, to avoid double-reporting the
        # slept portion once on_system_wake also reports it as sleep_time).
        # on_system_wake reopens a fresh _lock_start when this is set, since
        # macOS does not auto-unlock on wake — the screen is still locked.
        #
        # CRITICAL: an unlock can be processed BEFORE the matching wake — the
        # lock/unlock listener and the sleep/wake listener are separate OS
        # notification threads with independent run loops (the lock listener
        # polls its own loop every 5s), so there is no ordering guarantee
        # between "wake fired" and "unlock fired" when both are pending.
        # on_screen_unlock clears this flag unconditionally for exactly that
        # reason: an unlock means the user is back NOW, regardless of whether
        # the wake handler has run yet, and must not be silently overridden by
        # a wake that processes a few milliseconds later. Verified 2026-10-08
        # (Tudor's repro): without that clear, sleep 07:00 / lock 07:01 /
        # unlock 09:00 / wake 09:00 (processed in THIS order) left the flag
        # set, so wake reopened a lock span at ~09:00 that a later 12:00-12:30
        # lock then kept-earliest into — one five-and-a-half-hour span
        # swallowing 09:00-12:00 of real work that was never locked at all.
        self._locked_while_asleep = False

        # Update handler reference (set after construction)
        self.update_handler = None

    def on_system_sleep(self) -> None:
        """Handle system sleep / lid close."""
        try:
            from .ui.tray import TrayState
        except ImportError:
            from ui.tray import TrayState

        lock_start_to_flush = None
        sleep_boundary = None
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
                sleep_boundary = datetime.now(timezone.utc)
                self._sleep_start = sleep_boundary
                # A lock span already open is about to run through the sleep.
                # Close it HERE, at the sleep boundary, rather than letting it
                # keep growing. This is NOT about double-counting a sum (the
                # billed path merges overlapping intervals, it doesn't sum
                # them) — it's about PRESENCE: internal-tool2's
                # AgentAnalyticsService::bridgeActivityToSleepStarts treats a
                # sleep_time block's START as proof of presence up to that
                # instant and bridges the idle gap before it, and
                # clampIdleTailBeforeSleep does the matching idle-block clamp.
                # lock_time must get NEITHER of those (a lock earns back
                # nothing), so the two bucket types must never represent the
                # same wall-clock instant as if they were one continuous
                # thing — split the lock span here so its END lines up exactly
                # with the sleep span's START instead of overlapping it.
                # Mirrors ending Private Time at this same boundary, just
                # below, for the same reason: a span must not silently run
                # through a sleep it was never measuring.
                if self._lock_start is not None:
                    lock_start_to_flush = self._lock_start
                    self._lock_start = None
                    self._locked_while_asleep = True
            else:
                logger.debug(
                    "on_system_sleep fired while a prior _sleep_start is still pending "
                    "(no wake yet) — keeping the earlier timestamp"
                )
        if lock_start_to_flush is not None:
            try:
                self.sync_engine.send_lock_event(lock_start_to_flush, sleep_boundary)
            except Exception as e:
                logger.warning("send_lock_event (pre-sleep) failed: %s", e)
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
            if self._locked_while_asleep:
                # The lock span open when we fell asleep was already closed
                # and flushed AT the sleep boundary (on_system_sleep). macOS
                # does not auto-unlock on wake, so the screen is still locked
                # right now — open a fresh lock span covering the rest of it;
                # on_screen_unlock flushes it when the user actually returns.
                self._locked_while_asleep = False
                self._lock_start = datetime.now(timezone.utc)
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

    def on_display_sleep(self) -> None:
        """Handle macOS display sleep (NSWorkspaceScreensDidSleepNotification).

        Fires for a genuine inactivity-triggered display-off AND as a side
        effect of a real system sleep (both notifications fire for the
        latter; see system_events.py's docstring on why they are no longer
        deduped against each other). Which of those this instance is cannot
        be told from the notification alone, so it is resolved the same way
        the existing lock/sleep race already is: by LETTING on_system_sleep
        run its own flush logic rather than trying to guess here.

        Gate OFF (``self.sync_engine.config.capabilities.lock_time`` is
        False — the server has not advertised lock_time support, or we
        haven't fetched config yet): delegate straight to on_system_sleep,
        byte-identical to this agent's behaviour before this split existed
        (display-off becomes sleep_time, same as a real sleep, and earns the
        presence bridge the same way — unchanged for old agent builds and
        for days already recorded).

        Gate ON: delegate to on_screen_lock instead — per Tudor's product
        rule (2026-10-09), "the display turning off by itself from
        inactivity is treated the same as a lock". on_screen_lock's existing
        idempotent span-open logic already does the right thing whether or
        not a sleep is also in progress or about to start:
        - if `_sleep_start` is already set (WillSleep got there first), it
          just records `_locked_while_asleep` — no second span opens.
        - otherwise it opens `_lock_start` (or, if one is already open,
          leaves it — the no-overlap rule). If a real sleep follows,
          on_system_sleep's existing boundary-flush closes this span at the
          sleep instant and reopens it fresh on wake, exactly as it already
          does for a literal screen lock that was open when sleep began —
          the contiguous-split rule this method deliberately does not
          reimplement.
        """
        if not self.sync_engine.config.capabilities.lock_time:
            self.on_system_sleep()
            return
        self.on_screen_lock()

    def on_screen_lock(self) -> None:
        """Handle screen lock - treat as AFK."""
        try:
            from .ui.tray import TrayState
        except ImportError:
            from ui.tray import TrayState

        with self._pause_state_lock:
            self._pre_lock_private = self.sync_engine.is_private
            # Record the lock span start for later upload as a lock_time
            # event, UNLESS the system is already asleep: an open sleep span
            # already covers this exclusion, and a lock notification can
            # arrive concurrently with (or just after) the sleep notification
            # on some sleep paths. Opening a second, overlapping span here
            # would claim part of the already-reported sleep span as locked
            # too. Just remember we were locked, so on_system_wake can open a
            # fresh lock span once the sleep span has been flushed.
            if self._sleep_start is not None:
                self._locked_while_asleep = True
            elif self._lock_start is None:
                self._lock_start = datetime.now(timezone.utc)
            else:
                logger.debug(
                    "on_screen_lock fired while a prior _lock_start is still pending "
                    "(no unlock yet) — keeping the earlier timestamp"
                )
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

        # Emit the lock_time event before any early-return paths, mirroring
        # on_system_wake above — so even an unlock into a still-paused /
        # still-on-break state records the span. Captured under the lock to
        # avoid racing a second lock.
        with self._pause_state_lock:
            user_paused = self._user_paused
            pre_lock_private = self._pre_lock_private
            lock_start = self._lock_start
            self._lock_start = None
            # CRITICAL: an unlock is authoritative — it means the lock state
            # is OVER, full stop, even if on_system_wake hasn't run yet (the
            # lock/unlock and sleep/wake listeners are independent OS threads
            # and can be reordered; see the long comment on
            # _locked_while_asleep in __init__). Without this clear, a wake
            # that processes AFTER this unlock would still see the flag set
            # and reopen a bogus lock span starting at the wake instant —
            # covering real work that happened entirely after the user
            # already unlocked.
            self._locked_while_asleep = False
        if lock_start is not None:
            try:
                self.sync_engine.send_lock_event(lock_start)
            except Exception as e:
                logger.warning("send_lock_event failed: %s", e)
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
        # Coalesced (#221): this fires on EVERY unlock -- 18 times in one
        # measured day -- and nothing reads its outcome. A stable key makes
        # macOS replace the previous one instead of stacking another, which
        # matters because the same channel carries the Rosetta and
        # Accessibility notices a user must actually read.
        #
        # Distinct from the once-per-session greeting in main.py, which DOES
        # read the delivery verdict and therefore keeps its unique identifier.
        send_notification(
            "Welcome back!", _day_greeting(), sound=False, coalesce_key="welcome-back"
        )

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
