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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import src.sync.sync_engine as se
from src.config import Config
from src.main import SyncCoordinator
from src.reminders import ReminderManager
from src.sync.bf_client import SyncResult
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.http_client import BetterFlowAuthError, BetterFlowClientError
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

    def _run_cycles(self, monkeypatch, count, *, clear_backoff=True):
        """`count` cycles through the real _do_sync, each on a fresh budget."""
        monkeypatch.setattr(se.time, "monotonic", self.outage.monotonic)
        for _ in range(count):
            if clear_backoff:
                self.engine._queue_backoff_until = datetime.min.replace(
                    tzinfo=timezone.utc
                )
            self.coord._do_sync()

    def test_a_forced_drain_that_never_drained_is_not_a_designed_overrun(
        self, monkeypatch, caplog
    ):
        """The stamp is written by the GATE, and the gate is not the drain.

        ``_process_queue`` returns at its own queue-backoff gate before sending
        anything, so a forced drain that lands in that window costs ~0s and
        cannot be what made the cycle overrun. Labelling it "designed overrun,
        not a hang" would de-prioritise exactly the row the classification
        exists to make triageable — the reassuring direction, on the signal
        that decides whether anyone looks.

        Found by the pre-commit adversarial review, which noted the gate's
        stamp is provisional; this is the witness it flagged as missing.
        """
        import logging

        cycles = SyncEngine._DELIVERY_STARVATION_FLOOR_CYCLES
        with caplog.at_level(logging.WARNING, logger="src.sync.sync_engine"):
            # Starve delivery up to the cycle before the floor...
            self._run_cycles(monkeypatch, cycles - 1)
            # ...then park the queue, so the floor engages and _process_queue
            # bails at the backoff gate having sent nothing.
            self.engine._queue_backoff_until = datetime.now(
                timezone.utc
            ) + timedelta(minutes=10)
            self._run_cycles(monkeypatch, 1, clear_backoff=False)

        # Two preconditions, because without them this passes vacuously: the
        # floor must have engaged (else there is no stamp to clear) AND nothing
        # must have drained (else the overrun really was bought).
        assert any(
            "starvation floor engaged" in r.getMessage() for r in caplog.records
        ), f"the floor never engaged: {[r.getMessage()[:50] for r in caplog.records]}"
        assert not self.bf.send_events.called, (
            "the backoff gate did not hold, so this fixture cannot express the "
            "defect — something drained and the overrun WAS bought"
        )

        assert self.recorder.by_fingerprint(FORCED_DRAIN) == [], (
            "a cycle that drained nothing was reported as a designed overrun. "
            f"Captures: {[(c.get('fingerprint'), c['message'][:70]) for c in self.recorder.captures]}"
        )
        assert len(self.recorder.by_fingerprint(MODERATE)) == cycles, (
            "it must still be reported — as an unexplained overrun, which is "
            "what it is"
        )

    def test_a_forced_drain_that_raises_401_still_names_itself(self, monkeypatch):
        """sync() never RETURNS on the auth path, so the stamp has to survive
        the exception.

        A forced drain whose batch takes a 401 still ran the ~94s session chain
        and the ~94s drain, still overran, and — before the review's fix —
        still reached the board anonymous, because ``stats`` was never assigned
        in ``_do_sync``. The one cycle most worth explaining was the one that
        could not explain itself.
        """
        from src.sync.http_client import BetterFlowAuthError

        # The auth handler is a separate subsystem (re-login); the property
        # here is what the OVERRUN report says, not what auth does about it.
        self.coord._handle_auth_error = Mock()
        cycles = SyncEngine._DELIVERY_STARVATION_FLOOR_CYCLES
        self._run_cycles(monkeypatch, cycles - 1)
        self.bf.send_events.side_effect = BetterFlowAuthError("401 token expired")
        self._run_cycles(monkeypatch, 1)

        assert self.coord._handle_auth_error.called, (
            "the 401 never reached the auth handler, so the raise path was not "
            "exercised and this proves nothing"
        )
        forced = self.recorder.by_fingerprint(FORCED_DRAIN)
        assert len(forced) == 1, (
            "a forced drain that raised 401 was reported under a duration band "
            "— the stamp was lost with the return value. Captures: "
            f"{[(c.get('fingerprint'), c['message'][:70]) for c in self.recorder.captures]}"
        )

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


class TestARealForcedDrainStillRunningAtTheDeadlineIsNotAHang:
    """The end-of-cycle report is not the only report a forced drain triggers.

    ``SyncCoordinator._do_sync`` starts a REAL ``threading.Timer(_DO_SYNC_
    DEADLINE, _watchdog)`` before it does anything else. That Timer fires from
    the real OS clock, independent of whatever engine-internal clock a test
    scripts — so for a genuine ~188s forced drain it fires WHILE
    ``_process_queue`` is still blocked on the network, long before ``sync()``
    returns and ``_do_sync`` ever sees a ``SyncStats`` with ``forced_drain``
    set. 40aaaac taught ``_report_overrun_outcome`` (the report ``_do_sync``'s
    ``finally`` posts once the cycle actually ends) to name a forced drain. It
    never touched ``_watchdog()`` (the fire-time report), which has no stats
    object to read at all — only ``phase`` and the transient-failure counter —
    so a forced drain that is merely slow rather than failing outright still
    reports exactly like a genuine hang: ``level=error`` / "Sync hung" /
    ``sync-watchdog-timeout``.

    The starvation floor is driven by an auth failure on ``start_session``
    rather than the file's usual ``_SessionStartOutage`` (a transient 503) on
    purpose: ``BetterFlowAuthError._COUNTS_AS_NETWORK_FAILURE`` is False (see
    ``http_client.py`` — a 401 is definitive for the watchdog's question, not
    a network outage), so it starves delivery the same way without ever
    touching the transient-failure counter the fire-time report keys off. A
    counter left untouched all cycle is what makes this reproduce the gap: the
    forcing cycle has genuinely had ZERO transient failures by the time the
    real Timer fires mid-drain, exactly like a merely-slow-but-healthy
    backend would.

    Real wall-clock time throughout, on purpose — the other classes in this
    file script ``coord._monotonic`` and the engine's own ``time.monotonic``
    and never actually wait, so none of them can see whether the real Timer
    fires before or after a real blocking call returns. The per-cycle network
    budget is shrunk to make that observable without a real 50s wait.
    """

    _BUDGET_SECONDS = 0.05

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.queue = OfflineQueue(db_path=self.tmp / "q.db", max_size=1000)
        self.tracker = DailyTimeTracker(db_path=self.tmp / "t.db")

        aw = Mock()
        aw.is_running.return_value = True
        aw.get_window_buckets.return_value = []
        aw.get_web_buckets.return_value = []
        aw.get_afk_buckets.return_value = []
        aw.get_input_buckets.return_value = []

        def _slow_auth_failure():
            # A little real sleep so the shrunk budget is unambiguously spent
            # by the time the drain gate is checked afterwards, on any machine.
            time.sleep(self._BUDGET_SECONDS * 2)
            raise BetterFlowAuthError("token expired")

        bf = Mock()
        bf.is_reachable.return_value = True
        bf.start_session.side_effect = _slow_auth_failure
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
        self.coord._monitor_capture_health = Mock(return_value=True)
        self.coord._DO_SYNC_DEADLINE = CoordinatorHarness.TEST_DEADLINE
        # Deliberately NOT scripted here (contrast the other classes in this
        # file): the real elapsed/cycle_started_at values don't matter to this
        # test, and leaving the real clock in place keeps it obvious that
        # nothing about the Timer's own firing is being faked.

        now = datetime.now(timezone.utc)
        self.queue.enqueue([
            {
                "id": f"billable-{i}",
                "bucket_id": "aw-watcher-afk_h",
                "timestamp": (now - timedelta(minutes=i + 1)).isoformat(),
                "duration": 60,
                "data": {"status": "not-afk"},
            }
            for i in range(12)
        ])

    def teardown_method(self):
        self.queue.close()
        self.tracker.close()

    def _run_cycles(self, count):
        for _ in range(count):
            self.engine._queue_backoff_until = datetime.min.replace(tzinfo=timezone.utc)
            self.coord._do_sync()

    def test_a_forced_drain_still_draining_at_the_deadline_is_not_reported_as_a_hang(
        self, monkeypatch
    ):
        # Shrink the shared per-cycle network budget so the starvation floor
        # engages after a couple of real (but tiny) sleeps rather than a real
        # 50s wait. All three names have to move together — they're aliases of
        # one constant fixed at class-definition time (see sync_engine.py).
        monkeypatch.setattr(SyncEngine, "_CYCLE_NETWORK_BUDGET_SECONDS", self._BUDGET_SECONDS)
        monkeypatch.setattr(SyncEngine, "_QUEUE_SKIP_IF_CYCLE_ELAPSED", self._BUDGET_SECONDS)
        monkeypatch.setattr(SyncEngine, "_SEND_SKIP_IF_CYCLE_ELAPSED", self._BUDGET_SECONDS)

        # Warm up one cycle short of the floor. Each cycle's own start_session
        # attempt burns the (shrunk) budget and fails with a definitive auth
        # rejection — never a transient failure — so nothing is delivered and
        # the floor's counter advances without ever touching the tally the
        # fire-time report reads.
        self._run_cycles(SyncEngine._DELIVERY_STARVATION_FLOOR_CYCLES - 1)

        # Precondition: the floor must not have engaged yet, or the forcing
        # cycle below proves nothing.
        assert not self.bf.send_events.called, (
            "the floor engaged during warm-up; the forcing cycle below would "
            "prove nothing new"
        )
        from src.sync.http_client import transient_failure_count

        # The forcing cycle: the drain's send genuinely blocks in REAL
        # wall-clock time, well past the real Timer's deadline, and then
        # succeeds — nothing raises here either, so the transient-failure
        # counter stays exactly where the auth failures above left it
        # throughout this whole cycle, just like a slow-but-healthy backend.
        def _slow_but_healthy_send(batch):
            time.sleep(CoordinatorHarness.TEST_DEADLINE + 0.5)
            return SyncResult(success=True, events_synced=len(batch))

        self.bf.send_events.side_effect = _slow_but_healthy_send
        self.engine._queue_backoff_until = datetime.min.replace(tzinfo=timezone.utc)
        transient_before = transient_failure_count()

        self.coord._do_sync()

        assert self.bf.send_events.called, (
            "the floor never forced a drain on the forcing cycle, so this "
            f"fixture cannot express the defect. queue_size={self.queue.size()}"
        )
        assert transient_failure_count() == transient_before, (
            "the forcing cycle recorded a transient failure — this fixture is "
            "supposed to isolate the case where it does NOT, so it proves "
            "nothing about that gap"
        )
        hung = self.recorder.by_fingerprint("sync-watchdog-timeout")
        assert hung == [], (
            "a forced drain that was still blocked on the network when the "
            "real watchdog Timer fired was reported exactly like a genuine "
            "hang. Captures: "
            f"{[(c.get('fingerprint'), c.get('level'), c['message'][:70]) for c in self.recorder.captures]}"
        )
