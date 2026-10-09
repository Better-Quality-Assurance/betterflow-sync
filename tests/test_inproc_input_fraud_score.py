"""F2: the in-process input stream must reach the fraud analyzer.

On Windows (``in_process_input`` platform default) the agent counts input
in-process and drains it into one upload event per cycle. That event went to
upload only; the analyzer was fed exclusively from the external AW input
bucket, which Windows does not have. So ``has_input_data`` stayed False and no
window event ever carried a client ``fraud_score``.

These drive a full ``SyncEngine.sync()`` cycle with a REAL ActivityAnalyzer.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from src.config import Config
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.aw_client import AWEvent, BUCKET_TYPE_INPUT, BUCKET_TYPE_WINDOW
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.input_source import InputSource
from src.sync.sync_engine import SyncEngine


class _FakeBackend:
    def available(self):
        return True

    def start(self):
        return True

    def stop(self):
        pass


def _bucket(bucket_id, btype):
    b = Mock()
    b.id = bucket_id
    b.type = btype
    return b


def _engine(*, external_input_events=None):
    """Windows-shaped config: in-process input on, external window tracker."""
    now = datetime.now(timezone.utc)
    cfg = Config()
    cfg.working_hours.known = True
    cfg.sync.in_process_input = True
    cfg.sync.in_process_window = False
    cfg.sync.in_process_afk = False

    calls = {"n": 0}

    def window_event():
        # A fresh event (new id, current time) per fetch, as a live tracker
        # produces one cycle to the next.
        calls["n"] += 1
        t = datetime.now(timezone.utc)
        return AWEvent(
            id=10 + calls["n"], timestamp=t - timedelta(seconds=40), duration=30.0,
            data={"app": "Editor", "title": f"report-{calls['n']}.docx"},
        )

    aw = Mock()
    aw.is_running.return_value = True
    aw.get_window_buckets.return_value = [_bucket("aw-watcher-window_host", BUCKET_TYPE_WINDOW)]
    aw.get_web_buckets.return_value = []
    aw.get_afk_buckets.return_value = []
    if external_input_events is None:
        aw.get_input_buckets.return_value = []
    else:
        aw.get_input_buckets.return_value = [_bucket("aw-watcher-input_host", BUCKET_TYPE_INPUT)]
    aw.get_events_since.return_value = list(external_input_events or [])

    def get_events(bucket_id, *a, **k):
        return [window_event()] if bucket_id.startswith("aw-watcher-window") else []

    aw.get_events.side_effect = get_events

    bf = Mock()
    bf.is_reachable.return_value = True

    queue = Mock()
    queue.get_checkpoint.return_value = now - timedelta(minutes=5)
    queue.is_empty.return_value = True

    tracker = Mock(spec=DailyTimeTracker)
    tracker.get_today_active_time.return_value = timedelta(hours=1)

    analyzer = ActivityAnalyzer()
    eng = SyncEngine(aw=aw, bf=bf, queue=queue, config=cfg,
                     activity_analyzer=analyzer, time_tracker=tracker)
    eng._config_fetched = True
    eng._backlog_reconciled = True
    eng.input_source = InputSource(hostname="host", backend=_FakeBackend(),
                                   frontmost_app_getter=None)
    # One cycle already ran: the in-process checkpoint is seeded 60 s back.
    eng._input_inproc_checkpoint = now - timedelta(seconds=60)

    sent: list = []

    def capture(events, *a, **k):
        sent.extend(events)

    eng._send_and_advance_checkpoints = capture
    return eng, analyzer, sent


def _window_events(sent):
    return [e for e in sent if e.get("bucket_type") in (BUCKET_TYPE_WINDOW,)]


def test_windows_cycle_with_inproc_input_attaches_fraud_score():
    eng, _analyzer, sent = _engine()
    eng.input_source._on_press(40)
    eng.input_source._on_click(5)

    eng.sync()

    windows = _window_events(sent)
    assert windows, f"no window event uploaded: {sent!r}"
    for ev in windows:
        assert "fraud_score" in ev, ev
        assert "activity_metrics" in ev
    # ...and the drained input event still uploads exactly as before.
    inputs = [e for e in sent if e.get("bucket_type") == BUCKET_TYPE_INPUT]
    assert len(inputs) == 1 and inputs[0]["data"]["presses"] == 40


def test_analyzer_sees_the_inproc_counts_not_the_external_bucket():
    """Exclusivity: with the in-process source active the external input
    bucket is not uploaded — and must not be analysed either, or the same
    keystrokes would be counted twice where both exist."""
    now = datetime.now(timezone.utc)
    external = [AWEvent(id=7, timestamp=now - timedelta(seconds=30), duration=10.0,
                        data={"presses": 999, "clicks": 0, "scrolls": 0})]
    eng, analyzer, sent = _engine(external_input_events=external)
    eng.input_source._on_press(40)

    eng.sync()

    ids = {e.id for e in analyzer._input_events}
    assert 7 not in ids, "external bucket fed to the analyzer while skipped for upload"
    assert any(str(i).startswith("input-inproc_") for i in ids)
    assert not [e for e in sent if e.get("bucket_id") == "aw-watcher-input_host"]


def test_external_bucket_still_analysed_when_inproc_is_off():
    now = datetime.now(timezone.utc)
    external = [AWEvent(id=7, timestamp=now - timedelta(seconds=30), duration=10.0,
                        data={"presses": 30, "clicks": 0, "scrolls": 0})]
    eng, analyzer, _sent = _engine(external_input_events=external)
    eng.config.sync.in_process_input = False

    eng.sync()

    assert {e.id for e in analyzer._input_events} == {7}


def test_a_quiet_cycle_after_inproc_input_still_classifies():
    """Parity with the external bucket, which reports input data whenever ANY
    input event falls in the lookback: one cycle with no keystrokes (reading)
    must not drop the window events back to an unclassified, unscored
    'active' while the previous cycle's counts are still in the analyzer."""
    eng, _analyzer, sent = _engine()
    eng.input_source._on_press(40)
    eng.sync()

    sent.clear()
    eng.sync()  # nothing typed this cycle

    windows = _window_events(sent)
    assert windows
    for ev in windows:
        assert "fraud_score" in ev, ev
