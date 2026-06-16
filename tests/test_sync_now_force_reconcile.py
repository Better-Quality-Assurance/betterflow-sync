"""Manual "Sync Now" must re-arm the start-of-day backlog reconcile.

The reconcile (rewind checkpoints to start-of-day, re-send events the server
never received) is otherwise once-per-process — it only runs at startup, so
recovering a stuck day used to require a quit+restart. A user clicking
"Sync Now" reasonably expects it to push everything not yet in prod, so the
manual path calls request_backlog_reconcile() to re-arm it.
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock

from src.config import Config
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.queue import OfflineQueue
from src.sync.sync_engine import SyncEngine


def _engine(tmp: Path) -> SyncEngine:
    return SyncEngine(
        aw=Mock(),
        bf=Mock(),
        queue=OfflineQueue(db_path=tmp / "q.db", max_size=1000),
        config=Config(),
        time_tracker=DailyTimeTracker(db_path=tmp / "t.db"),
    )


def test_request_backlog_reconcile_rearms_the_once_per_process_flag():
    tmp = Path(tempfile.mkdtemp())
    engine = _engine(tmp)
    try:
        # Fresh process: reconcile is armed (fires on the first sync).
        assert engine._backlog_reconciled is False

        # Simulate the first sync having consumed it (the normal periodic sync
        # would now never reconcile again for the life of the process).
        engine._backlog_reconciled = True

        # Manual "Sync Now" re-arms it so the NEXT sync replays the day's
        # backlog — recovering stranded events without a restart.
        engine.request_backlog_reconcile()
        assert engine._backlog_reconciled is False
    finally:
        engine.queue.close()
        engine._time_tracker.close()
