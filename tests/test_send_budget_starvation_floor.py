"""The per-cycle network budget must never freeze delivery permanently.

``_CYCLE_NETWORK_BUDGET_SECONDS`` (50s) gates every retrying request chain a
cycle can start, so N chains can't stack past the ``_do_sync`` watchdog. Gating
the per-bucket SEND loop as well as the drain closed a real watchdog overrun —
but it removed the last unconditional delivery from the cycle, and nothing
bounded how long the gate could keep saying no.

The failing sequence, and it is not exotic: the backend degrades on
``/session/start`` only while ``/events/batch`` stays healthy. ``start_session``
raises, ``_session_active`` stays False, so ``need_session`` is True every cycle
and each cycle burns a ~94s retry chain there before reaching either delivery
gate. Both then see elapsed >= 50s. The cycle sends nothing AND drains nothing,
forever, while the heartbeat stays green — the "alive but uploads frozen" mode
this repo has an incident file for. Events accumulate until ``max_size``, and
oldest-eviction starts destroying billable time.

These tests pin BOTH halves of the property, which is the point:

* the budget gate still holds for the first cycles (deleting it is not a fix),
* and delivery still happens within a bounded number of cycles.
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import src.sync.sync_engine as se
from src.config import Config
from src.sync.bf_client import SyncResult
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.http_client import BetterFlowClientError
from src.sync.queue import OfflineQueue
from src.sync.sync_engine import SyncEngine

#: One full retry chain against a hung endpoint, per the constant's own comment.
HUNG_CHAIN_SECONDS = 94.0


class _SessionStartOutage:
    """Backend degraded on /session/start only; /events/batch is healthy.

    Each start_session attempt raises AND burns a full retry chain off the
    cycle's shared network budget — which is the whole mechanism.
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


def _build(queue: OfflineQueue, tt: DailyTimeTracker, outage: _SessionStartOutage):
    aw = Mock()
    aw.is_running.return_value = True
    # No new capture this cycle: the backlog already in the queue is what has to
    # get out. Keeps the test on the delivery gates, not the fetch path.
    aw.get_window_buckets.return_value = []
    aw.get_web_buckets.return_value = []
    aw.get_afk_buckets.return_value = []
    aw.get_input_buckets.return_value = []

    bf = Mock()
    bf.is_reachable.return_value = True
    bf.start_session.side_effect = outage.start_session
    bf.send_events.side_effect = lambda batch: SyncResult(
        success=True, events_synced=len(batch)
    )

    config = Config()
    config.working_hours.known = True
    engine = SyncEngine(aw=aw, bf=bf, queue=queue, config=config, time_tracker=tt)
    engine._config_fetched = True
    engine._backlog_reconciled = True
    return engine, bf


def _seed_backlog(queue: OfflineQueue, count: int = 12) -> None:
    now = datetime.now(timezone.utc)
    queue.enqueue([
        {"id": f"billable-{i}", "bucket_id": "aw-watcher-afk_h",
         "timestamp": (now - timedelta(minutes=i + 1)).isoformat(),
         "duration": 60, "data": {"status": "not-afk"}}
        for i in range(count)
    ])


def test_session_start_outage_cannot_freeze_delivery_forever(monkeypatch, caplog):
    """Delivery must resume within a bounded number of cycles.

    Pre-fix this never delivered: the send loop queued everything and the drain
    gate refused every cycle, so bf.send_events was never called no matter how
    many cycles ran.
    """
    import logging

    tmp = Path(tempfile.mkdtemp())
    queue = OfflineQueue(db_path=tmp / "q.db", max_size=1000)
    tt = DailyTimeTracker(db_path=tmp / "t.db")
    outage = _SessionStartOutage()
    monkeypatch.setattr(se.time, "monotonic", outage.monotonic)
    try:
        _seed_backlog(queue)
        engine, bf = _build(queue, tt, outage)

        delivered_on_cycle = None
        with caplog.at_level(logging.WARNING, logger="src.sync.sync_engine"):
            for cycle in range(1, 9):
                # Each cycle is a fresh 50s budget; only start_session burns it.
                engine._queue_backoff_until = datetime.min.replace(tzinfo=timezone.utc)
                engine.sync()
                if bf.send_events.called and delivered_on_cycle is None:
                    delivered_on_cycle = cycle

        assert outage.session_attempts >= 8, (
            "the mechanism requires start_session to be retried every cycle; "
            f"it was attempted {outage.session_attempts} times"
        )
        assert delivered_on_cycle is not None, (
            "uploads are frozen permanently: the send budget blocked both the "
            "send loop and the drain on every cycle, so no captured event ever "
            "reached the server while the heartbeat stayed green"
        )
        # Exactly at the floor: not earlier (the gate would be neutered) and not
        # later (the floor would not be doing its job).
        assert delivered_on_cycle == SyncEngine._DELIVERY_STARVATION_FLOOR_CYCLES
        assert queue.is_empty(), "the backlog must actually clear, not just start"

        # And it must be visible. A cycle deliberately overrunning the watchdog
        # is something an operator has to be able to find, and it names the real
        # fault (something ahead of the upload is eating the whole budget).
        engaged = [
            r for r in caplog.records
            if "starvation floor engaged" in r.getMessage()
        ]
        assert engaged, (
            "the floor engaged silently; the resulting watchdog overrun would "
            f"look like an unexplained hang. Records: "
            f"{[r.getMessage()[:60] for r in caplog.records]}"
        )
        assert engaged[0].levelno >= logging.WARNING
    finally:
        queue.close()
        tt.close()


def test_the_budget_gate_still_defers_the_early_cycles(monkeypatch):
    """The other half of the property, so this cannot be 'fixed' by deleting the
    gate. The floor is a floor, not an exemption: the first cycles must still
    defer, or the watchdog overrun the gate exists to prevent comes straight
    back."""
    tmp = Path(tempfile.mkdtemp())
    queue = OfflineQueue(db_path=tmp / "q.db", max_size=1000)
    tt = DailyTimeTracker(db_path=tmp / "t.db")
    outage = _SessionStartOutage()
    monkeypatch.setattr(se.time, "monotonic", outage.monotonic)
    try:
        _seed_backlog(queue)
        engine, bf = _build(queue, tt, outage)

        engine._queue_backoff_until = datetime.min.replace(tzinfo=timezone.utc)
        engine.sync()

        assert not bf.send_events.called, (
            "a cycle that has already spent the network budget must defer its "
            "drain — starting a second ~94s chain is the watchdog overrun the "
            "budget exists to prevent"
        )
        assert queue.size() == 12
    finally:
        queue.close()
        tt.close()


def test_a_cycle_that_delivered_does_not_count_toward_the_floor(monkeypatch):
    """The floor must only fire on STARVATION. A cycle where the budget was
    spent but delivery still happened is the gate working as designed, so it
    must reset the counter — otherwise a merely slow-but-healthy agent would be
    forced into a second chain every few cycles for no reason."""
    tmp = Path(tempfile.mkdtemp())
    queue = OfflineQueue(db_path=tmp / "q.db", max_size=1000)
    tt = DailyTimeTracker(db_path=tmp / "t.db")
    outage = _SessionStartOutage()
    monkeypatch.setattr(se.time, "monotonic", outage.monotonic)
    try:
        _seed_backlog(queue)
        engine, bf = _build(queue, tt, outage)

        # Stand in for a healthy regular send earlier in the same cycle: the
        # budget is spent, but the cycle DID put events on the wire. sync()
        # clears the per-cycle flag at the top, so it has to be marked from
        # inside the cycle.
        def burn_and_deliver():
            outage.now += HUNG_CHAIN_SECONDS
            outage.session_attempts += 1
            engine._note_delivery_attempt()
            raise BetterFlowClientError("session start 503")

        bf.start_session.side_effect = burn_and_deliver

        for _ in range(10):
            engine._queue_backoff_until = datetime.min.replace(tzinfo=timezone.utc)
            engine.sync()

        assert not bf.send_events.called, (
            "the floor fired on a cycle that had already delivered; it must "
            "track starvation, not merely a spent budget"
        )
    finally:
        queue.close()
        tt.close()
