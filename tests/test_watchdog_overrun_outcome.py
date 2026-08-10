"""How long an overrunning cycle actually ran — issue #179.

The watchdog fires AT the deadline, so elapsed measured inside it is always
~150s no matter what the cycle goes on to do. The real duration is knowable
only at cycle end, which is where this report is emitted from.

The predicate is `elapsed >= deadline`, deliberately NOT "did the watchdog
fire": deriving it from elapsed avoids racing the in-flight Timer thread, and
means most of these tests never need the timer to fire at all.

Duration is SCRIPTED, not slept
-------------------------------
These tests used to choose a band by sleeping inside sync() and letting the
real clock decide which band the cycle landed in. That cannot work at test
scale: the bands are ratios of the deadline, so with TEST_DEADLINE = 0.3 the
marginal band is 0.300-0.360 — sixty milliseconds wide. The macOS CI runner
added 85-150ms on top of each sleep, so the marginal fixture arrived in
MODERATE and the moderate fixture (0.45) arrived at 0.6, exactly the severe
boundary. Both failures killed the v1.5.122 release build.

A previous round answered that by widening the margin from 2.7ms to 45ms and
validating on an idle laptop, which proved nothing about the runner. There is
no sleep duration that fixes this, because any wall-clock margin is a bet on
the slowest machine that will ever run the suite.

So the duration _do_sync MEASURES now comes from a scripted clock
(`_ScriptedClock`, driving `SyncCoordinator._monotonic`) and is exact on any
machine, while the duration the cycle really TAKES is a separate knob used
only by the tests that need the watchdog Timer to have fired. Those get a
generous multiple of the deadline (`_TIMER_SLEEP`, 3x) rather than a marginal
one, because that margin is the only wall-clock dependency left in this file.
"""

import threading
import time

import src.main as main_module
from src.error_reporter import ErrorReporter
from tests._watchdog_harness import CoordinatorHarness, _ok_stats

MARGINAL = "sync-watchdog-overrun-marginal"
MODERATE = "sync-watchdog-overrun-moderate"
SEVERE = "sync-watchdog-overrun-severe"
ALL_BANDS = (MARGINAL, MODERATE, SEVERE)

# Scripted durations, as multiples of CoordinatorHarness.TEST_DEADLINE (0.3).
# Exact boundaries are pinned against the pure function in
# tests/test_watchdog_overrun_bands.py; these only have to sit unambiguously
# inside a band and round to a distinct one-decimal figure in the message.
MARGINAL_ELAPSED = 0.315  # 1.05x -> "0.3"
MODERATE_ELAPSED = 0.42  # 1.40x -> "0.4"
SEVERE_ELAPSED = 0.81  # 2.70x -> "0.8"
HEALTHY_ELAPSED = 0.02  # 0.07x -> no report at all

# How long a cycle really runs when a test needs the Timer to have fired. 3x
# the deadline, so there is 0.6s of slack on a deadline of 0.3 — four times the
# worst overhead the CI runner has been seen to add. Deliberately generous: the
# consequence of it being marginal is a phase_at_deadline of 'unknown', which
# reads as a product defect rather than as the timing artifact it would be.
_TIMER_SLEEP = 0.9


def _outcome_captures(recorder):
    return [c for c in recorder.captures if c.get("fingerprint") in ALL_BANDS]


class _ScriptedClock:
    """Stand-in for time.monotonic on SyncCoordinator._monotonic.

    Returns `base` on the first read of a cycle and `base + elapsed` on the
    second, so the duration _do_sync computes is exactly `elapsed` however slow
    the machine is.

    `base` is deliberately absurd — 1e9 seconds is ~31 years of uptime, and a
    fresh CI runner reports ~95s — so a read that escaped back to the real
    clock cannot coincidentally look like a scripted one. It produces either
    ~+1e9 (loudly wrong figure) or ~-1e9 (below the deadline, so no report at
    all), and `_Harness._drive`'s read-count assertion catches both regardless
    of which direction it lands in.
    """

    def __init__(self, elapsed=0.0, base=1_000_000_000.0):
        self.elapsed = elapsed
        self.base = base
        self.reads = 0

    def __call__(self):
        value = self.base + (self.elapsed if self.reads % 2 else 0.0)
        self.reads += 1
        return value


class _Harness(CoordinatorHarness):
    def setup_method(self):
        super().setup_method()
        self.clock = _ScriptedClock()
        self.coord._monotonic = self.clock

    def _drive(self, reports):
        """Run one cycle whose measured duration is exactly `reports`."""
        self.clock.elapsed = reports
        before = self.clock.reads
        self.coord._do_sync()
        # The partial-injection guard, on every cycle in this file rather than
        # in one test. _do_sync must take BOTH its clock reads from the seam;
        # if either escaped to time.monotonic() the seam is read once and the
        # reported duration becomes the gap between a scripted value and the
        # machine's uptime. See TestTheClockSeamIsFullyInjected.
        assert self.clock.reads - before == 2, (
            f"the cycle-duration seam was read {self.clock.reads - before} "
            "times, not twice — a read escaped to the real clock, or a third "
            "was added"
        )

    def run_cycle(self, reports, real_sleep=0.0):
        """Measured as `reports`; really takes `real_sleep`.

        Pass real_sleep=_TIMER_SLEEP only when the test needs the watchdog
        Timer to have fired (i.e. it asserts on phase_at_deadline, or on the
        fire-time report the Timer emits). Everything else leaves it at 0 and
        runs in microseconds.
        """

        def _sync(*_a, **_k):
            if real_sleep:
                time.sleep(real_sleep)
            return _ok_stats()

        self.sync_engine.sync.side_effect = _sync
        self._drive(reports)


class TestOverrunIsMeasured(_Harness):
    def test_a_marginal_overrun_reports_the_marginal_band(self):
        """1.05x. Band selection is exact here — the reported duration is
        scripted, so it does not matter how loaded the machine is. This is the
        test the CI runner broke: the marginal band is 60ms wide at this
        deadline and the runner's own overhead was larger than the band."""
        self.run_cycle(MARGINAL_ELAPSED)

        got = self.recorder.by_fingerprint(MARGINAL)
        assert len(got) == 1, self.recorder.captures
        assert got[0]["level"] == "warning"
        assert got[0]["tags"] == {"component": "sync-watchdog"}

    def test_a_moderate_overrun_reports_the_moderate_band(self):
        self.run_cycle(MODERATE_ELAPSED)

        assert len(self.recorder.by_fingerprint(MODERATE)) == 1, self.recorder.captures
        assert self.recorder.by_fingerprint(MARGINAL) == []

    def test_a_severe_overrun_reports_the_severe_band(self):
        self.run_cycle(SEVERE_ELAPSED)

        assert len(self.recorder.by_fingerprint(SEVERE)) == 1, self.recorder.captures
        assert self.recorder.by_fingerprint(MODERATE) == []

    def test_the_report_carries_the_elapsed_time_and_the_phase(self):
        self.run_cycle(MODERATE_ELAPSED, real_sleep=_TIMER_SLEEP)

        got = self.recorder.by_fingerprint(MODERATE)[0]
        assert got["context"]["phase_at_deadline"] == "sync"
        assert got["context"]["deadline_seconds"] == self.TEST_DEADLINE
        # Exact, not a tolerance — the duration is scripted. A tolerance is
        # what a wall-clock margin buys, and it is what this file removed.
        assert got["context"]["elapsed_seconds"] == 0.4
        assert "finished at 0.4s" in got["message"], got["message"]

    def test_the_elapsed_time_tracks_the_actual_duration_not_a_constant(self):
        """A hardcoded literal (e.g. 0.5) would satisfy a wide band like
        `0.45 <= x < 5.0` for every fixture in this file — they all land
        somewhere in that range. Run two cycles with distinctly different
        durations and require elapsed_seconds to track EACH one; no single
        constant can satisfy both, and the two reported values must move apart
        by roughly the gap between them.

        The same demand is made of the MESSAGE, and that half is the one that
        matters operationally: the daily digest renders `message` and the
        occurrence count and reads `context` never, so a message frozen to
        `finished at 0.5s` would publish a fixed number to every operator
        while every context assertion above stayed green."""
        self.run_cycle(MODERATE_ELAPSED)
        short_capture = self.recorder.by_fingerprint(MODERATE)[0]
        short = short_capture["context"]["elapsed_seconds"]
        short_message = short_capture["message"]
        assert short == 0.4

        self.recorder.captures.clear()
        self.run_cycle(SEVERE_ELAPSED)
        long_capture = self.recorder.by_fingerprint(SEVERE)[0]
        long = long_capture["context"]["elapsed_seconds"]
        long_message = long_capture["message"]
        assert long == 0.8

        assert long - short > 0.2
        # Two materially different cycles must not produce one string. A
        # frozen figure in the message satisfies every other assertion in
        # this file — this is the only one it cannot.
        assert short_message != long_message, (short_message, long_message)

    def test_the_report_also_carries_the_exit_phase_and_the_two_differ(self):
        """phase_at_deadline and phase_at_exit are genuinely different fields:
        the watchdog fires mid-sync (deadline snapshot: 'sync'), but the cycle
        goes on to complete normally, so the LAST stamp before the cycle ends
        is a later stage. Proving they differ is what stops someone
        'simplifying' the pair back into one field.

        Needs the Timer, and asserts the deadline phase positively — a cycle
        where the Timer never fired would report 'unknown', which also differs
        from 'hours_fetch' and would pass this vacuously."""
        self.run_cycle(MODERATE_ELAPSED, real_sleep=_TIMER_SLEEP)

        got = self.recorder.by_fingerprint(MODERATE)[0]
        assert got["context"]["phase_at_deadline"] == "sync"
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
            time.sleep(_TIMER_SLEEP)
            return True

        self.coord._monitor_capture_health = _slow_health
        self._drive(MODERATE_ELAPSED)

        got = self.recorder.by_fingerprint(MODERATE)[0]
        assert got["context"]["phase_at_deadline"] == "capture_health"
        # It still completes the cycle normally afterwards (sync() is instant
        # here), so the exit phase has moved on — same pair shape as the
        # sync-overrun fixtures above, different deadline phase.
        assert got["context"]["phase_at_exit"] == "hours_fetch"
        # And the MESSAGE has to carry it. The digest reads message + count
        # and never reads context, so the phase reaching an operator depends
        # entirely on this line — deleting `in phase '...'` from the message
        # leaves every context assertion in this file green. This is also the
        # only fixture whose deadline phase is not 'sync', so it is the only
        # one that can tell a real phase in the message from a constant.
        assert "capture_health" in got["message"], got["message"]

    def test_an_aw_bucket_failure_exits_at_post_sync_not_hours_fetch(self):
        """The discriminating fixture for phase_at_exit. Every other test in
        this file completes normally and exits at 'hours_fetch' — a mutant
        hardcoding "phase_at_exit": "hours_fetch" is invisible to all of them
        (the same agreement-region shape as the capture_health test above,
        this time on the exit field). aw_bucket_fetch_failed returns _do_sync
        right after the post_sync stamp, before hours_fetch is ever reached."""
        def _slow_failed_sync(*_a, **_k):
            time.sleep(_TIMER_SLEEP)
            stats = _ok_stats()
            stats.aw_bucket_fetch_failed = True
            return stats

        self.sync_engine.sync.side_effect = _slow_failed_sync
        self._drive(MODERATE_ELAPSED)

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
        forcing phase.at_deadline to stay None for the whole cycle.

        With a scripted duration this needs no sleep at all: the cycle is
        measured as an overrun while really taking microseconds."""

        class _NoOpTimer:
            def __init__(self, *_a, **_k):
                pass

            def start(self):
                pass

            def cancel(self):
                pass

        monkeypatch.setattr(main_module.threading, "Timer", _NoOpTimer)
        self.run_cycle(MODERATE_ELAPSED)

        got = self.recorder.by_fingerprint(MODERATE)[0]
        assert got["context"]["phase_at_deadline"] == "unknown"
        assert got["context"]["phase_at_deadline"] != got["context"]["phase_at_exit"]


class TestTheClockSeamIsFullyInjected(_Harness):
    """The partial-injection guard, spelled out as its own test.

    _do_sync reads the clock twice — the start stamp and the elapsed in its
    finally — and BOTH must come from SyncCoordinator._monotonic. Route only
    one through the seam and the reported duration becomes the gap between a
    scripted value and the machine's uptime.

    This is issue #131's shape one file over: there, a reader took an injected
    `now=` while the sibling function that WROTE the timestamp used the real
    clock, and the fixture broke the day wall-clock passed it — presenting as
    a defect in the feature rather than in the test. One seam covering both
    reads is what makes that unrepresentable.

    Witnessed by pointing each read at time.monotonic() in turn; see the
    commit message for which assertion caught which direction.
    """

    def test_the_reported_elapsed_is_exactly_the_scripted_delta(self):
        # 0.7 / 0.3 = 2.33x -> severe, and rounds to a one-decimal figure that
        # no other fixture in this file produces.
        self.run_cycle(0.7)

        got = self.recorder.by_fingerprint(SEVERE)
        assert len(got) == 1, self.recorder.captures
        # Exact equality. If either read escaped, this is ~1e9 (start on the
        # real clock) — and if it escaped the other way the elapsed is ~-1e9,
        # below the deadline, so no capture exists and the length assertion
        # above fires instead.
        assert got[0]["context"]["elapsed_seconds"] == 0.7
        assert "finished at 0.7s" in got[0]["message"], got[0]["message"]

    def test_a_second_cycle_is_measured_independently(self):
        """Two cycles, two scripted durations, no drift between them — the
        seam must not accumulate state that makes cycle N+1's measurement
        depend on cycle N's."""
        self.run_cycle(0.7)
        self.run_cycle(MARGINAL_ELAPSED)

        assert self.recorder.by_fingerprint(SEVERE)[0]["context"][
            "elapsed_seconds"
        ] == 0.7
        assert self.recorder.by_fingerprint(MARGINAL)[0]["context"][
            "elapsed_seconds"
        ] == 0.3


class TestHealthyCyclesStaySilent(_Harness):
    """THE critical negative. If the predicate is implemented backwards this is
    the only test that catches it, and the failure mode is an outcome report on
    every healthy cycle — flooding the ingest this change exists to quieten."""

    def test_a_cycle_inside_the_deadline_emits_no_outcome_report(self):
        self.run_cycle(HEALTHY_ELAPSED)  # far under 0.3

        assert _outcome_captures(self.recorder) == [], self.recorder.captures

    def test_the_negative_above_is_not_passing_vacuously(self):
        """A cycle that never ran would also produce zero outcome captures.
        Prove the subject was reached: the same harness, overrunning, DOES
        produce one."""
        self.run_cycle(HEALTHY_ELAPSED)
        assert _outcome_captures(self.recorder) == []

        self.recorder.captures.clear()
        self.run_cycle(MODERATE_ELAPSED)
        assert len(_outcome_captures(self.recorder)) == 1, self.recorder.captures


class TestExistingReportsAreUnchanged(_Harness):
    def test_an_overrun_still_emits_its_fire_time_error(self):
        """The outcome report is additive. The group that already holds 34
        occurrences must keep firing exactly as before. Emitted by the Timer
        thread, so this one genuinely has to outlive the deadline."""
        self.run_cycle(MODERATE_ELAPSED, real_sleep=_TIMER_SLEEP)

        hung = self.recorder.by_fingerprint("sync-watchdog-timeout")
        assert len(hung) == 1, self.recorder.captures
        assert hung[0]["level"] == "error"

    def test_the_outcome_report_never_uses_error_level(self):
        """It measures; the fire-time report pages. A second error would double
        the alert volume."""
        self.run_cycle(SEVERE_ELAPSED)

        captures = _outcome_captures(self.recorder)
        assert captures, self.recorder.captures
        for capture in captures:
            assert capture["level"] == "warning"


class TestEveryOverrunIsCounted(_Harness):
    """The design's central claim is that the occurrence counter IS the
    distribution — the digest reads message + count and never reads context,
    so a band's count is the only thing that publishes how the population is
    shaped. That claim is only true if no overrun is ever suppressed.

    This has to drive the REAL ErrorReporter. `_Recorder` (used by every other
    class in this file) has no dedup at all, so it cannot see the client-side
    cooldown and would pass whatever the production code did."""

    def setup_method(self):
        super().setup_method()
        self.posted = []
        posted_lock = threading.Lock()
        outer = self

        class _CapturingReporter(ErrorReporter):
            def _post(self, payload):  # noqa: N805 - matches the base signature
                with posted_lock:
                    outer.posted.append(payload)

        self.coord.error_reporter = _CapturingReporter(
            endpoint="https://bot.example/notify/error",
            dsn="dsn-token-1234567890",
            release="test",
            # Deliberately huge: with the shipped 300s default this fixture
            # would be timing-dependent. A window this size means any
            # suppression at all is a hard failure rather than a race.
            dedup_window=10_000,
        )

    def _drain(self):
        for t in threading.enumerate():
            if t.name == "error-report":
                t.join(timeout=5)

    def _overrun_payloads(self):
        return [p for p in self.posted if p["message"].startswith("Sync overran")]

    def test_two_consecutive_overruns_in_one_band_are_both_reported(self):
        """Both cycles land in the same band, so they share a fingerprint —
        the exact case the reporter's per-fingerprint cooldown suppresses.
        Suppressed, the second overrun is invisible to the counter and the
        band's count stops being its population.

        The bias this closes is not uniform: the marginal band (150-180s at
        the real deadline) puts the next tick ~180s later and loses roughly
        40% of its reports to a 300s window, while the severe band (>=300s)
        can never be throttled at all — so the digest overstates severe's
        share, the wrong direction for a question whose known failure mode is
        a false 'hung'."""
        self.run_cycle(MODERATE_ELAPSED)
        self.run_cycle(MODERATE_ELAPSED)
        self._drain()

        got = self._overrun_payloads()
        assert len(got) == 2, [p["message"] for p in self.posted]
        assert all(p["level"] == "warning" for p in got)

    def test_the_fire_time_report_keeps_its_default_dedup(self):
        """The vacuity control, and a scope check in one. If `dedup_window=0`
        had been applied to the reporter or to every capture rather than to
        the outcome report alone, this would post twice — and the fire-time
        group is the one that pages, whose volume this change exists to leave
        exactly as it was.

        Both cycles must genuinely fire the Timer, or a single 'Sync hung'
        would mean 'suppressed once' and 'only fired once' indistinguishably."""
        self.run_cycle(MODERATE_ELAPSED, real_sleep=_TIMER_SLEEP)
        self.run_cycle(MODERATE_ELAPSED, real_sleep=_TIMER_SLEEP)
        self._drain()

        hung = [p for p in self.posted if p["message"].startswith("Sync hung")]
        assert len(hung) == 1, [p["message"] for p in self.posted]


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
        raised = []

        def _fail_only_the_outcome_report(message, **kwargs):
            if kwargs.get("fingerprint") in ALL_BANDS:
                raised.append(message)
                raise RuntimeError("can't start new thread")
            return real_capture(message, **kwargs)

        self.recorder.capture = _fail_only_the_outcome_report

        self.run_cycle(MODERATE_ELAPSED)  # must not raise

        # Not vacuous: the outcome report must actually have been attempted,
        # or "the heartbeat still ran" says nothing about the guard.
        assert raised, "the outcome report was never attempted"
        assert self.sync_engine.send_heartbeat_if_due.called
