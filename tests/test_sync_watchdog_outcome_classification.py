"""Watchdog outcome classification — offline slowness vs. a genuine wedge.

Origin: 2026-07-22, device 18 (Razvan Zerfas, macOS, agent 1.5.105). A cycle ran
150.86s — 0.86s past ``_DO_SYNC_DEADLINE`` — purely because the BetterFlow API
was unreachable from that device for an hour. Nothing was hung (the largest quiet
stretch inside the "hang" was ~23s) and nothing was lost (the three events went
out on the next cycle), yet it reported as ``level=error`` /
``sync-watchdog-timeout`` / "Sync hung", paging humans and drawing the autofix
drafter onto a non-problem.

Design: docs/superpowers/specs/2026-07-22-watchdog-outcome-classification-design.md

The signal is the transient-failure counter incremented at
``BetterFlowClientError.is_transient`` — the codebase's single "network problem
vs. definitive rejection" decision point (``rules/one-rule-one-implementation.md``).
``_do_sync`` snapshots it at cycle start; the watchdog compares against the live
value.

These tests drive the REAL ``SyncCoordinator._do_sync`` (real watchdog timer,
real classifier) with a stubbed transport, and assert on the reports the error
reporter actually captured — never on arguments forwarded between functions
(``rules/test-fixture-discipline.md``).
"""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

from src.config import Config
from src.main import SyncCoordinator
from src.reminders import ReminderManager
from src.sync.bf_client import BetterFlowClient
from src.sync.http_client import (
    BetterFlowAuthError,
    BetterFlowClientError,
    transient_failure_count,
)
from src.sync.sync_engine import SyncEngine
from tests._watchdog_harness import WatchdogSignal

# Shrunk deadline so the watchdog fires inside a test, not in 150s. The
# production value is pinned by tests/test_sync_watchdog_budget.py.
_TEST_DEADLINE = 0.3


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


class _Recorder:
    """Records what was actually captured, instead of asserting on a Mock's args."""

    def __init__(self):
        self.captures = []

    def capture(self, message, **kwargs):
        self.captures.append({"message": message, **kwargs})

    def by_fingerprint(self, fingerprint):
        return [c for c in self.captures if c.get("fingerprint") == fingerprint]

    def by_level(self, level):
        return [c for c in self.captures if c.get("level") == level]


def _unreachable_client() -> BetterFlowClient:
    """A real BetterFlowClient whose transport always fails the way an offline
    device's does: a connection error, raised WITHOUT a status_code."""
    client = BetterFlowClient(api_url="https://example.invalid/api", token="t", device_id="d")
    client._request = Mock(
        side_effect=BetterFlowClientError("Cannot connect to BetterFlow API")
    )
    return client


def _rejecting_client() -> BetterFlowClient:
    """A real BetterFlowClient whose transport returns a definitive 4xx."""
    client = BetterFlowClient(api_url="https://example.invalid/api", token="t", device_id="d")
    client._request = Mock(
        side_effect=BetterFlowClientError("Invalid payload", status_code=400)
    )
    return client


def _one_event():
    return [{"timestamp": "2026-07-22T12:00:00Z", "duration": 1, "bucket_id": "b", "data": {}}]


class _CoordinatorHarness:
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
        # Instance override only — the class constant stays 150s.
        self.coord._DO_SYNC_DEADLINE = _TEST_DEADLINE

    def _run_overrunning_cycle(self, before_overrun=None):
        """Drive one real _do_sync whose body outlives the watchdog deadline.

        The body waits for the watchdog Timer to have FIRED rather than
        sleeping a fixed 1.2s and assuming it did. A wall-clock margin is a bet
        on the slowest machine that will ever run the suite; overshoot it here
        and the cycle finishes before the Timer runs, so no report is captured
        at all and every assertion below fails as if the classifier were broken.
        """
        fired = WatchdogSignal(self.coord.error_reporter)

        def _slow_sync(*_args, **_kwargs):
            if before_overrun is not None:
                before_overrun()
            fired.wait()
            return _ok_stats()

        self.sync_engine.sync.side_effect = _slow_sync
        self.coord._do_sync()


class TestWatchdogOffline(_CoordinatorHarness):
    """Case 1 — an overrun with transient failures is offline slowness, not a hang."""

    def test_overrun_with_transient_failures_reports_warning(self):
        def fail_transiently():
            # The real classifier runs here: send_events catches the transport
            # error and consults BetterFlowClientError.is_transient.
            result = _unreachable_client().send_events(_one_event())
            assert result.transient is True

        self._run_overrunning_cycle(before_overrun=fail_transiently)

        offline = self.recorder.by_fingerprint("sync-watchdog-timeout-offline")
        assert len(offline) == 1, self.recorder.captures
        assert offline[0]["level"] == "warning"
        assert self.recorder.by_fingerprint("sync-watchdog-timeout") == []
        assert self.recorder.by_level("error") == []
        message = offline[0]["message"]
        assert str(_TEST_DEADLINE) in message
        assert "1 transient failure" in message

    def test_offline_branch_still_resets_both_sessions(self):
        # Unchanged by design: discarding pooled connections after a network
        # outage is the correct recovery and was the watchdog's original purpose.
        def fail_transiently():
            _unreachable_client().send_events(_one_event())

        self._run_overrunning_cycle(before_overrun=fail_transiently)

        assert self.bf.reset_session.called
        assert self.aw.reset_session.called


class TestWatchdogGenuineHang(_CoordinatorHarness):
    """Case 2 — an overrun with NO transient failures still pages as a hang.

    REQUIRED by the design: this is the assertion that catches the rule being
    implemented backwards (which would leave a real wedge reported as a warning
    for 270s, until _SYNC_WEDGE_CEILING pages it at 420s).
    """

    def test_overrun_without_transient_failures_reports_error(self):
        self._run_overrunning_cycle()

        hung = self.recorder.by_fingerprint("sync-watchdog-timeout")
        assert len(hung) == 1, self.recorder.captures
        assert hung[0]["level"] == "error"
        assert "Sync hung" in hung[0]["message"]
        assert self.recorder.by_fingerprint("sync-watchdog-timeout-offline") == []

    def test_hang_branch_still_resets_both_sessions(self):
        self._run_overrunning_cycle()

        assert self.bf.reset_session.called
        assert self.aw.reset_session.called

    def test_zombie_cycles_failures_do_not_downgrade_this_cycle(self):
        """A failure raised by an ABANDONED cycle must not classify this one.

        _acquire_sync_slot deliberately abandons a cycle that has held the lock
        past _SYNC_WEDGE_CEILING and re-arms a fresh lock, and documents that the
        stuck thread cannot be killed. So two _do_sync bodies can be live at once:
        the zombie cycle A keeps making requests while cycle B runs. A single
        process-wide counter has no cycle identity, so A's late transient failure
        would classify B as "the API was unreachable" — masking a genuine wedge
        with a warning, on the strength of a failure that was not B's.

        Attribution is per-thread: A's work runs on A's thread, B's on B's.
        """

        def zombie_failure():
            # Another thread — as the abandoned cycle's zombie would be —
            # experiences a transient failure while THIS cycle is running.
            other = threading.Thread(
                target=lambda: BetterFlowClientError("Cannot connect to BetterFlow API")
            )
            other.start()
            other.join()

        self._run_overrunning_cycle(before_overrun=zombie_failure)

        hung = self.recorder.by_fingerprint("sync-watchdog-timeout")
        assert len(hung) == 1, self.recorder.captures
        assert hung[0]["level"] == "error"
        assert self.recorder.by_fingerprint("sync-watchdog-timeout-offline") == []


class TestTransientFailureCounter:
    """Case 3 — the counter moves on a transient failure and not on a 4xx."""

    def test_counter_counts_failures_not_property_reads(self):
        """The count must be a function of how many transient failures OCCURRED,
        not how many times ``is_transient`` was READ.

        Counting inside the property getter inflates the moment anyone logs
        ``e.is_transient``, asserts on it in a test, or reads it twice in one
        path — and inflation is the fail-open direction here: it makes a real
        hang report as an outage warning, which is precisely the signal this
        feature exists to get right. So the increment belongs at construction and
        ``is_transient`` stays a pure query.
        """
        error = BetterFlowClientError("Cannot connect to BetterFlow API")
        before = transient_failure_count()

        assert error.is_transient is True
        assert error.is_transient is True
        assert error.is_transient is True

        assert transient_failure_count() == before

    def test_one_failure_object_counts_exactly_once(self):
        before = transient_failure_count()
        BetterFlowClientError("Cannot connect to BetterFlow API")
        assert transient_failure_count() == before + 1

    def test_counter_increments_on_transient_failure(self):
        before = transient_failure_count()
        result = _unreachable_client().send_events(_one_event())
        assert result.transient is True
        assert transient_failure_count() == before + 1

    def test_auth_failure_does_not_move_the_counter(self):
        """A 401/403 is a definitive rejection — the fix is re-authentication, not
        waiting for a network to come back. Counting it would report an expired
        token as "the API was unreachable", hiding the actionable condition.

        Both live call sites construct BetterFlowAuthError with no status_code,
        so is_transient is True and only the COUNTER excludes it.
        """
        before = transient_failure_count()
        BetterFlowAuthError("Invalid or expired API token")
        assert transient_failure_count() == before

    def test_auth_error_is_still_transient_for_the_queue(self):
        """The counter and is_transient deliberately DISAGREE on auth errors, and
        this pins the half that must not move.

        is_transient is load-bearing for offline-queue durability: a transient
        failure HOLDS events without burning a retry. Flipping auth errors to
        definitive would burn retries and, past the threshold, drop real queued
        activity — the 2026-06-30 data-loss class (up to ~18h of spans). The
        watchdog's classification must never buy accuracy with that.
        """
        assert BetterFlowAuthError("Invalid or expired API token").is_transient is True
        assert BetterFlowAuthError("Device not authorized").is_transient is True

    def test_counter_does_not_move_on_definitive_4xx(self):
        before = transient_failure_count()
        result = _rejecting_client().send_events(_one_event())
        assert result.transient is False
        assert transient_failure_count() == before


class TestWatchdogNotFired(_CoordinatorHarness):
    """A cycle that finishes inside the deadline reports nothing either way."""

    def test_fast_cycle_captures_nothing(self):
        def fail_transiently(*_args, **_kwargs):
            _unreachable_client().send_events(_one_event())
            return _ok_stats()

        self.sync_engine.sync.side_effect = fail_transiently
        self.coord._do_sync()

        assert self.recorder.captures == []
