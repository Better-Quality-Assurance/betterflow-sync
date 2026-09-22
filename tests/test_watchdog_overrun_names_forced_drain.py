"""An overrun the agent BUILT must not look like one it SUFFERED.

``SyncEngine._drain_gate_allows`` forces one queue drain through a spent
network budget after ``_DELIVERY_STARVATION_FLOOR_CYCLES`` starved cycles,
rather than let uploads freeze forever behind a degraded ``/session/start``.
Its own docstring prices that at "~94s (session chain) + ~94s (drain) ~= 188s,
over the 150s 'Sync hung' report threshold", calls the resulting noisy watchdog
report "the correct trade", and ends:

    the report is also how an operator finds out this is happening.

That clause was false. The floor announced itself in a ``logger.warning`` and
nowhere else, and ``betterflow.log`` never leaves the device except on an
explicit admin request — so the only artifact an operator could see was a bare
``Sync overran the 150s deadline — finished at 211.3s in phase 'sync'`` on the
ops board, which is byte-for-byte what a genuine hang produces. Seven
unclassifiable deadline/queue rows on the board (2026-09-11 → 2026-09-22) are
what sent someone looking.

WHY THE ASSERTIONS READ THE MESSAGE AND THE FINGERPRINT, NOT THE CONTEXT
-----------------------------------------------------------------------
``_report_overrun_outcome``'s own comment says the ops digest "reads message +
count and never reads context". A ``forced_drain`` context key would therefore
be true, correct and invisible to the only reader that matters. A test that
asserted the context would pass while the operator's problem stayed exactly as
it was, so the context assertion here is a companion, never the proof.

WHY THE ENGINE IS REAL
----------------------
``tests/_watchdog_harness.py`` hands ``_do_sync`` a ``Mock(spec=SyncEngine)``
whose ``sync()`` returns a fixture. Every test built on it therefore SUPPLIES
the stats the report reads, so none of them can see a caller failing to produce
``forced_drain`` in the first place (test-fixture-discipline Phantom 14). The
end-to-end test below drives a REAL ``SyncEngine`` under a real session-start
outage through the REAL ``SyncCoordinator._do_sync``: nothing in it writes
``forced_drain``, and the only thing that can set it is the production drain
gate deciding to force a drain.
"""

import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import src.sync.sync_engine as se
from src.config import Config
from src.main import SyncCoordinator
from src.reminders import ReminderManager
from src.sync.bf_client import SyncResult
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.http_client import BetterFlowClientError
from src.sync.queue import OfflineQueue
from src.sync.sync_engine import SyncEngine
from tests._watchdog_harness import CoordinatorHarness, _ok_stats, _Recorder

FORCED_DRAIN = "sync-watchdog-overrun-forced-drain"
MARGINAL = "sync-watchdog-overrun-marginal"
MODERATE = "sync-watchdog-overrun-moderate"
SEVERE = "sync-watchdog-overrun-severe"
BANDS = (MARGINAL, MODERATE, SEVERE)

#: One full retry chain against a hung endpoint, per DEFAULT_RETRY_CONFIG's own
#: comment in sync/http_client.py. Mirrors tests/test_send_budget_starvation_floor.py.
HUNG_CHAIN_SECONDS = 94.0

#: Scripted cycle duration, as a multiple of the harness's 0.3s TEST_DEADLINE.
#: 1.40x lands unambiguously in the MODERATE band, which is the whole point of
#: choosing it: pre-fix a forced-drain overrun is reported as MODERATE, so the
#: band and the forced-drain group are separated by nothing but this change.
MODERATE_ELAPSED = 0.42
HEALTHY_ELAPSED = 0.02  # 0.07x -> under the deadline, no report at all


class _ScriptedClock:
    """`base` on a cycle's first read, `base + elapsed` on its second.

    Copied in shape from tests/test_watchdog_overrun_outcome.py: the duration
    _do_sync MEASURES must be exact on any machine, so it cannot come from the
    duration the cycle actually TAKES. `base` is ~31 years of uptime so a read
    that escaped to the real clock is loudly wrong rather than plausible.
    """

    def __init__(self, elapsed=0.0, base=1_000_000_000.0):
        self.elapsed = elapsed
        self.base = base
        self.reads = 0

    def __call__(self):
        value = self.base + (self.elapsed if self.reads % 2 else 0.0)
        self.reads += 1
        return value


class _SessionStartOutage:
    """Backend degraded on /session/start only; /events/batch is healthy.

    Each attempt raises AND burns a full retry chain off the cycle's shared
    network budget — which is the mechanism that starves delivery and is what
    eventually makes the floor force a drain. Same shape as
    tests/test_send_budget_starvation_floor.py, which pins the floor itself.
    """

    def __init__(self):
        self.now = 10_000.0
        self.session_attempts = 0

    def monotonic(self) -> float:
        return self.now

    def start_session(self):
        self.session_attempts += 1
        self.now += HUNG_CHAIN_SECONDS
        raise BetterFlowClientError("session start 503")


def _outcome_captures(recorder):
    """Every cycle-end overrun report, whatever group it landed in."""
    return [
        c
        for c in recorder.captures
        if c.get("fingerprint") in BANDS or c.get("fingerprint") == FORCED_DRAIN
    ]


class TestARealForcedDrainNamesItself:
    """End to end: real engine, real drain gate, real _do_sync, real report.

    Nothing here writes ``forced_drain``. The engine has to produce it from a
    genuine starvation sequence, ``sync()`` has to carry it out on SyncStats,
    and ``_do_sync`` has to read it back in its finally block. Any link missing
    and the report lands in a duration band instead.
    """

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.queue = OfflineQueue(db_path=self.tmp / "q.db", max_size=1000)
        self.tracker = DailyTimeTracker(db_path=self.tmp / "t.db")
        self.outage = _SessionStartOutage()

        aw = Mock()
        aw.is_running.return_value = True
        # No new capture this cycle: the backlog already queued is what has to
        # get out. Keeps the test on the delivery gates, not the fetch path.
        aw.get_window_buckets.return_value = []
        aw.get_web_buckets.return_value = []
        aw.get_afk_buckets.return_value = []
        aw.get_input_buckets.return_value = []

        bf = Mock()
        bf.is_reachable.return_value = True
        bf.start_session.side_effect = self.outage.start_session
        bf.send_events.side_effect = lambda batch: SyncResult(
            success=True, events_synced=len(batch)
        )
        self.bf = bf

        config = Config()
        config.working_hours.known = True
        self.engine = SyncEngine(
            aw=aw, bf=bf, queue=self.queue, config=config, time_tracker=self.tracker
        )
        self.engine._config_fetched = True
        self.engine._backlog_reconciled = True
        # The heartbeat runs after the sync lock is released and is not part of
        # the property under test; a real one would reach the network.
        self.engine.send_heartbeat_if_due = Mock(return_value=None)

        tray = Mock()
        tray.model = Mock()
        tray.model.lock = threading.RLock()
        self.recorder = _Recorder()

        self.coord = SyncCoordinator(
            config=config,
            aw=aw,
            bf=bf,
            queue=self.queue,
            sync_engine=self.engine,
            tray=tray,
            aw_manager=Mock(is_managing=False),
            reminder_manager=Mock(spec=ReminderManager),
        )
        self.coord.scheduler = Mock(running=True)
        self.coord.error_reporter = self.recorder
        self.coord._fetch_hours_today = Mock(return_value="1:00")
        # Capture-health self-heal is a separate subsystem with its own tests;
        # True means "proceed to the upload sync", which is this test's subject.
        self.coord._monitor_capture_health = Mock(return_value=True)
        self.coord._DO_SYNC_DEADLINE = CoordinatorHarness.TEST_DEADLINE
        self.clock = _ScriptedClock(elapsed=MODERATE_ELAPSED)
        self.coord._monotonic = self.clock

        self._seed_backlog()

    def teardown_method(self):
        self.queue.close()
        self.tracker.close()

    def _seed_backlog(self, count: int = 12) -> None:
        now = datetime.now(timezone.utc)
        self.queue.enqueue([
            {
                "id": f"billable-{i}",
                "bucket_id": "aw-watcher-afk_h",
                "timestamp": (now - timedelta(minutes=i + 1)).isoformat(),
                "duration": 60,
                "data": {"status": "not-afk"},
            }
            for i in range(count)
        ])

    def _run_cycles(self, monkeypatch, count):
        """`count` cycles through the real _do_sync, each on a fresh budget."""
        monkeypatch.setattr(se.time, "monotonic", self.outage.monotonic)
        for _ in range(count):
            self.engine._queue_backoff_until = datetime.min.replace(
                tzinfo=timezone.utc
            )
            self.coord._do_sync()

    def test_the_forced_drain_overrun_is_its_own_group(self, monkeypatch):
        """The report an operator triages must say which of the two it is.

        Pre-fix this lands in the MODERATE duration band — indistinguishable on
        the board from an unexplained 1.4x hang.
        """
        self._run_cycles(monkeypatch, SyncEngine._DELIVERY_STARVATION_FLOOR_CYCLES)

        # Precondition, not decoration: if the outage never starved delivery
        # the floor never engaged, and the assertion below would be testing
        # nothing (test-fixture-discipline Phantom 4 — assert the state the
        # subject needs in order to be REACHED).
        assert self.bf.send_events.called, (
            "the floor never forced a drain, so this fixture cannot express "
            f"the defect. session_attempts={self.outage.session_attempts}, "
            f"queue_size={self.queue.size()}"
        )

        forced = self.recorder.by_fingerprint(FORCED_DRAIN)
        assert len(forced) == 1, (
            "a cycle that overran because the delivery-starvation floor forced "
            "a drain was reported under a duration band, where it is "
            "indistinguishable from a genuine hang. Captures: "
            f"{[(c.get('fingerprint'), c['message'][:70]) for c in self.recorder.captures]}"
        )

    def test_the_message_says_so_because_the_digest_reads_the_message(
        self, monkeypatch
    ):
        """The fingerprint groups the row; the MESSAGE is what a human reads.

        _report_overrun_outcome's own comment records that the ops digest reads
        message + count and never reads context, so putting the explanation
        anywhere else leaves the operator with the same bare sentence.
        """
        self._run_cycles(monkeypatch, SyncEngine._DELIVERY_STARVATION_FLOOR_CYCLES)

        forced = self.recorder.by_fingerprint(FORCED_DRAIN)
        assert forced, self.recorder.captures
        message = forced[0]["message"]
        assert "delivery-starvation floor" in message, message
        assert "not a hang" in message, message
        # The original sentence is still there — this is additive, and the
        # elapsed figure and phase are what the existing tests pin.
        assert "Sync overran" in message, message
        assert "in phase" in message, message

    def test_the_forced_drain_cycle_is_not_also_counted_as_a_hang(self, monkeypatch):
        """One cycle, one row — the forced drain REPLACES the duration band.

        Counting it in both inflates exactly the distribution the bands exist
        to publish. The arithmetic is the assertion: N cycles here all overrun
        (the scripted duration is per-cycle), the last is the forced drain, so
        N-1 land in bands and 1 lands in the forced-drain group. An
        implementation that emitted both would report N+1.

        Note what this does NOT say: the earlier starved cycles overrun too and
        belong in a band, because nothing explains them yet — the floor has not
        engaged. Asserting 'no band at all' would have been wrong, and was; the
        first draft of this test failed here and the assertion was the defect.
        """
        cycles = SyncEngine._DELIVERY_STARVATION_FLOOR_CYCLES
        self._run_cycles(monkeypatch, cycles)

        banded = [c for c in self.recorder.captures if c.get("fingerprint") in BANDS]
        forced = self.recorder.by_fingerprint(FORCED_DRAIN)

        assert len(forced) == 1, self.recorder.captures
        assert len(banded) == cycles - 1, (
            "every cycle overran, so there must be exactly one report per "
            f"cycle: {cycles - 1} banded + 1 forced-drain. Got "
            f"{len(banded)} banded + {len(forced)} forced. Captures: "
            f"{[(c.get('fingerprint'), c['message'][:60]) for c in self.recorder.captures]}"
        )
        assert len(_outcome_captures(self.recorder)) == cycles

    def test_a_forced_drain_inside_the_deadline_still_reports_nothing(
        self, monkeypatch
    ):
        """THE flood negative. The trigger is still the overrun, never the
        forced drain: the floor is allowed to engage on a fast cycle (a small
        queue drains quickly) and that must stay silent."""
        self.clock.elapsed = HEALTHY_ELAPSED
        self._run_cycles(monkeypatch, SyncEngine._DELIVERY_STARVATION_FLOOR_CYCLES)

        assert self.bf.send_events.called, (
            "vacuous: the floor never engaged, so this proves nothing about "
            "a forced drain staying silent under the deadline"
        )
        assert _outcome_captures(self.recorder) == [], self.recorder.captures


class TestAnOrdinaryOverrunIsUnchanged(CoordinatorHarness):
    """The other half of the property.

    Without this, 'always report forced_drain' passes every test above while
    destroying the duration bands — and the bands are how an unexplained
    overrun's distribution is published. Uses the shared mocked harness on
    purpose: here the point is that a cycle with NO forced drain is untouched.
    """

    def setup_method(self):
        super().setup_method()
        self.clock = _ScriptedClock(elapsed=MODERATE_ELAPSED)
        self.coord._monotonic = self.clock
        self.sync_engine.sync.return_value = _ok_stats()

    def test_an_overrun_with_no_forced_drain_keeps_its_duration_band(self):
        self.coord._do_sync()

        assert len(self.recorder.by_fingerprint(MODERATE)) == 1, (
            self.recorder.captures
        )
        assert self.recorder.by_fingerprint(FORCED_DRAIN) == []

    def test_and_its_message_makes_no_forced_drain_claim(self):
        """A wrong explanation is worse than none: it converts a thing somebody
        would have investigated into a thing they defer to."""
        self.coord._do_sync()

        message = self.recorder.by_fingerprint(MODERATE)[0]["message"]
        assert "starvation" not in message, message
        assert "forced" not in message, message

    def test_a_cycle_that_never_reached_sync_is_not_a_forced_drain(self):
        """_do_sync returns early on private / on-break / capture-health, so
        `stats` is still None when the finally block reads it. None must read
        as 'no drain gate ran', never crash and never claim a forced drain."""
        self.sync_engine.is_private = True

        self.coord._do_sync()

        assert self.recorder.by_fingerprint(FORCED_DRAIN) == [], (
            self.recorder.captures
        )
        assert len(self.recorder.by_fingerprint(MODERATE)) == 1, (
            "the early-return cycle still overran and must still be reported — "
            f"captures: {self.recorder.captures}"
        )
