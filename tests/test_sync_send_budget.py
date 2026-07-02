"""Regular-send in-cycle network budget: N buckets must not stack N retry chains.

Origin: 2026-07-02 Azorel outage. betterflow-sync fired "Sync hung — exceeded
120s/150s watchdog deadline" (86x + 24x, fingerprints 707a9ecc / 63a18e4f) plus
one "Sync wedged — held sync lock >420s" (d31bb248), all on the same devices
during a server outage.

Root cause: ``SyncEngine._send_events`` groups the cycle's events BY BUCKET (the
#4 blast-radius decoupling) and sends each group in its own retrying request
chain. A hung server makes each chain take ~94s (max_retries=2 -> 3 attempts *
30s + backoff). A device with N distinct buckets (window, afk, input,
inproc-afk, inproc-window, call ~= 3-6) therefore stacks N * 94s SEQUENTIALLY
inside a single ``sync()``, inside the ``_do_sync`` watchdog. The watchdog budget
(150s deadline / 420s wedge) was sized for ONE regular chain plus the
already-guarded queue drain, not N. N~=3 overruns 150s ("Sync hung"); N~=6
overruns 420s ("Sync wedged") — the SAME root cause, differing only in bucket
count.

Fix: the same elapsed-budget guard that already gates the queue drain
(``_QUEUE_SKIP_IF_CYCLE_ELAPSED``) now gates the per-bucket send loop. Once a
cycle has spent ``_SEND_SKIP_IF_CYCLE_ELAPSED`` on network, the remaining
buckets are queued (durable OfflineQueue drains them next cycle) instead of
starting another chain. The first bucket always attempts, so a cycle still makes
forward progress.

Pre-fix: ``test_over_budget_stops_sending_remaining_buckets`` FAILS — all N
buckets are sent (N send_events calls). Post-fix: only the first is sent, the
rest are queued.
"""

import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from src.config import Config
from src.main import SyncCoordinator
from src.sync.aw_client import BUCKET_TYPE_AFK, BUCKET_TYPE_INPUT, BUCKET_TYPE_WINDOW
from src.sync.bf_client import SyncResult
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.http_client import BaseApiClient
from src.sync.queue import OfflineQueue
from src.sync.retry import calculate_delay
from src.sync.sync_engine import SyncEngine


def _worst_case_chain_seconds() -> float:
    """Upper bound on ONE retrying request whose every attempt eats the full
    timeout (mirrors tests/test_sync_watchdog_budget.py)."""
    cfg = BaseApiClient.DEFAULT_RETRY_CONFIG
    timeout = 30.0
    attempts = cfg.max_retries + 1
    backoff = sum(
        calculate_delay(a, cfg.base_delay, cfg.max_delay, cfg.exponential_base, jitter=False) * 1.25
        for a in range(cfg.max_retries)
    )
    return attempts * timeout + backoff


def _events_across_buckets():
    """One event in each of four distinct buckets, so _send_events forms four
    by-bucket groups (four independent send chains)."""
    now = datetime.now(timezone.utc).isoformat()
    specs = [
        ("aw-watcher-window_h", BUCKET_TYPE_WINDOW),
        ("aw-watcher-afk_h", BUCKET_TYPE_AFK),
        ("aw-watcher-input_h", BUCKET_TYPE_INPUT),
        ("bf-window-inproc_h", BUCKET_TYPE_WINDOW),
    ]
    return [
        {"id": f"e{i}", "timestamp": now, "duration": 1.0,
         "bucket_id": bid, "bucket_type": btype, "data": {}}
        for i, (bid, btype) in enumerate(specs)
    ]


def _engine():
    tmp = Path(tempfile.mkdtemp())
    q = OfflineQueue(db_path=tmp / "q.db", max_size=100000)
    tt = DailyTimeTracker(db_path=tmp / "t.db")
    bf = Mock()
    bf.send_events = Mock(return_value=SyncResult(success=True, events_synced=1))
    engine = SyncEngine(aw=Mock(), bf=bf, queue=q, config=Config(), time_tracker=tt)
    return engine, bf, q, tt


def test_over_budget_stops_sending_remaining_buckets():
    """When the cycle has already spent the send budget, only the first bucket
    group is sent over the network; the rest are queued for the next cycle."""
    engine, bf, q, tt = _engine()
    try:
        # Simulate a cycle that has already burned past the budget (as it would
        # after one ~94s chain against a hung server).
        engine._cycle_start_monotonic = time.monotonic() - (
            SyncEngine._SEND_SKIP_IF_CYCLE_ELAPSED + 5
        )
        from src.sync.sync_engine import SyncStats
        stats = SyncStats()
        engine._send_events(_events_across_buckets(), stats)

        # Exactly ONE network chain (the first group, for forward progress); the
        # other three buckets are queued without a send.
        assert bf.send_events.call_count == 1, (
            f"over-budget cycle must not start a chain per bucket; "
            f"got {bf.send_events.call_count} send_events calls"
        )
        assert stats.events_queued == 3
        assert q.size() == 3
    finally:
        q.close()
        tt.close()


def test_within_budget_sends_all_buckets():
    """A healthy cycle (well under budget) still sends every bucket — no
    regression to the decoupled-bucket delivery."""
    engine, bf, q, tt = _engine()
    try:
        engine._cycle_start_monotonic = time.monotonic()  # elapsed ~= 0
        from src.sync.sync_engine import SyncStats
        stats = SyncStats()
        engine._send_events(_events_across_buckets(), stats)

        assert bf.send_events.call_count == 4, "healthy cycle must send all buckets"
        assert stats.events_queued == 0
        assert q.size() == 0
    finally:
        q.close()
        tt.close()


def test_send_budget_plus_one_chain_under_watchdog():
    """Invariant: the send budget + one worst-case chain must fit under the
    watchdog deadline, so at most one in-flight chain survives the budget cut."""
    budget = SyncEngine._SEND_SKIP_IF_CYCLE_ELAPSED
    worst = _worst_case_chain_seconds()
    deadline = SyncCoordinator._DO_SYNC_DEADLINE
    assert budget + worst < deadline, (
        f"send budget {budget}s + one chain {worst:.1f}s = {budget + worst:.1f}s "
        f"must stay under the {deadline}s watchdog"
    )
