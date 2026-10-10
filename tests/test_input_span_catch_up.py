"""A catch-up input event must carry the span its counts cover (F1).

MacOSInputWatcher posts its counters to ActivityWatch every 10 s and, when a
post fails, KEEPS the counters so the next tick re-sends them. Before this fix
the first successful post after an outage was stamped ``timestamp=now,
duration=10`` while carrying the whole outage's counts — so a 15-minute AW
outage while typing at 150/min landed as ONE event of ~2,250 presses inside a
single 5-minute analyzer window (450/min), tripping ``implausible_input_rate``
(+30, sticky until midnight) on a person who merely typed through an outage.

These tests drive the real emitter loop with a scripted clock and a scripted
AW client (no threads, no sleeps — see the memory note on timing tests) and
then feed what it posted into the real ActivityAnalyzer.
"""

from datetime import datetime, timedelta, timezone

from src.config import FraudDetectionConfig
from src.sync import macos_input_watcher as miw
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.aw_client import AWEvent

T0 = datetime(2026, 10, 9, 9, 0, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, now):
        self.now = now


def _install_clock(monkeypatch, clock):
    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now

    monkeypatch.setattr(miw, "datetime", _FakeDatetime)


class _ScriptedAW:
    """post_events fails while ``fail`` is True, records every success."""

    def __init__(self):
        self.fail = False
        self.posted = []
        self.attempts = 0

    def post_events(self, bucket_id, events):
        self.attempts += 1
        if self.fail:
            raise ConnectionError("AW down")
        self.posted.extend(events)


def _press(w, n):
    for _ in range(n):
        w._event_callback(None, miw._kCGEventKeyDown, object(), None)


def _run(monkeypatch, w, clock, script):
    """Run the real _run_emitter. ``script`` is a list of callables, one per
    tick: each runs while the clock is advanced by the emit interval (i.e. it
    is the input typed during that interval). The loop ends after the last."""
    ticks = iter(script)

    def fake_wait(timeout):
        step = next(ticks, None)
        if step is None:
            return True  # stop
        clock.now = clock.now + timedelta(seconds=timeout)
        step()
        return False

    monkeypatch.setattr(w._stop_event, "wait", fake_wait)
    monkeypatch.setattr(w, "_current_frontmost_app", lambda: (None, None))
    w._run_emitter()


def _watcher(aw):
    return miw.MacOSInputWatcher(aw, emit_interval=10.0)


def _aw_event(posted, eid=1):
    return AWEvent.from_dict({**posted, "id": eid})


def test_steady_state_post_keeps_the_ten_second_span(monkeypatch):
    clock = _Clock(T0)
    _install_clock(monkeypatch, clock)
    aw = _ScriptedAW()
    w = _watcher(aw)

    _run(monkeypatch, w, clock, [lambda: _press(w, 25)])

    assert len(aw.posted) == 1
    ev = aw.posted[0]
    assert ev["duration"] == 10.0
    assert ev["timestamp"] == (T0).isoformat(), "span is [now-10s, now]"
    assert ev["data"]["presses"] == 25


def test_retried_post_carries_the_real_outage_span(monkeypatch):
    """15 min of AW outage at 150/min: the recovery post covers the whole
    outage, not the last 10 s."""
    clock = _Clock(T0)
    _install_clock(monkeypatch, clock)
    aw = _ScriptedAW()
    w = _watcher(aw)
    aw.fail = True

    def type_25():
        _press(w, 25)

    def recover_and_type():
        aw.fail = False
        _press(w, 25)

    script = [type_25] * 89 + [recover_and_type]  # 90 ticks = 900 s
    _run(monkeypatch, w, clock, script)

    assert len(aw.posted) == 1, "one catch-up event"
    ev = aw.posted[0]
    assert ev["data"]["presses"] == 25 * 90
    start = datetime.fromisoformat(ev["timestamp"])
    end = clock.now
    # The first count landed during the first interval (T0, T0+10]; the span
    # starts no later than the first count and ends at the successful post.
    assert start <= T0 + timedelta(seconds=10)
    assert start >= T0
    assert ev["duration"] == (end - start).total_seconds()
    assert ev["duration"] >= 890


def test_span_after_idle_does_not_stretch_back_to_the_last_post(monkeypatch):
    """Skip-on-zero means an idle hour posts nothing. Typing after it must not
    be stamped as an hour-long span (that would dilute it to nothing)."""
    clock = _Clock(T0)
    _install_clock(monkeypatch, clock)
    aw = _ScriptedAW()
    w = _watcher(aw)

    idle = [lambda: None] * 360  # one idle hour, nothing posted
    _run(monkeypatch, w, clock, [lambda: _press(w, 25)] + idle + [lambda: _press(w, 30)])

    assert len(aw.posted) == 2
    second = aw.posted[1]
    assert second["data"]["presses"] == 30
    assert second["duration"] == 10.0


def test_outage_catch_up_does_not_trip_the_rate_signal(monkeypatch):
    """End to end: the emitter's catch-up event, fed to the real analyzer with
    default config, must not read as implausible typing."""
    clock = _Clock(T0)
    _install_clock(monkeypatch, clock)
    aw = _ScriptedAW()
    w = _watcher(aw)
    aw.fail = True

    def type_25():
        _press(w, 25)

    def recover_and_type():
        aw.fail = False
        _press(w, 25)

    _run(monkeypatch, w, clock, [type_25] * 89 + [recover_and_type])

    analyzer = ActivityAnalyzer(fraud_config=FraudDetectionConfig())
    analyzer.add_input_events([_aw_event(aw.posted[0])])
    assessment = analyzer.get_fraud_assessment(clock.now, app="Editor")

    assert "implausible_input_rate" not in assessment.signals
    # ...and the counts were not simply thrown away: the window holds its
    # 5-minute share of the 15-minute span (~150/min x 5).
    metrics = analyzer.get_raw_metrics(clock.now)
    assert 700 <= metrics.presses <= 800


def test_counts_arriving_during_a_post_keep_their_span_start(monkeypatch):
    """Keys pressed while a post is in flight are left in the counters by the
    subtract. If AW then goes down, their eventual post must still start at
    that post, not at [now - 10 s]."""
    clock = _Clock(T0)
    _install_clock(monkeypatch, clock)
    aw = _ScriptedAW()
    w = _watcher(aw)

    real_post = aw.post_events

    def post_while_typing(bucket_id, events):
        real_post(bucket_id, events)
        if len(aw.posted) == 1:
            _press(w, 3)  # landed between the snapshot and the subtract

    aw.post_events = post_while_typing

    def go_down():
        aw.fail = True

    def come_back():
        aw.fail = False

    script = [lambda: _press(w, 25), go_down] + [lambda: None] * 29 + [come_back]
    _run(monkeypatch, w, clock, script)

    assert len(aw.posted) == 2
    second = aw.posted[1]
    assert second["data"]["presses"] == 3
    assert datetime.fromisoformat(second["timestamp"]) == T0 + timedelta(seconds=10)
    assert second["duration"] == 310.0  # 31 ticks after the first post
