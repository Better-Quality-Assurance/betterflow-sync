"""Tests for sync-wedge self-recovery (A) and sync-staleness telemetry (B).

Origin: Cristian Dragota / sync:67a77a43-787, 2026-06-25 — the agent heartbeat
stayed alive (last_seen fresh) while window/app uploads were frozen for ~40 min
and only a restart cleared it. Root cause: a _do_sync that hangs past the
watchdog holds the non-blocking _sync_lock forever, so every later cycle skips
("Sync already in progress"). The watchdog only resets HTTP sessions — it can't
release the lock or unblock the zombie thread, so the wedge never self-heals.

A: after a hard ceiling, a new cycle abandons the stuck holder and re-arms a
   fresh lock so syncing resumes.
B: the heartbeat carries seconds-since-last-successful-sync so the fleet can flag
   "alive but sync stale" directly instead of inferring it from upload gaps.

Each test fails against pre-fix code (the new attrs/behaviour don't exist) and
passes after the fix lands.
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from src.config import Config
from src.main import SyncCoordinator
from src.reminders import ReminderManager
from src.sync.bf_client import BetterFlowClient
from src.sync.sync_engine import SyncEngine


def _ok_stats():
    """A SyncStats-shaped object that drives _do_sync down its success path."""
    return SimpleNamespace(
        success=True,
        events_sent=0,
        events_queued=0,
        events_filtered=0,
        gaps_filled=0,
        errors=[],
        aw_bucket_fetch_failed=False,
    )


class TestSyncWedgeRecovery:
    """A — a wedged _do_sync no longer freezes sync forever."""

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
        self.coord.error_reporter = Mock()
        # keep _do_sync's tail off the network
        self.coord._fetch_hours_today = Mock(return_value="1:00")

    def test_wedged_lock_is_taken_over_and_sync_resumes(self):
        # Simulate a zombie cycle that grabbed the lock and never let go.
        old = self.coord._sync_lock
        old.acquire()
        self.coord._sync_started_at = time.monotonic() - (
            self.coord._SYNC_WEDGE_CEILING + 30
        )
        self.coord._sync_holder = old

        self.coord._do_sync()

        # The new cycle ran the real body despite the held lock.
        self.sync_engine.sync.assert_called_once()
        # The lock was re-armed (a different object) and the new cycle released it.
        assert self.coord._sync_lock is not old
        assert not self.coord._sync_lock.locked()
        # The wedge was escalated for fleet visibility.
        self.coord.error_reporter.capture.assert_called_once()
        assert (
            self.coord.error_reporter.capture.call_args.kwargs.get("fingerprint")
            == "sync-wedged"
        )

        old.release()  # release the simulated zombie's (now-orphaned) lock

    def test_healthy_in_progress_cycle_is_not_taken_over(self):
        # A cycle that just started must NOT be abandoned — only a wedged one.
        old = self.coord._sync_lock
        old.acquire()
        self.coord._sync_started_at = time.monotonic()
        self.coord._sync_holder = old

        self.coord._do_sync()

        self.sync_engine.sync.assert_not_called()
        assert self.coord._sync_lock is old  # not swapped
        self.coord.error_reporter.capture.assert_not_called()

        old.release()

    def test_successful_sync_records_last_successful_timestamp(self):
        assert self.coord._last_successful_sync is None
        self.coord._do_sync()
        assert self.coord._last_successful_sync is not None


class TestSyncStalenessTelemetry:
    """B — the heartbeat carries seconds-since-last-successful-sync."""

    def setup_method(self):
        self.config = Config()
        self.sync_engine = Mock(spec=SyncEngine)
        self.coord = SyncCoordinator(
            config=self.config,
            aw=Mock(),
            bf=Mock(),
            queue=Mock(),
            sync_engine=self.sync_engine,
            tray=Mock(),
            aw_manager=Mock(),
            reminder_manager=Mock(spec=ReminderManager),
        )
        self.coord.aw_manager.health_snapshot.return_value = {}

    def test_health_telemetry_includes_sync_staleness(self):
        self.coord._last_successful_sync = time.monotonic() - 42
        tele = self.coord._build_health_telemetry()
        assert 40 <= tele["sync_stale_seconds"] <= 70

    def test_health_telemetry_omits_staleness_before_first_sync(self):
        self.coord._last_successful_sync = None
        tele = self.coord._build_health_telemetry()
        assert "sync_stale_seconds" not in tele


def test_heartbeat_forwards_sync_stale_seconds():
    bf = BetterFlowClient(api_url="https://app.betterflow.eu/api/agent")
    bf._request = Mock(return_value={})

    bf.heartbeat(health={"sync_stale_seconds": 42})

    sent = bf._request.call_args.kwargs["data"]
    assert sent["sync_stale_seconds"] == 42
