"""End-to-end pipeline test for #4 (decouple buckets) + #5a (monotonic checkpoints).

Drives the REAL ``SyncEngine.sync()`` across multiple cycles with a REAL on-disk
SQLite ``OfflineQueue`` and ``DailyTimeTracker``; only the two external
boundaries are faked (the AW client serves events, the BetterFlow client accepts
window sends but fails afk sends transiently, then recovers). Assertions are made
against the checkpoint state read back off disk between cycles — the same state
the live agent keeps.

Regression context: a transient failure in one bucket (e.g. a frozen/duplicate
AFK stream) used to re-queue the whole mixed batch and withhold every bucket's
checkpoint, stalling unrelated window sync (the doc's "one type failing re-queues
+ backs off everything"). #4 confines the blast radius to the failing bucket; #5a
guarantees a checkpoint can never be dragged backward.
"""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import Config
from src.sync.aw_client import BUCKET_TYPE_AFK, BUCKET_TYPE_WINDOW, AWBucket, AWEvent
from src.sync.bf_client import SyncResult
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.queue import OfflineQueue
from src.sync.sync_engine import SyncEngine

WIN = "aw-watcher-window_smoke"
AFK = "aw-watcher-afk_smoke"


def _bucket(now, bid, btype):
    return AWBucket(id=bid, name=bid, type=btype, client="", hostname="smoke", created=now)


class _FakeAW:
    """Serves one window + one afk event, both ~4 min old (past the checkpoint,
    inside the lookback), filtered by the requested [start, end) window."""

    def __init__(self, now):
        self._now = now
        t = now - timedelta(minutes=4)
        self._events = {
            WIN: [AWEvent(id=1, timestamp=t, duration=60.0,
                          data={"app": "Terminal", "title": "work"})],
            AFK: [AWEvent(id=2, timestamp=t, duration=60.0,
                          data={"status": "not-afk"})],
        }

    def is_running(self):
        return True

    def get_window_buckets(self):
        return [_bucket(self._now, WIN, BUCKET_TYPE_WINDOW)]

    def get_web_buckets(self):
        return []

    def get_afk_buckets(self):
        return [_bucket(self._now, AFK, BUCKET_TYPE_AFK)]

    def get_input_buckets(self):
        return []

    def get_events(self, bid, start=None, end=None, limit=1000):
        return [
            e for e in self._events.get(bid, [])
            if (start is None or e.timestamp >= start)
            and (end is None or e.timestamp < end)
        ]

    def get_events_since(self, bid, since, limit=1000):
        return [e for e in self._events.get(bid, []) if e.timestamp >= since]

    def get_latest_afk_event(self):
        evs = self._events.get(AFK, [])
        return evs[-1] if evs else None


class _FakeBF:
    """Window sends succeed; afk sends fail transiently until ``afk_ok`` flips."""

    def __init__(self):
        self.afk_ok = False
        self.sent_window = 0
        self.sent_afk = 0

    def is_reachable(self):
        return True

    def get_config(self):
        return {}

    def start_session(self):
        return {}

    def end_session(self, reason="app_quit"):
        return {}

    def heartbeat(self, agent_version="0"):
        return {}

    def get_trends(self):
        return {}

    def send_events(self, events):
        is_afk = any("afk" in e.get("bucket_id", "") for e in events)
        if is_afk and not self.afk_ok:
            return SyncResult(success=False, error="transient failure")
        if is_afk:
            self.sent_afk += len(events)
        else:
            self.sent_window += len(events)
        return SyncResult(success=True, events_synced=len(events))


def test_decouple_and_monotonic_checkpoints_end_to_end():
    tmp = Path(tempfile.mkdtemp())
    now = datetime.now(timezone.utc)
    q = OfflineQueue(db_path=tmp / "q.db", max_size=100000)
    tt = DailyTimeTracker(db_path=tmp / "t.db")
    aw, bf = _FakeAW(now), _FakeBF()

    cfg = Config()
    cfg.working_hours.known = True  # known + unrestricted; capture is fail-closed otherwise
    engine = SyncEngine(aw=aw, bf=bf, queue=q, config=cfg, time_tracker=tt)
    # Exercise the STEADY-STATE path: skip the one-time whole-day backlog replay.
    engine._backlog_reconciled = True
    # Suppress the stale-AFK synthesizer: on a genuinely-active CI/dev machine it
    # would add an extra synthetic not-afk span (real, correct behaviour) and
    # muddy the event count this test asserts on. Report "idle" so it stays quiet.
    engine._get_system_idle_seconds = lambda: 9999.0

    # Seed both checkpoints to 10 min ago so the 4-min-old events are fetched.
    t0 = now - timedelta(minutes=10)
    q.set_checkpoint(WIN, t0)
    q.set_checkpoint(AFK, t0)

    try:
        # --- Cycle 1: window OK, afk fails transiently -------------------------
        engine.sync()
        win_cp1, afk_cp1 = q.get_checkpoint(WIN), q.get_checkpoint(AFK)
        # #4: the healthy window bucket advances even though afk failed.
        assert win_cp1 > t0, "window checkpoint must advance despite afk failure"
        # The failed afk bucket is withheld.
        assert afk_cp1 == t0, "afk checkpoint must be withheld on failure"
        # Its event is durably queued, window was delivered.
        assert q.size() == 1
        assert bf.sent_window == 1

        # --- Cycle 2: afk still failing; verify monotonicity -------------------
        engine.sync()
        win_cp2, afk_cp2 = q.get_checkpoint(WIN), q.get_checkpoint(AFK)
        # #5a: neither checkpoint is ever dragged backward.
        assert win_cp2 >= win_cp1, "window checkpoint must be monotonic"
        assert afk_cp2 >= afk_cp1, "afk checkpoint must be monotonic"
        # The afk event is dedup-cached after cycle 1, so its checkpoint may
        # advance — but the undelivered event must still live in the durable
        # queue (no loss); the recovery cycle below delivers it.
        assert q.size() >= 1, "undelivered afk event must not be lost"

        # --- Cycle 3: afk recovers; queue drains -------------------------------
        bf.afk_ok = True
        # Clear the exponential backoff armed by the prior failures so the queue
        # drains this cycle (otherwise _process_queue correctly waits it out).
        engine._queue_backoff_until = datetime.min.replace(tzinfo=timezone.utc)
        engine._queue_consecutive_failures = 0
        engine.sync()
        assert q.size() == 0, "offline queue must drain after recovery"
        assert bf.sent_afk >= 1, "afk event must eventually be delivered"
    finally:
        q.close()
        tt.close()
