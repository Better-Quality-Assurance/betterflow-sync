"""The overrun report says how long EACH phase took, not only which one was
running when the deadline passed.

Why: phase_at_deadline is the stage running at the instant the 150s Timer
fired. For a cycle that tips over the line by a few seconds, that instant lands
in whatever stage FOLLOWS the slow one. The 2026-11-17 board row read "Sync
overran the 150s deadline - finished at 153.6s in phase 'post_sync'", which
points at post_sync: a stage that, on the success path, does no network I/O at
all. The ~150s were spent earlier (almost certainly in sync(), where the retry
chain and send budget live), and the report could not say so.

So the cycle's phase box also accumulates the time spent in each phase, and the
outcome report carries the full breakdown in context and names the slowest
phase in the message (the digest reads message + count only).

The phase clock is its own seam (`SyncCoordinator._phase_clock`), separate from
the two-read cycle-duration seam `_monotonic`, whose read count other tests pin.
"""

from src.main import _CyclePhase
from src.ui.tray import TrayState

from ._watchdog_harness import CoordinatorHarness, _ok_stats


class _StepClock:
    """A clock that only moves when the test says so."""

    def __init__(self, start=1_000_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestCyclePhaseTimings:
    def test_each_phase_accumulates_the_time_spent_in_it(self):
        clock = _StepClock()
        phase = _CyclePhase(clock=clock)
        clock.advance(0.5)
        phase.name = "capture_health"
        clock.advance(2.0)
        phase.name = "sync"
        clock.advance(148.9)
        phase.name = "post_sync"
        clock.advance(4.0)

        assert phase.timings() == {
            "startup": 0.5,
            "capture_health": 2.0,
            "sync": 148.9,
            "post_sync": 4.0,
        }

    def test_re_entering_a_phase_adds_to_its_total(self):
        clock = _StepClock()
        phase = _CyclePhase(clock=clock)
        phase.name = "sync"
        clock.advance(10.0)
        phase.name = "post_sync"
        clock.advance(1.0)
        phase.name = "sync"
        clock.advance(5.0)

        assert phase.timings()["sync"] == 15.0
        assert phase.timings()["post_sync"] == 1.0

    def test_timings_can_be_read_twice_without_double_counting(self):
        clock = _StepClock()
        phase = _CyclePhase(clock=clock)
        phase.name = "sync"
        clock.advance(3.0)
        first = phase.timings()
        second = phase.timings()
        assert first == second == {"startup": 0.0, "sync": 3.0}

    def test_name_still_reads_back_as_assigned(self):
        phase = _CyclePhase(clock=_StepClock())
        assert phase.name == "startup"
        phase.name = "hours_fetch"
        assert phase.name == "hours_fetch"


class TestOverrunReportNamesTheSlowestPhase(CoordinatorHarness):
    """The 2026-11-17 shape: sync() eats almost the whole budget, the deadline
    passes while post_sync is running, the cycle finishes a moment later."""

    def test_overrun_in_post_sync_after_a_slow_sync_blames_sync(self):
        clock = _StepClock()
        self.coord._phase_clock = clock
        # The cycle DURATION is scripted too (two reads: start, end), as in
        # tests/test_watchdog_overrun_outcome.py. On the real clock the Timer
        # can fire with measured elapsed a hair under the deadline (Windows
        # monotonic resolution is ~15ms), and then no outcome report is sent.
        cycle_reads = iter([2_000_000_000.0, 2_000_000_000.0 + 0.4])
        self.coord._monotonic = lambda: next(cycle_reads)
        fired = self.watchdog_signal()

        def _slow_sync(*_a, **_k):
            clock.advance(148.9)
            return _ok_stats()

        def _post_sync_tray_choice(*_a, **_k):
            # The real Timer fires while we are in post_sync.
            fired.wait()
            clock.advance(4.0)
            return TrayState.SYNCING, None

        self.sync_engine.sync.side_effect = _slow_sync
        self.coord._select_tray_state = _post_sync_tray_choice
        self.coord._do_sync()

        outcome = [
            c
            for c in self.recorder.captures
            if str(c.get("fingerprint", "")).startswith("sync-watchdog-overrun")
        ]
        assert len(outcome) == 1, self.recorder.captures
        got = outcome[0]
        # The instant-of-breach phase is unchanged and still reported...
        assert got["context"]["phase_at_deadline"] == "post_sync"
        # ...but the breakdown shows where the time actually went.
        assert got["context"]["phase_seconds"]["sync"] == 148.9
        assert got["context"]["phase_seconds"]["post_sync"] == 4.0
        assert got["context"]["slowest_phase"] == "sync"
        assert "slowest phase 'sync' (148.9s)" in got["message"], got["message"]
