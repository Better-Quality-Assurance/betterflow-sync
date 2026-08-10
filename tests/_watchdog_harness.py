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
    )


class _Recorder:
    def __init__(self):
        self.captures = []

    def capture(self, message, **kwargs):
        self.captures.append({"message": message, **kwargs})

    def by_fingerprint(self, fingerprint):
        return [c for c in self.captures if c.get("fingerprint") == fingerprint]


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
