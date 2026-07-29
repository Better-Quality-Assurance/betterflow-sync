"""Work done just BEFORE a pause must survive the pause.

`_advance_checkpoints_to_now` exists to make sure a private/break/lock window is
never recorded, and it does that by jumping every checkpoint to `now`. Its own
docstring conceded the side effect: "the enter call only drops pre-window
buffered events".

Those events are not part of the excluded window. They are seconds the person
actually worked in the up-to-60s since the last fetch — right before they locked
the screen, started a break, or turned Private Time on. Every screen lock, every
break and every Private Time toggle threw them away, on every device.

The tail is now queued before the checkpoint moves, using the same
fetch-transform-enqueue path as `_reconcile_backlog` (backend upserts by AW event
id, window events dedup through the counted-time cache, non-window events pass
skip_time_tracking) so it cannot double-count.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from src.config import Config
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.afk_source import AfkSource
from src.sync.aw_client import AWEvent
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.sync_engine import SyncEngine

T0 = datetime(2026, 7, 24, 11, 0, 0, tzinfo=timezone.utc)
LAST_FETCH = T0 - timedelta(seconds=45)          # a normal mid-cycle moment
AFK_BUCKET = "aw-watcher-afk_host"


class _Bucket:
    def __init__(self, bid, btype):
        self.id = bid
        self.type = btype


class _FakeQueue:
    def __init__(self):
        self._cp = {}
        self.enqueued = []

    def set_checkpoint(self, bucket_id, ts, event_id=None):
        self._cp[bucket_id] = ts

    def set_checkpoint_forward(self, bucket_id, ts, event_id=None):
        if bucket_id not in self._cp or ts > self._cp[bucket_id]:
            self._cp[bucket_id] = ts

    def get_checkpoint(self, bucket_id):
        return self._cp.get(bucket_id)

    def enqueue(self, events):
        self.enqueued.extend(events)
        return len(events)


def _engine(tail_events):
    cfg = Config()
    aw = Mock()
    bucket = _Bucket(AFK_BUCKET, "afkstatus")
    aw.get_afk_buckets.return_value = [bucket]
    aw.get_window_buckets.return_value = []
    aw.get_web_buckets.return_value = []
    aw.get_input_buckets.return_value = []
    aw.get_events.return_value = list(tail_events)
    eng = SyncEngine(aw=aw, bf=Mock(), queue=_FakeQueue(), config=cfg,
                     activity_analyzer=Mock(spec=ActivityAnalyzer),
                     time_tracker=Mock(spec=DailyTimeTracker))
    eng.afk_source = AfkSource(600, "host", idle_clock=lambda: 5.0)
    return eng


@pytest.fixture
def frozen(monkeypatch):
    import src.sync.sync_engine as se

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return T0

    monkeypatch.setattr(se, "datetime", _DT)
    return T0


def _tail():
    return [AWEvent(id=1, timestamp=LAST_FETCH + timedelta(seconds=5),
                duration=40.0, data={"status": "not-afk"})]


def test_a_manual_pause_queues_the_work_done_before_it(frozen):
    eng = _engine(_tail())
    eng.queue.set_checkpoint(AFK_BUCKET, LAST_FETCH)

    eng.pause()

    assert eng.queue.enqueued, (
        "the 45 seconds worked before the pause were discarded, not queued"
    )


def test_private_time_queues_the_work_done_before_it(frozen):
    eng = _engine(_tail())
    eng.queue.set_checkpoint(AFK_BUCKET, LAST_FETCH)

    eng.set_private_mode(True)

    assert eng.queue.enqueued


def test_the_flush_only_reaches_up_to_the_boundary(frozen):
    # It must never fetch past the pause boundary — that is the window the
    # caller is excluding, and uploading it would break the Private Time
    # contract outright.
    eng = _engine(_tail())
    eng.queue.set_checkpoint(AFK_BUCKET, LAST_FETCH)

    eng.pause()

    _, kwargs = eng.aw.get_events.call_args
    assert kwargs["start"] == LAST_FETCH
    assert kwargs["end"] == T0


def test_leaving_private_time_does_NOT_flush(frozen):
    # On the way out `now` is the END of the window. Flushing there would upload
    # the private span itself. This is the single most important case here.
    eng = _engine(_tail())
    eng.queue.set_checkpoint(AFK_BUCKET, LAST_FETCH)
    eng.set_private_mode(True)
    eng.queue.enqueued.clear()
    eng.aw.get_events.reset_mock()

    eng.set_private_mode(False)

    assert eng.queue.enqueued == [], "leaving private time must not upload the window"
    eng.aw.get_events.assert_not_called()


def test_resuming_from_a_pause_does_NOT_flush(frozen):
    eng = _engine(_tail())
    eng.queue.set_checkpoint(AFK_BUCKET, LAST_FETCH)
    eng.pause()
    eng.queue.enqueued.clear()
    eng.aw.get_events.reset_mock()

    eng.resume()

    assert eng.queue.enqueued == []
    eng.aw.get_events.assert_not_called()


def test_the_checkpoint_still_advances_past_the_window(frozen):
    # The whole point of the method must survive the addition.
    eng = _engine(_tail())
    eng.queue.set_checkpoint(AFK_BUCKET, LAST_FETCH)

    eng.pause()

    assert eng.queue.get_checkpoint(AFK_BUCKET) == T0


def test_a_failing_flush_never_blocks_the_pause(frozen):
    # This runs on the tray thread inside pause(). If ActivityWatch is hung the
    # pause must still take effect — degrading to the old behaviour, not worse.
    eng = _engine(_tail())
    eng.queue.set_checkpoint(AFK_BUCKET, LAST_FETCH)
    eng.aw.get_events.side_effect = RuntimeError("AW is hung")

    eng.pause()

    assert eng.is_paused is True
    assert eng.queue.get_checkpoint(AFK_BUCKET) == T0


def test_nothing_is_fetched_when_the_checkpoint_is_already_current(frozen):
    # No tail to rescue: don't spend a localhost round-trip on every lock.
    eng = _engine(_tail())
    eng.queue.set_checkpoint(AFK_BUCKET, T0)

    eng.pause()

    eng.aw.get_events.assert_not_called()
