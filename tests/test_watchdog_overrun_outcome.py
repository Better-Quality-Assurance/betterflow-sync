"""How long an overrunning cycle actually ran — issue #179.

The watchdog fires AT the deadline, so elapsed measured inside it is always
~150s no matter what the cycle goes on to do. The real duration is knowable
only at cycle end, which is where this report is emitted from.

The predicate is `elapsed >= deadline`, deliberately NOT "did the watchdog
fire": deriving it from elapsed avoids racing the in-flight Timer thread, and
means these tests never need the timer to fire at all.
"""

import time

import src.main as main_module
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
        # Tight band tied to the configured sleep, not a wide "any plausible
        # constant" range — see test_the_elapsed_time_tracks_the_actual_sleep
        # below for the mutant-killing version of this same field.
        assert abs(got["context"]["elapsed_seconds"] - 0.45) < 0.15
        assert "0.4" in got["message"] or "0.5" in got["message"]

    def test_the_elapsed_time_tracks_the_actual_sleep_not_a_constant(self):
        """A hardcoded literal (e.g. 0.5) would satisfy a wide band like
        `0.45 <= x < 5.0` for every fixture in this file — they all sleep
        somewhere in that range. Run two cycles with distinctly different
        sleep durations and require elapsed_seconds to track EACH one within
        a tight tolerance; no single constant can satisfy both, and the two
        reported values must move apart by roughly the gap between the
        sleeps."""
        self.run_cycle_taking(0.45)
        short = self.recorder.by_fingerprint(MODERATE)[0]["context"]["elapsed_seconds"]
        assert abs(short - 0.45) < 0.15

        self.recorder.captures.clear()
        self.run_cycle_taking(0.75)
        long = self.recorder.by_fingerprint(SEVERE)[0]["context"]["elapsed_seconds"]
        assert abs(long - 0.75) < 0.15

        assert long - short > 0.2

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

    def test_an_aw_bucket_failure_exits_at_post_sync_not_hours_fetch(self):
        """The discriminating fixture for phase_at_exit. Every other test in
        this file completes normally and exits at 'hours_fetch' — a mutant
        hardcoding "phase_at_exit": "hours_fetch" is invisible to all of them
        (the same agreement-region shape as Mutant C / the capture_health
        test above, this time on the exit field). aw_bucket_fetch_failed
        returns _do_sync right after the post_sync stamp, before hours_fetch
        is ever reached."""
        def _slow_failed_sync(*_a, **_k):
            time.sleep(0.45)
            stats = _ok_stats()
            stats.aw_bucket_fetch_failed = True
            return stats

        self.sync_engine.sync.side_effect = _slow_failed_sync
        self.coord._do_sync()

        got = self.recorder.by_fingerprint(MODERATE)[0]
        assert got["context"]["phase_at_deadline"] == "sync"
        assert got["context"]["phase_at_exit"] == "post_sync"

    def test_when_the_timer_never_fires_the_deadline_phase_is_unknown(self, monkeypatch):
        """The outcome predicate is gated on elapsed, not "did the watchdog
        fire" — so it can fire in the narrow window where elapsed has crossed
        the deadline but the Timer thread has not run yet. phase_at_deadline
        must stay honestly 'unknown' there, never silently fall back to
        phase_at_exit — that fallback is exactly the pre-fix behaviour this
        change removed (report whichever stage ran last). Stub
        threading.Timer to a no-op so _watchdog() genuinely never runs,
        forcing phase.at_deadline to stay None for the whole cycle."""

        class _NoOpTimer:
            def __init__(self, *_a, **_k):
                pass

            def start(self):
                pass

            def cancel(self):
                pass

        monkeypatch.setattr(main_module.threading, "Timer", _NoOpTimer)
        self.run_cycle_taking(0.45)

        got = self.recorder.by_fingerprint(MODERATE)[0]
        assert got["context"]["phase_at_deadline"] == "unknown"
        assert got["context"]["phase_at_deadline"] != got["context"]["phase_at_exit"]


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


class TestOutcomeReportFailureIsContained(_Harness):
    def test_a_failed_outcome_report_does_not_skip_the_heartbeat(self):
        """capture() documents "never raises", but its final
        threading.Thread(...).start() is unguarded and can raise
        RuntimeError on a resource-starved machine — exactly the machine
        that overruns its deadline. Unguarded, that would propagate out of
        _do_sync's finally block and skip send_heartbeat_if_due right after
        it: a struggling device would also stop reporting that it is
        alive.

        Fails only the NEW outcome-report capture, not the pre-existing
        fire-time capture (fired from the Timer thread, unguarded, out of
        scope for this fix) — a blanket failing mock would raise there too
        and turn this into an unrelated background-thread exception instead
        of a clean assertion on the guard actually being tested."""
        real_capture = self.recorder.capture

        def _fail_only_the_outcome_report(message, **kwargs):
            if kwargs.get("fingerprint") in ALL_BANDS:
                raise RuntimeError("can't start new thread")
            return real_capture(message, **kwargs)

        self.recorder.capture = _fail_only_the_outcome_report

        self.run_cycle_taking(0.45)  # must not raise

        assert self.sync_engine.send_heartbeat_if_due.called
