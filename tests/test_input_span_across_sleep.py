"""An input event must never claim the time the machine was asleep.

Device 53 (agent 1.5.145, 2026-11-09): the laptop slept from Friday
2026-11-06 ~14:42 UTC to Monday ~10:55 UTC. A keypress landed in the last
seconds before sleep, after the emitter's previous tick, so the counter held
it with ``_span_start`` = Friday 14:42. The emitter thread froze with the
machine; on wake its 10 s wait returned and it posted ONE
``aw-watcher-input`` event stamped [Friday 14:42, Monday 10:56] (~68 h)
carrying that one press.

ActivityWatch clips an event to the query range, so the start-of-day backlog
reconcile (2 h windows from local midnight) uploaded it as six 7200 s input
events from 00:00 Bucharest, and the live fetch added a 6885 s piece. The
server counts an input span as active wall-clock time, so BetterFlow showed
13.6 h tracked for a machine that was asleep until 12:55 local.

The fix in ``MacOSInputWatcher``: held counts never stretch across a silence
longer than ``_MAX_INPUT_SILENCE_S``. These tests drive the real emitter loop
and the real tap callback with a scripted clock (no threads, no sleeps).
"""

from datetime import datetime, timedelta, timezone

from src.sync import macos_input_watcher as miw

T0 = datetime(2026, 11, 6, 14, 42, 0, tzinfo=timezone.utc)
SLEEP = timedelta(hours=68, minutes=14)


class _Clock:
    def __init__(self, now):
        self.now = now


class _AW:
    def __init__(self):
        self.posted = []

    def post_events(self, bucket_id, events):
        self.posted.extend(events)


def _install_clock(monkeypatch, clock):
    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now

    monkeypatch.setattr(miw, "datetime", _FakeDatetime)


def _press(w, n=1):
    for _ in range(n):
        w._event_callback(None, miw._kCGEventKeyDown, object(), None)


def _run(monkeypatch, w, steps):
    """Run the real _run_emitter. Each step runs inside one ``wait`` (i.e.
    between two emitter ticks) and is responsible for moving the clock."""
    it = iter(steps)

    def fake_wait(timeout):
        step = next(it, None)
        if step is None:
            return True
        step()
        return False

    monkeypatch.setattr(w._stop_event, "wait", fake_wait)
    monkeypatch.setattr(w, "_current_frontmost_app", lambda: (None, None))
    w._run_emitter()


def _span(ev):
    start = datetime.fromisoformat(ev["timestamp"])
    return start, start + timedelta(seconds=ev["duration"])


def test_press_just_before_sleep_is_not_stamped_across_the_sleep(monkeypatch):
    clock = _Clock(T0)
    _install_clock(monkeypatch, clock)
    aw = _AW()
    w = miw.MacOSInputWatcher(aw, emit_interval=10.0)

    def press_then_sleep():
        clock.now += timedelta(seconds=5)
        _press(w)  # held: the next tick is 5 s away...
        clock.now += SLEEP  # ...and the machine sleeps first

    _run(monkeypatch, w, [press_then_sleep])

    assert len(aw.posted) == 1
    ev = aw.posted[0]
    assert ev["data"]["presses"] == 1
    start, end = _span(ev)
    press_at = T0 + timedelta(seconds=5)
    assert ev["duration"] <= 10.0, f"span covers the sleep: {ev}"
    assert start <= press_at <= end


def test_wake_keypress_does_not_bridge_the_sleep(monkeypatch):
    """The key that wakes the Mac reaches the tap before the frozen emitter
    ticks: the held span must restart at it, not run back to Friday."""
    clock = _Clock(T0)
    _install_clock(monkeypatch, clock)
    aw = _AW()
    w = miw.MacOSInputWatcher(aw, emit_interval=10.0)

    def press_sleep_wake_press():
        clock.now += timedelta(seconds=5)
        _press(w)
        clock.now += SLEEP
        _press(w)

    _run(monkeypatch, w, [press_sleep_wake_press])

    assert len(aw.posted) == 1
    ev = aw.posted[0]
    assert ev["data"]["presses"] == 2, "counts are kept, only the span is honest"
    start, end = _span(ev)
    assert ev["duration"] <= 10.0, f"span covers the sleep: {ev}"
    assert end == clock.now


def test_no_posted_event_overlaps_the_sleep(monkeypatch):
    """Whatever the emitter posts around a sleep, no event may cover the
    middle of it (the instant the server would bill)."""
    clock = _Clock(T0)
    _install_clock(monkeypatch, clock)
    aw = _AW()
    w = miw.MacOSInputWatcher(aw, emit_interval=10.0)
    sleep_from = {}

    def type_then_sleep():
        clock.now += timedelta(seconds=5)
        _press(w, 3)
        sleep_from["t"] = clock.now
        clock.now += SLEEP
        _press(w, 2)

    def one_more_tick():
        clock.now += timedelta(seconds=10)
        _press(w, 4)

    _run(monkeypatch, w, [type_then_sleep, one_more_tick])

    mid_sleep = sleep_from["t"] + SLEEP / 2
    for ev in aw.posted:
        start, end = _span(ev)
        assert not (start < mid_sleep < end), f"event spans the sleep: {ev}"
    assert sum(ev["data"]["presses"] for ev in aw.posted) == 9
