"""Both input senders stamp held counts with ONE rule (src/sync/input_span.py).

PR #302 review, two findings:

1. MacOSInputWatcher, on input after a long silence, moved the held span's start
   to "now" and KEPT every held count. Counts held through an ActivityWatch
   outage were then crammed into a 10 s span: 541 presses over 10 s is
   3246/min against a 400/min limit, which trips ``implausible_input_rate``
   (+30 fraud score, sticky until midnight) on someone who just took a break.
2. The in-process sender (``SyncEngine._build_inproc_input`` ->
   ``InputSource.drain_input_events``; the Windows default) still stamped
   [checkpoint, now], so a press before a 68 h sleep was uploaded as a 68 h
   input event, which the server bills as active time.

The rule now lives in ``HeldInputSpans``: held counts are segments with first
and last count instants; a silence longer than the configured AFK timeout closes
a segment [first, last + pad] and the next count opens a new one. Counts never
move between segments. Both senders call it; the guards at the bottom fail if
either stops doing so.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.config import Config, FraudDetectionConfig
from src.sync import input_span
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.input_source import InputSource
from src.sync.input_span import HeldInputSpans
from tests.test_input_span_catch_up import (
    T0,
    _aw_event,
    _Clock,
    _install_clock,
    _press,
    _run,
    _ScriptedAW,
    _watcher,
)
from tests.test_sync_engine_inproc_input import _engine as _bare_engine


def _span(ev):
    start = datetime.fromisoformat(ev["timestamp"])
    return start, start + timedelta(seconds=ev["duration"])


# -- 1. macOS emitter: a break during an AW outage --------------------------


def _outage_break(monkeypatch, per_tick=10, typing_ticks=54):
    """AW down; ``per_tick`` presses per 10 s for ``typing_ticks`` ticks
    (default 1 press/s for 9 min = 540); a 15 min break, AW still down; one
    press; AW recovers on the next tick. Returns (aw, marks)."""
    clock = _Clock(T0)
    _install_clock(monkeypatch, clock)
    aw = _ScriptedAW()
    w = _watcher(aw)
    aw.fail = True
    marks = {}

    def type_10():  # one tick = 10 s at 1 press/s
        marks.setdefault("from", clock.now)
        _press(w, per_tick)
        marks["to"] = clock.now

    def quiet():
        pass

    def wake_press():
        marks["wake"] = clock.now
        _press(w, 1)

    def recover():
        aw.fail = False

    script = [type_10] * typing_ticks + [quiet] * 90 + [wake_press, recover]
    _run(monkeypatch, w, clock, script)
    marks["break_mid"] = marks["to"] + timedelta(minutes=7, seconds=30)
    return aw, marks


def test_outage_break_does_not_trip_the_rate_signal(monkeypatch):
    """1 h at 150/min, then the break. The analyzer rates per 5-min window, so
    the brief's 541-press repro (~108/min per window) never tripped it client
    side; 9000 presses relocated into 10 s do (1800/min)."""
    aw, marks = _outage_break(monkeypatch, per_tick=25, typing_ticks=360)
    assert sum(ev["data"]["presses"] for ev in aw.posted) == 9001
    analyzer = ActivityAnalyzer(fraud_config=FraudDetectionConfig())
    analyzer.add_input_events([_aw_event(ev, eid=i) for i, ev in enumerate(aw.posted)])
    a = analyzer.get_fraud_assessment(marks["wake"] + timedelta(seconds=10), app="Editor")
    assert "implausible_input_rate" not in a.signals, (a.signals, aw.posted)


def test_outage_break_keeps_every_count_once_in_its_own_segment(monkeypatch):
    aw, marks = _outage_break(monkeypatch)
    assert sum(ev["data"]["presses"] for ev in aw.posted) == 541
    assert len(aw.posted) == 2, aw.posted
    typed, wake = aw.posted
    assert typed["data"]["presses"] == 540
    t_start, t_end = _span(typed)
    # The segment spans the real typing period, not the last 10 s.
    assert t_start <= marks["from"] and t_end >= marks["to"], typed
    assert t_end <= marks["to"] + timedelta(seconds=10), "ends at the last press + one interval"
    assert wake["data"]["presses"] == 1
    w_start, w_end = _span(wake)
    assert w_start <= marks["wake"] <= w_end
    for ev in aw.posted:
        s, e = _span(ev)
        assert not (s < marks["break_mid"] < e), f"an event covers the break: {ev}"


# -- 2. in-process sender: the 68 h sleep -----------------------------------


SLEEP = timedelta(hours=68, minutes=14)


class _Backend:
    def available(self):
        return True

    def start(self):
        return True

    def stop(self):
        pass


def _inproc_engine(max_silence_s=600.0):
    eng = _bare_engine(True)
    eng.input_source = InputSource(
        hostname="host", backend=_Backend(), frontmost_app_getter=None,
        max_silence_s=max_silence_s,
    )
    return eng


def test_inproc_press_before_a_sleep_is_not_stamped_across_it():
    """Reviewer's probe: checkpoint seeded, one press, first cycle 68h14m later
    produced duration 245640 s with presses=1."""
    eng = _inproc_engine()
    assert eng._build_inproc_input(T0) == ([], None)  # seed
    press_at = T0 + timedelta(seconds=20)
    eng.input_source._on_press(1, now=press_at)
    events, pending = eng._build_inproc_input(T0 + SLEEP)
    assert pending == T0 + SLEEP
    assert len(events) == 1, events
    [ev] = events
    assert ev["data"]["presses"] == 1
    start, end = _span(ev)
    assert start <= press_at <= end
    assert ev["duration"] <= 60, f"in-process span bridges the sleep: {ev['duration']} s"


def test_inproc_wake_press_is_not_stamped_back_to_the_checkpoint():
    eng = _inproc_engine()
    eng._build_inproc_input(T0)
    wake = T0 + SLEEP
    eng.input_source._on_press(1, now=wake)
    [ev] = eng._build_inproc_input(wake + timedelta(seconds=3))[0]
    assert ev["data"]["presses"] == 1
    assert ev["duration"] <= 10.0, f"wake keypress bridges the sleep: {ev['duration']} s"


def test_inproc_press_sleep_wake_press_are_two_segments_and_nothing_spans_the_sleep():
    eng = _inproc_engine()
    eng._build_inproc_input(T0)
    eng.input_source._on_press(3, now=T0 + timedelta(seconds=5))
    eng.input_source._on_press(2, now=T0 + SLEEP)
    events, _ = eng._build_inproc_input(T0 + SLEEP + timedelta(seconds=30))
    assert [e["data"]["presses"] for e in events] == [3, 2]
    mid = T0 + SLEEP / 2
    for ev in events:
        s, e = _span(ev)
        assert not (s < mid < e), ev
    assert len({e["id"] for e in events}) == 2, "distinct ids: the server upserts by id"


def test_inproc_steady_state_is_still_one_checkpoint_to_now_event():
    """No silence: unchanged [checkpoint, now] stamp and checkpoint-derived id."""
    eng = _inproc_engine()
    eng._build_inproc_input(T0)
    for k in range(6):
        eng.input_source._on_press(5, now=T0 + timedelta(seconds=10 * k + 1))
    [ev] = eng._build_inproc_input(T0 + timedelta(seconds=60))[0]
    assert ev["timestamp"] == T0.isoformat()
    assert ev["duration"] == 60.0
    assert ev["id"] == f"input-inproc_host_{int(T0.timestamp() * 1000)}"


def test_inproc_peek_and_drain_of_a_fresh_segment_share_an_id():
    """A segment after a silence starts at its first count, not at the
    checkpoint; the min-duration pad may move its visible start with `now`, the
    id must not, or the analyzer counts the peek and the drain twice."""
    eng = _inproc_engine()
    eng._build_inproc_input(T0)
    wake = T0 + SLEEP
    eng.input_source._on_press(4, now=wake)
    [peeked] = eng._peek_inproc_input(wake + timedelta(seconds=2))
    [drained] = eng._build_inproc_input(wake + timedelta(seconds=7))[0]
    assert peeked["id"] == drained["id"]


# -- 3. the threshold is the configured AFK timeout -------------------------


def test_threshold_is_the_configured_afk_timeout_not_600():
    """37 of 44 devices run a 20 min AFK timeout: a 15 min pause is still
    'at work' for them and must not split; at 10 min it must."""
    for minutes, expect_segments in ((20, 1), (10, 2)):
        cfg = Config()
        cfg.aw.afk_timeout_minutes = minutes
        from src.main import BetterFlowApp
        getter = BetterFlowApp._input_max_silence_s.__get__(SimpleNamespace(config=cfg))
        assert getter() == minutes * 60.0
        src = InputSource(hostname="h", backend=_Backend(), frontmost_app_getter=None,
                          max_silence_s=getter)
        src._on_press(1, now=T0 + timedelta(seconds=1))
        src._on_press(1, now=T0 + timedelta(minutes=15))
        events = src.drain_input_events(T0, T0 + timedelta(minutes=16))
        assert len(events) == expect_segments, (minutes, events)


def test_threshold_change_applies_without_a_restart():
    cfg = SimpleNamespace(v=1200.0)
    held = HeldInputSpans(max_silence_s=lambda: cfg.v)
    held.note(T0, presses=1)
    held.note(T0 + timedelta(minutes=15), presses=1)
    assert len(held.stamp(T0 + timedelta(minutes=16), floor=T0)) == 1
    cfg.v = 600.0
    held.note(T0 + timedelta(minutes=30), presses=1)
    assert len(held.stamp(T0 + timedelta(minutes=31), floor=T0)) == 2


@pytest.mark.parametrize("bad", [None, 0, -5, "x", float("nan"), float("inf")])
def test_an_unusable_threshold_falls_back_to_the_default(bad):
    held = HeldInputSpans(max_silence_s=lambda: bad)
    assert held.max_silence_s() == input_span.DEFAULT_MAX_SILENCE_S


def test_a_raising_threshold_getter_falls_back_to_the_default():
    def boom():
        raise RuntimeError("config unreadable")
    assert HeldInputSpans(max_silence_s=boom).max_silence_s() == input_span.DEFAULT_MAX_SILENCE_S


# -- 4. the helper conserves counts ------------------------------------------


def test_consume_keeps_counts_that_arrived_after_the_stamp():
    held = HeldInputSpans(max_silence_s=600.0)
    held.note(T0, presses=5)
    spans = held.stamp(T0 + timedelta(seconds=10), floor=None)
    held.note(T0 + timedelta(seconds=11), presses=3)        # during the post
    held.note(T0 + timedelta(minutes=30), clicks=2)         # a new segment
    held.consume(spans, taken_at=T0 + timedelta(seconds=10))
    assert held.totals() == (3, 2, 0)
    rest = held.stamp(T0 + timedelta(minutes=31), floor=T0 + timedelta(seconds=10))
    assert [s.counts for s in rest] == [(3, 0, 0), (0, 2, 0)]
    assert rest[0].start >= T0 + timedelta(seconds=10), "never re-covers the delivered span"


def test_a_second_consume_of_the_same_spans_is_a_no_op():
    held = HeldInputSpans(max_silence_s=600.0)
    held.note(T0, presses=5)
    spans = held.stamp(T0 + timedelta(seconds=10))
    held.consume(spans, taken_at=T0 + timedelta(seconds=10))
    held.note(T0 + timedelta(seconds=12), presses=1)
    held.consume(spans, taken_at=T0 + timedelta(seconds=10))
    assert held.totals() == (1, 0, 0)


def test_spans_never_overlap_and_never_shorter_than_pad():
    held = HeldInputSpans(max_silence_s=60.0, pad_s=10.0)
    for t in (0, 5, 100, 101, 400):
        held.note(T0 + timedelta(seconds=t), presses=1)
    spans = held.stamp(T0 + timedelta(seconds=401), floor=T0)
    assert [s.counts[0] for s in spans] == [2, 2, 1]
    for a, b in zip(spans, spans[1:], strict=False):
        assert a.end <= b.start
    assert all(s.duration_s >= 10.0 for s in spans)


# -- 5. guards: both senders go through the shared helper --------------------


class _StampSpy:
    def __init__(self, monkeypatch):
        self.calls = []
        real = HeldInputSpans.stamp
        spy = self

        def stamp(held, now, floor=None):
            out = real(held, now, floor=floor)
            spy.calls.append(out)
            return out

        monkeypatch.setattr(HeldInputSpans, "stamp", stamp)


def test_guard_macos_emitter_stamps_through_the_shared_helper(monkeypatch):
    spy = _StampSpy(monkeypatch)
    clock = _Clock(T0)
    _install_clock(monkeypatch, clock)
    aw = _ScriptedAW()
    w = _watcher(aw)
    _run(monkeypatch, w, clock, [lambda: _press(w, 3)])
    assert spy.calls, "MacOSInputWatcher built its span without HeldInputSpans.stamp"
    [spans] = spy.calls
    assert [(s.start.isoformat(), s.duration_s, s.counts[0]) for s in spans] == [
        (ev["timestamp"], ev["duration"], ev["data"]["presses"]) for ev in aw.posted
    ]


def test_guard_inproc_sender_stamps_through_the_shared_helper(monkeypatch):
    spy = _StampSpy(monkeypatch)
    eng = _inproc_engine()
    eng._build_inproc_input(T0)
    eng.input_source._on_press(3, now=T0 + timedelta(seconds=5))
    events, _ = eng._build_inproc_input(T0 + timedelta(seconds=60))
    assert spy.calls, "InputSource built its span without HeldInputSpans.stamp"
    [spans] = spy.calls
    assert [(s.start.isoformat(), round(s.duration_s, 2), s.counts[0]) for s in spans] == [
        (ev["timestamp"], ev["duration"], ev["data"]["presses"]) for ev in events
    ]
