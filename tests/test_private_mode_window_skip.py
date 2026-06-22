"""Private/pause window must be skipped, not billed (Lucian, 2026-06-22).

Repro: a user sets private time for an hour while actively working. The logo
changed and 'End private time' showed, but the hour was still added to the total.

Root cause: `_advance_checkpoints_to_now` ran only on ENTER (setting the AW
checkpoints to private-START) and never touched the in-process AFK checkpoint.
Because `_sync_bucket` fetches events *since* the checkpoint, the next sync after
leaving private re-fetched the whole private window (active window + not-afk) and
billed it. Private ≠ away, so it shows as ACTIVE time.

Fix: advance ALL checkpoints (AW buckets AND `_afk_inproc_checkpoint`) on LEAVE
(resume from pause / exit private), so the just-recorded private window is skipped.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from src.config import Config
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.afk_source import AfkSource
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.sync_engine import SyncEngine

T0 = datetime(2026, 6, 22, 14, 0, 0, tzinfo=timezone.utc)
WIN_BUCKET = "aw-watcher-window_host"


class _Bucket:
    def __init__(self, bid):
        self.id = bid


class _FakeQueue:
    """Dict-backed checkpoint store — enough to assert what gets skipped."""

    def __init__(self):
        self._cp = {}

    def set_checkpoint(self, bucket_id, ts, event_id=None):
        self._cp[bucket_id] = ts

    def set_checkpoint_forward(self, bucket_id, ts, event_id=None):
        if bucket_id not in self._cp or ts > self._cp[bucket_id]:
            self._cp[bucket_id] = ts

    def get_checkpoint(self, bucket_id):
        return self._cp.get(bucket_id)


def _engine(now_fn):
    cfg = Config()
    cfg.sync.in_process_afk = True
    aw = Mock()
    aw.get_window_buckets.return_value = [_Bucket(WIN_BUCKET)]
    aw.get_web_buckets.return_value = []
    aw.get_afk_buckets.return_value = []
    aw.get_input_buckets.return_value = []
    eng = SyncEngine(aw=aw, bf=Mock(), queue=_FakeQueue(), config=cfg,
                     activity_analyzer=Mock(spec=ActivityAnalyzer),
                     time_tracker=Mock(spec=DailyTimeTracker))
    eng.afk_source = AfkSource(600, "host", idle_clock=lambda: 5.0)
    return eng


def test_private_window_is_skipped_on_leave(monkeypatch):
    import src.sync.sync_engine as se

    clock = {"t": T0}

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["t"]

    monkeypatch.setattr(se, "datetime", _DT)

    eng = _engine(lambda: clock["t"])
    # Steady state just before private: window checkpoint + inproc checkpoint
    # are roughly "now".
    eng.queue.set_checkpoint(WIN_BUCKET, T0 - timedelta(seconds=30))
    eng.afk_source.record_sample(T0)
    eng._build_inproc_afk(T0)  # seed inproc checkpoint at T0

    eng.set_private_mode(True)        # enter at T0
    clock["t"] = T0 + timedelta(hours=1)
    eng.set_private_mode(False)       # leave at T0 + 1h

    leave = T0 + timedelta(hours=1)
    # Both the AW window checkpoint and the in-process AFK checkpoint must have
    # jumped past the private window so the next sync re-fetches NOTHING from it.
    assert eng.queue.get_checkpoint(WIN_BUCKET) >= leave, "AW window window leaked"
    assert eng._afk_inproc_checkpoint >= leave, "in-process AFK window leaked"


def test_pause_window_is_skipped_on_resume(monkeypatch):
    # Same root cause as private: a manual break where the user keeps typing
    # would otherwise re-sync as active on resume. pause()'s contract is "drop
    # buffered events until resume", so the window must be skipped on resume.
    import src.sync.sync_engine as se

    clock = {"t": T0}

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["t"]

    monkeypatch.setattr(se, "datetime", _DT)

    eng = _engine(lambda: clock["t"])
    eng.queue.set_checkpoint(WIN_BUCKET, T0 - timedelta(seconds=30))
    eng.afk_source.record_sample(T0)
    eng._build_inproc_afk(T0)

    eng.pause()                       # enter at T0
    clock["t"] = T0 + timedelta(minutes=20)
    eng.resume()                      # leave at T0 + 20m

    leave = T0 + timedelta(minutes=20)
    assert eng.queue.get_checkpoint(WIN_BUCKET) >= leave, "AW window leaked on resume"
    assert eng._afk_inproc_checkpoint >= leave, "in-process AFK leaked on resume"
