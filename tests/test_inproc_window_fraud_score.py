"""TASK-002178 item 3: with ``in_process_window`` on, the agent builds its own
per-app window events (``_build_inproc_window``). Those went to upload as raw
dicts and never passed through the transform that classifies activity and
attaches the client fraud score, so those devices uploaded window events with no
``fraud_score``. They now take the same path as ActivityWatch window events.

Event ids change with this: the shared transform ids a window event
``<id>:<segment>``, so ``win-inproc_<host>_<ms>`` becomes
``win-inproc_<host>_<ms>:0``.

Drives a full ``SyncEngine.sync()`` cycle with a REAL ActivityAnalyzer.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock

from src.config import Config
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.aw_client import BUCKET_TYPE_WINDOW
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.input_source import InputSource
from src.sync.sync_engine import SyncEngine
from src.sync.window_source import WindowSource

INPROC_BUCKET = "bf-window-inproc_host"


class _FakeBackend:
    def available(self):
        return True

    def start(self):
        return True

    def stop(self):
        pass


def _engine(samples):
    """Windows-shaped config: in-process input AND in-process window on.

    ``samples`` is a list of (seconds_ago, app, title) frontmost samples, so the
    window source has a real span to rebuild between its checkpoint and now."""
    now = datetime.now(timezone.utc)
    cfg = Config()
    cfg.working_hours.known = True
    cfg.sync.in_process_input = True
    cfg.sync.in_process_window = True
    cfg.sync.in_process_afk = False

    aw = Mock()
    aw.is_running.return_value = True
    aw.get_window_buckets.return_value = []
    aw.get_web_buckets.return_value = []
    aw.get_afk_buckets.return_value = []
    aw.get_input_buckets.return_value = []
    aw.get_events_since.return_value = []
    aw.get_events.return_value = []

    bf = Mock()
    bf.is_reachable.return_value = True
    queue = MagicMock()
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
    eng.window_source = WindowSource(hostname="host",
                                     foreground_getter=lambda: ("Editor", "x"))
    with eng.window_source._lock:
        for secs_ago, app, title in samples:
            eng.window_source._samples.append((now - timedelta(seconds=secs_ago), app, title))
    # One cycle already ran: both in-process checkpoints are seeded 60 s back.
    eng._input_inproc_checkpoint = now - timedelta(seconds=60)
    eng._window_inproc_checkpoint = now - timedelta(seconds=60)

    sent: list = []

    def capture(events, *a, **k):
        sent.extend(events)

    eng._send_and_advance_checkpoints = capture
    return eng, sent


def _inproc_windows(sent):
    return [e for e in sent if e.get("bucket_id") == INPROC_BUCKET]


def test_inproc_window_events_carry_the_client_fraud_score():
    eng, sent = _engine([(50, "Editor", "report.docx"), (30, "Editor", "report.docx")])
    eng.input_source._on_press(40)
    eng.input_source._on_click(5)

    eng.sync()

    windows = _inproc_windows(sent)
    assert windows, f"no in-process window event uploaded: {sent!r}"
    for ev in windows:
        assert ev["bucket_type"] == BUCKET_TYPE_WINDOW
        assert isinstance(ev.get("fraud_score"), (int, float)), ev
        assert "fraud_signals" in ev, ev
        assert "activity_state" in ev, ev
        assert ev["activity_metrics"]["presses"] > 0, ev["activity_metrics"]


def test_inproc_window_event_ids_are_the_transform_ids():
    """The id is ``<source id>:<segment>``: the same shape every other window
    event has. Pinning it makes the id change (and so the one-off duplicate
    rows for the span in flight at upgrade) a deliberate, visible contract."""
    eng, sent = _engine([(50, "Editor", "a"), (30, "Editor", "a")])
    eng.input_source._on_press(10)

    eng.sync()

    windows = _inproc_windows(sent)
    assert windows
    for ev in windows:
        assert ev["id"].startswith("win-inproc_host_"), ev["id"]
        assert ev["id"].endswith(":0"), ev["id"]


def test_an_excluded_app_never_leaves_the_machine_via_the_inproc_window_stream():
    eng, sent = _engine([(50, "SecretVault", "passwords"), (30, "SecretVault", "passwords")])
    eng.config.privacy.exclude_apps = ["SecretVault"]
    eng.input_source._on_press(10)

    eng.sync()

    assert not [e for e in _inproc_windows(sent) if e["data"].get("app") == "SecretVault"], sent


def test_a_one_sample_inproc_span_is_not_dropped_by_the_flicker_filter():
    """The in-process sampler ticks every 5 s, which is exactly the AW flicker
    filter's 5 s minimum, so an app frontmost for one sample yields a ~5 s span
    and jitter puts about half of them at 4.99 s. The filter exists for
    ActivityWatch's sub-second heartbeat fragments; applied here it dropped
    real focus (review finding on PR #299). Samples: Editor, Slack 4.99 s later,
    Editor 4.99 s after that."""
    eng, sent = _engine([(50, "Editor", "a"), (45.01, "Slack", "b"),
                         (40.02, "Editor", "a"), (30, "Editor", "a")])
    eng.input_source._on_press(10)

    eng.sync()

    apps = [e["data"].get("app") for e in _inproc_windows(sent)]
    assert "Slack" in apps, [(e["data"].get("app"), e["duration"]) for e in _inproc_windows(sent)]


def test_a_cycle_boundary_tail_under_5s_is_uploaded():
    """A span cut by the cycle boundary is an artifact of the cut, not a
    flicker; the next cycle builds only from samples after the checkpoint, so a
    dropped tail is lost for good."""
    eng, sent = _engine([(50, "Editor", "a"), (3, "Browser", "t")])
    eng.input_source._on_press(10)

    eng.sync()

    assert any(e["data"].get("app") == "Browser" for e in _inproc_windows(sent)), \
        [(e["data"].get("app"), e["duration"]) for e in _inproc_windows(sent)]


def test_the_aw_window_flicker_filter_still_applies_when_a_window_source_is_wired():
    """main.py wires a WindowSource on every device, flag on or off, so an
    exemption keyed on "window_source is not None" instead of the bucket id
    would strip the 5 s flicker filter fleet-wide (round-2 review mutant M3)."""
    from src.sync.aw_client import AWEvent

    eng, _ = _engine([])
    assert eng.window_source is not None
    short = AWEvent.from_dict({
        "id": 1, "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration": 3.0, "data": {"app": "Editor", "title": "a"},
    })
    assert eng._transform_event(short, "aw-watcher-window_host", BUCKET_TYPE_WINDOW) is None
    assert eng._transform_event(short, INPROC_BUCKET, BUCKET_TYPE_WINDOW) is not None
