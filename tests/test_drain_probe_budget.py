"""The reachability probe ahead of the queue drain must not overrun the watchdog (#291).

Ops board, BW TASK-002052 (2026-10-15): "Sync overran the 150s deadline -
finished at 156.0s in phase 'sync'", with nothing hung. The drain site in
``sync()`` called ``bf.is_reachable()`` BEFORE ``_drain_gate_allows``. On a cycle
that started a chain at the gate edge (~49.9s) and ran it (~94s), the gate was
always going to refuse. The probe still ran first, and against a blackholed API
it costs 2 endpoints x 2 addresses x ``REACHABILITY_TIMEOUT`` = 20s:
49.9 + 94 + 20 is over 150.

The fix asks the pure half of the gate (``_drain_gate_verdict``) first and skips
the probe when the answer is already a side-effect-free refusal. The
starvation floor (``_drain_gate_allows``, see its docstring) must behave exactly
as before, so these tests pin both:

* the worst case (gate edge + one chain + a probe pair) fits the deadline;
* the floor still forces a drain after N starved cycles, and an UNREACHABLE
  cycle still does not count toward it.

A fake clock drives ``time.monotonic`` in sync_engine; the fakes advance it by
the cost of the call they stand in for.
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import src.sync.sync_engine as se
from src.config import Config
from src.main import SyncCoordinator
from src.sync.bf_client import SyncResult
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.http_client import BaseApiClient, BetterFlowClientError
from src.sync.queue import OfflineQueue
from src.sync.sync_engine import SyncEngine
from tests.test_sync_watchdog_budget import _client_default_timeout, _worst_case_chain_seconds

#: One full retry chain against a hung endpoint (the watchdog test's own bound).
CHAIN = _worst_case_chain_seconds(BaseApiClient.DEFAULT_RETRY_CONFIG, _client_default_timeout())
#: A just-in-budget start: the chain was allowed to start, and ran to the end.
GATE_EDGE = SyncEngine._CYCLE_NETWORK_BUDGET_SECONDS - 0.1
#: is_reachable() against a blackholed API: health then events/status, each
#: paying REACHABILITY_TIMEOUT once per resolved address (2 A records).
PROBE_PAIR = 2 * 2 * BaseApiClient.REACHABILITY_TIMEOUT


class _Clock:
    def __init__(self):
        self.now = 50_000.0
        self.cycle_start = None

    def monotonic(self) -> float:
        return self.now


def _seed_backlog(queue: OfflineQueue, count: int = 12) -> None:
    now = datetime.now(timezone.utc)
    queue.enqueue([
        {"id": f"billable-{i}", "bucket_id": "aw-watcher-afk_h",
         "timestamp": (now - timedelta(minutes=i + 1)).isoformat(),
         "duration": 60, "data": {"status": "not-afk"}}
        for i in range(count)
    ])


def _build(queue, tt, clock, *, reachable=True):
    aw = Mock()
    aw.is_running.return_value = True
    aw.get_window_buckets.return_value = []
    aw.get_web_buckets.return_value = []
    aw.get_afk_buckets.return_value = []
    aw.get_input_buckets.return_value = []

    bf = Mock()
    state = {"reachable": reachable, "probes": 0}

    def probe():
        state["probes"] += 1
        if not state["reachable"]:
            clock.now += PROBE_PAIR  # every attempt times out
        return state["reachable"]

    bf.is_reachable.side_effect = probe
    bf.send_events.side_effect = lambda batch, **_kw: SyncResult(
        success=True, events_synced=len(batch)
    )
    config = Config()
    config.working_hours.known = True
    engine = SyncEngine(aw=aw, bf=bf, queue=queue, config=config, time_tracker=tt)
    engine._config_fetched = True
    engine._backlog_reconciled = True
    return engine, bf, state


def _fresh(tmp: Path):
    return OfflineQueue(db_path=tmp / "q.db", max_size=1000), DailyTimeTracker(db_path=tmp / "t.db")


def test_gate_edge_chain_then_probe_pair_fits_the_deadline(monkeypatch):
    """The #291 worst case: a chain that started at the gate edge and put events
    on the wire, then a probe pair against a blackholed API, on a cycle with a
    queued backlog. The drain cannot run (budget spent, cycle delivered), so the
    probe is pure cost and must not be paid."""
    clock = _Clock()
    monkeypatch.setattr(se.time, "monotonic", clock.monotonic)
    tmp = Path(tempfile.mkdtemp())
    queue, tt = _fresh(tmp)
    try:
        _seed_backlog(queue)
        engine, bf, state = _build(queue, tt, clock, reachable=False)

        def gate_edge_chain_that_delivered():
            # Something ahead burned the budget to its edge, then ONE chain was
            # allowed to start and ran to the end, putting events on the wire.
            clock.now += GATE_EDGE + CHAIN
            engine._note_delivery_attempt()
            raise BetterFlowClientError("session start 503")

        bf.start_session.side_effect = gate_edge_chain_that_delivered
        start = clock.now
        engine._queue_backoff_until = datetime.min.replace(tzinfo=timezone.utc)
        engine.sync()
        elapsed = clock.now - start

        deadline = SyncCoordinator._DO_SYNC_DEADLINE
        # Control: the scenario is the one #291 describes, not a cheaper one.
        assert GATE_EDGE + CHAIN < deadline < GATE_EDGE + CHAIN + PROBE_PAIR, (
            f"control: edge {GATE_EDGE}s + chain {CHAIN:.1f}s + probes {PROBE_PAIR}s "
            f"must straddle the {deadline}s deadline, or this test proves nothing"
        )
        assert elapsed < deadline, (
            f"the cycle took {elapsed:.1f}s against the {deadline}s watchdog: the "
            f"reachability probe ran ({state['probes']} probe(s)) although the drain "
            f"gate could only refuse"
        )
        assert state["probes"] == 0
        assert not bf.send_events.called
        assert queue.size() == 12, "the backlog waits for the next cycle"
    finally:
        queue.close()
        tt.close()


def _starved_cycle(engine, bf, clock):
    """One cycle whose session-start chain burns the budget and delivers nothing."""
    def burn():
        clock.now += CHAIN
        raise BetterFlowClientError("session start 503")

    bf.start_session.side_effect = burn
    engine._queue_backoff_until = datetime.min.replace(tzinfo=timezone.utc)
    engine.sync()


def test_the_starvation_floor_still_forces_a_drain_after_n_starved_cycles(monkeypatch):
    """Undelivered, budget-spent cycles still reach the probe and still count:
    the floor forces the drain on exactly the Nth cycle, as before."""
    clock = _Clock()
    monkeypatch.setattr(se.time, "monotonic", clock.monotonic)
    tmp = Path(tempfile.mkdtemp())
    queue, tt = _fresh(tmp)
    try:
        _seed_backlog(queue)
        engine, bf, state = _build(queue, tt, clock, reachable=True)
        delivered_on = None
        for cycle in range(1, SyncEngine._DELIVERY_STARVATION_FLOOR_CYCLES + 3):
            _starved_cycle(engine, bf, clock)
            if bf.send_events.called and delivered_on is None:
                delivered_on = cycle
        assert delivered_on == SyncEngine._DELIVERY_STARVATION_FLOOR_CYCLES
        assert queue.is_empty()
    finally:
        queue.close()
        tt.close()


def test_an_unreachable_starved_cycle_still_does_not_count_toward_the_floor(monkeypatch):
    """The other half of "exactly as before": a starved cycle whose probe says
    unreachable never consulted the gate, so it never moved the counter. With
    N-1 unreachable cycles first, the floor still needs N REACHABLE starved
    cycles. Counting the unreachable ones would force the drain on the first
    reachable cycle."""
    clock = _Clock()
    monkeypatch.setattr(se.time, "monotonic", clock.monotonic)
    tmp = Path(tempfile.mkdtemp())
    queue, tt = _fresh(tmp)
    try:
        _seed_backlog(queue)
        engine, bf, state = _build(queue, tt, clock, reachable=False)
        floor = SyncEngine._DELIVERY_STARVATION_FLOOR_CYCLES
        for _ in range(floor - 1):
            _starved_cycle(engine, bf, clock)
        assert state["probes"] == floor - 1, "control: the starved cycles probed"
        assert engine._consecutive_undelivered_cycles == 0

        state["reachable"] = True
        delivered_on = None
        for cycle in range(1, floor + 2):
            _starved_cycle(engine, bf, clock)
            if bf.send_events.called and delivered_on is None:
                delivered_on = cycle
        assert delivered_on == floor
    finally:
        queue.close()
        tt.close()


def test_the_pure_verdict_moves_nothing():
    """`_drain_gate_verdict` is the side-effect-free half: calling it any number
    of times leaves the floor counter and the stats untouched."""
    tmp = Path(tempfile.mkdtemp())
    queue, tt = _fresh(tmp)
    try:
        engine, _bf, _state = _build(queue, tt, _Clock())
        engine._cycle_start_monotonic = se.time.monotonic() - 10_000  # budget spent
        engine._cycle_delivered = False
        engine._consecutive_undelivered_cycles = 1
        for _ in range(5):
            assert engine._drain_gate_verdict() == "count"
        assert engine._consecutive_undelivered_cycles == 1
        engine._cycle_delivered = True
        assert engine._drain_gate_verdict() == "refuse"
        engine._cycle_start_monotonic = se.time.monotonic()
        assert engine._drain_gate_verdict() == "allow"
        assert engine._consecutive_undelivered_cycles == 1
    finally:
        queue.close()
        tt.close()
