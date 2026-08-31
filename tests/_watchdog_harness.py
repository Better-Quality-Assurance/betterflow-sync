"""Shared coordinator setup for watchdog phase/duration tests.

Extracted so Task 2's and Task 3's test files don't duplicate the same
~40-line SyncCoordinator wiring — see task-2-brief.md's amendment.
"""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

from src.config import Config
from src.main import SyncCoordinator
from src.reminders import ReminderManager
from src.sync.sync_engine import SyncEngine


def _ok_stats():
    return SimpleNamespace(
        success=True,
        events_sent=0,
        events_queued=0,
        events_filtered=0,
        gaps_filled=0,
        errors=[],
        aw_bucket_fetch_failed=False,
        # Real SyncStats (sync/sync_engine.py) defaults this False. Missing
        # here made _select_tray_state raise AttributeError for any test that
        # runs a cycle to natural completion rather than just past the
        # watchdog firing — masked previously because nothing read phase.name
        # past that exception until the cycle-end outcome report did.
        capture_suppressed=False,
    )


class _Recorder:
    def __init__(self):
        self.captures = []

    def capture(self, message, **kwargs):
        self.captures.append({"message": message, **kwargs})

    def by_fingerprint(self, fingerprint):
        return [c for c in self.captures if c.get("fingerprint") == fingerprint]


# Generous by design. Every wait on a WatchdogSignal is a wait on a CONDITION,
# so this bound is only reached when the Timer genuinely never ran — a real
# failure. A slow machine waits longer; it does not go red.
WATCHDOG_WAIT_SECONDS = 30.0

# Both branches of _watchdog() (main.py): a cycle with transient API failures
# reports "-offline", one without reports the bare fingerprint. Either means
# the Timer ran and phase.at_deadline is stamped.
_FIRE_TIME_FINGERPRINTS = frozenset(
    {"sync-watchdog-timeout", "sync-watchdog-timeout-offline"}
)


class WatchdogSignal:
    """Set the moment the watchdog Timer emits its fire-time report.

    Lets a cycle wait for the Timer to have ACTUALLY fired instead of sleeping
    a wall-clock margin and hoping. The margin was the last timing bet left in
    these files: overshoot it and phase_at_deadline is 'unknown', which reads
    as a product defect rather than as the scheduling artifact it is.

    Waiting on the capture is sound because _watchdog() stamps
    ``phase.at_deadline = phase.name`` BEFORE it captures (main.py) — so by the
    time this Event is set, everything the assertions read is in place.

    Installed as an instance-level wrapper around the reporter's own capture,
    so the recorder underneath still records exactly what it recorded before.
    """

    def __init__(self, reporter):
        self._event = threading.Event()
        self._inner = reporter.capture
        reporter.capture = self._capture

    def _capture(self, message, **kwargs):
        try:
            return self._inner(message, **kwargs)
        finally:
            if kwargs.get("fingerprint") in _FIRE_TIME_FINGERPRINTS:
                self._event.set()

    def rearm(self):
        """Forget a previous cycle's firing, for a test that runs two."""
        self._event.clear()

    def wait(self, timeout=WATCHDOG_WAIT_SECONDS):
        return self._event.wait(timeout)


class CoordinatorHarness:
    """Base class wiring a SyncCoordinator with mocked collaborators.

    Subclass and use self.coord / self.recorder / self.sync_engine etc.
    TEST_DEADLINE overrides the watchdog deadline used for the cycle under
    test; override it on a subclass if a different value is needed.
    """

    TEST_DEADLINE = 0.3

    def setup_method(self):
        self.config = Config()
        self.aw = Mock()
        self.aw.is_running.return_value = True
        self.bf = Mock()
        self.queue = Mock()
        self.queue.get_checkpoint.return_value = None
        self.queue.size.return_value = 0
        self.queue.is_near_capacity.return_value = False
        self.sync_engine = Mock(spec=SyncEngine)
        self.sync_engine.is_paused = False
        self.sync_engine.is_private = False
        self.sync_engine.sync.return_value = _ok_stats()
        self.tray = Mock()
        self.tray.model = Mock()
        self.tray.model.lock = threading.RLock()
        self.aw_manager = Mock()
        self.aw_manager.is_managing = False
        self.reminder = Mock(spec=ReminderManager)
        self.coord = SyncCoordinator(
            config=self.config,
            aw=self.aw,
            bf=self.bf,
            queue=self.queue,
            sync_engine=self.sync_engine,
            tray=self.tray,
            aw_manager=self.aw_manager,
            reminder_manager=self.reminder,
        )
        self.coord.scheduler = Mock()
        self.coord.scheduler.running = True
        self.recorder = _Recorder()
        self.coord.error_reporter = self.recorder
        self.coord._fetch_hours_today = Mock(return_value="1:00")
        self.coord._DO_SYNC_DEADLINE = self.TEST_DEADLINE
        self._watchdog_signal = None

    def watchdog_signal(self):
        """A WatchdogSignal over whatever reporter is installed right now.

        Created lazily, so a subclass that swaps in its own reporter after
        setup_method (TestEveryOverrunIsCounted) still gets wrapped correctly.
        """
        if self._watchdog_signal is None:
            self._watchdog_signal = WatchdogSignal(self.coord.error_reporter)
        return self._watchdog_signal
