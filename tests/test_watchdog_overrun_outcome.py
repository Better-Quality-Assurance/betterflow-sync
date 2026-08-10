"""How long an overrunning cycle actually ran — issue #179.

The watchdog fires AT the deadline, so elapsed measured inside it is always
~150s no matter what the cycle goes on to do. The real duration is knowable
only at cycle end, which is where this report is emitted from.

The predicate is `elapsed >= deadline`, deliberately NOT "did the watchdog
fire": deriving it from elapsed avoids racing the in-flight Timer thread, and
means these tests never need the timer to fire at all.
"""

import time

from tests._watchdog_harness import CoordinatorHarness, _ok_stats

MARGINAL = "sync-watchdog-overrun-marginal"
MODERATE = "sync-watchdog-overrun-moderate"
SEVERE = "sync-watchdog-overrun-severe"
ALL_BANDS = (MARGINAL, MODERATE, SEVERE)


def _outcome_captures(recorder):
    return [c for c in recorder.captures if c.get("fingerprint") in ALL_BANDS]


class _Harness(CoordinatorHarness):
    def run_cycle_taking(self, seconds):
        def _slow_sync(*_a, **_k):
            time.sleep(seconds)
            return _ok_stats()

        self.sync_engine.sync.side_effect = _slow_sync
        self.coord._do_sync()


class TestOverrunIsMeasured(_Harness):
    def test_a_marginal_overrun_reports_the_marginal_band(self):
        self.run_cycle_taking(0.33)  # ~1.1x of 0.3

        got = self.recorder.by_fingerprint(MARGINAL)
        assert len(got) == 1, self.recorder.captures
        assert got[0]["level"] == "warning"
        assert got[0]["tags"] == {"component": "sync-watchdog"}

    def test_a_moderate_overrun_reports_the_moderate_band(self):
        self.run_cycle_taking(0.45)  # 1.5x

        assert len(self.recorder.by_fingerprint(MODERATE)) == 1, self.recorder.captures
        assert self.recorder.by_fingerprint(MARGINAL) == []

    def test_a_severe_overrun_reports_the_severe_band(self):
        self.run_cycle_taking(0.75)  # 2.5x

        assert len(self.recorder.by_fingerprint(SEVERE)) == 1, self.recorder.captures
        assert self.recorder.by_fingerprint(MODERATE) == []

    def test_the_report_carries_the_elapsed_time_and_the_phase(self):
        self.run_cycle_taking(0.45)

        got = self.recorder.by_fingerprint(MODERATE)[0]
        assert got["context"]["phase_at_deadline"] == "sync"
        assert got["context"]["deadline_seconds"] == self.TEST_DEADLINE
        # A real measurement, not a constant: at least the sleep, and not
        # absurdly more.
        assert 0.45 <= got["context"]["elapsed_seconds"] < 5.0
        assert "0.4" in got["message"] or "0.5" in got["message"]

    def test_the_report_also_carries_the_exit_phase_and_the_two_differ(self):
        """phase_at_deadline and phase_at_exit are genuinely different fields:
        the watchdog fires mid-sync (deadline snapshot: 'sync'), but the cycle
        goes on to complete normally, so the LAST stamp before the cycle ends
        is a later stage. Proving they differ is what stops someone
        'simplifying' the pair back into one field."""
        self.run_cycle_taking(0.45)

        got = self.recorder.by_fingerprint(MODERATE)[0]
        assert got["context"]["phase_at_exit"] == "hours_fetch"
        assert got["context"]["phase_at_deadline"] != got["context"]["phase_at_exit"]

    def test_a_capture_health_overrun_reports_that_phase_at_deadline(self):
        """The discriminating fixture. Every other test in this file overruns
        inside sync(), so phase_at_deadline == 'sync' everywhere else — a
        mutant that hardcodes 'sync' is invisible to all of them (the
        agreement-region trap: test-fixture-discipline.md Phantom 12). This is
        the one fixture where the deadline is breached in a DIFFERENT phase,
        so only this test can tell a real snapshot from a constant."""
        def _slow_health():
            time.sleep(0.45)
            return True

        self.coord._monitor_capture_health = _slow_health
        self.coord._do_sync()

        got = self.recorder.by_fingerprint(MODERATE)[0]
        assert got["context"]["phase_at_deadline"] == "capture_health"
        # It still completes the cycle normally afterwards (sync() is instant
        # here), so the exit phase has moved on — same pair shape as the
        # sync-overrun fixtures above, different deadline phase.
        assert got["context"]["phase_at_exit"] == "hours_fetch"


class TestHealthyCyclesStaySilent(_Harness):
    """THE critical negative. If the predicate is implemented backwards this is
    the only test that catches it, and the failure mode is an outcome report on
    every healthy cycle — flooding the ingest this change exists to quieten."""

    def test_a_cycle_inside_the_deadline_emits_no_outcome_report(self):
        self.run_cycle_taking(0.02)  # far under 0.3

        assert _outcome_captures(self.recorder) == [], self.recorder.captures

    def test_the_negative_above_is_not_passing_vacuously(self):
        """A cycle that never ran would also produce zero outcome captures.
        Prove the subject was reached: the same harness, overrunning, DOES
        produce one."""
        self.run_cycle_taking(0.02)
        assert _outcome_captures(self.recorder) == []

        self.recorder.captures.clear()
        self.run_cycle_taking(0.45)
        assert len(_outcome_captures(self.recorder)) == 1, self.recorder.captures


class TestExistingReportsAreUnchanged(_Harness):
    def test_an_overrun_still_emits_its_fire_time_error(self):
        """The outcome report is additive. The group that already holds 34
        occurrences must keep firing exactly as before."""
        self.run_cycle_taking(0.45)

        hung = self.recorder.by_fingerprint("sync-watchdog-timeout")
        assert len(hung) == 1, self.recorder.captures
        assert hung[0]["level"] == "error"

    def test_the_outcome_report_never_uses_error_level(self):
        """It measures; the fire-time report pages. A second error would double
        the alert volume."""
        self.run_cycle_taking(0.75)

        for capture in _outcome_captures(self.recorder):
            assert capture["level"] == "warning"
