from datetime import datetime, timedelta, timezone

from src.sync.aw_client import BUCKET_TYPE_INPUT
from src.sync.input_source import InputSource

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


class _FakeBackend:
    """A backend whose start()/available() are scriptable and that lets a test
    inject counts by calling the source's callbacks directly."""

    def __init__(self, *, available=True, start_ok=True):
        self._available = available
        self._start_ok = start_ok
        self.started = False
        self.stopped = False

    def available(self):
        return self._available

    def start(self):
        self.started = True
        return self._start_ok

    def stop(self):
        self.stopped = True


def _src(backend=None, **kw):
    return InputSource(hostname="host", backend=backend,
                       frontmost_app_getter=None, **kw)


# -- available() / sticky latch ----------------------------------------------


def test_available_false_when_no_backend():
    assert _src(backend=None).available() is False


def test_available_true_when_backend_reports_usable():
    assert _src(_FakeBackend(available=True)).available() is True


def test_available_false_when_backend_unusable():
    assert _src(_FakeBackend(available=False)).available() is False


def test_available_latches_on_after_start():
    b = _FakeBackend(available=False, start_ok=True)
    src = _src(b)
    assert src.available() is False  # backend reports not-yet-usable
    assert src.start() is True
    assert b.started is True
    # Latched on: even though the backend still reports unusable, a started
    # backend means the platform HAS the capability.
    assert src.available() is True


def test_start_returns_false_when_backend_fails():
    src = _src(_FakeBackend(start_ok=False))
    assert src.start() is False


def test_start_noop_without_backend():
    src = _src(backend=None)
    assert src.start() is False


def test_stop_delegates_to_backend():
    b = _FakeBackend()
    src = _src(b)
    src.stop()
    assert b.stopped is True


# -- counters ----------------------------------------------------------------


def test_callbacks_increment_counters():
    src = _src(_FakeBackend())
    src._on_press()
    src._on_press(3)
    src._on_click()
    src._on_scroll(2)
    assert src.counts == (4, 1, 2)


# -- drain -------------------------------------------------------------------


def test_drain_builds_event_with_counts_and_resets():
    src = _src(_FakeBackend())
    src._on_press(5)
    src._on_click(2)
    src._on_scroll(1)
    ev = src.drain_input_event(T0, T0 + timedelta(seconds=60))
    assert ev is not None
    assert ev["data"] == {"presses": 5, "clicks": 2, "scrolls": 1}
    assert ev["bucket_id"] == "bf-input-inproc_host"
    assert ev["bucket_type"] == BUCKET_TYPE_INPUT
    assert ev["timestamp"] == T0.isoformat()
    assert ev["duration"] == 60.0
    # Counters reset after the drain.
    assert src.counts == (0, 0, 0)
    # A second drain over a fresh (empty) span yields nothing.
    assert src.drain_input_event(T0 + timedelta(seconds=60),
                                 T0 + timedelta(seconds=120)) is None


def test_drain_zero_counts_returns_none_and_holds_counters():
    src = _src(_FakeBackend())
    assert src.drain_input_event(T0, T0 + timedelta(seconds=60)) is None
    # No counters to hold, but the counters stay at zero (not mutated below zero).
    assert src.counts == (0, 0, 0)


def test_drain_none_when_unavailable_and_preserves_counts():
    # No backend -> never available -> a period produces no event (gap), and the
    # counts injected are NOT drained/reset.
    src = _src(backend=None)
    src._on_press(3)
    assert src.drain_input_event(T0, T0 + timedelta(seconds=60)) is None
    assert src.counts == (3, 0, 0)  # preserved


def test_drain_none_for_empty_or_inverted_range():
    src = _src(_FakeBackend())
    src._on_press(1)
    assert src.drain_input_event(T0, T0) is None
    assert src.drain_input_event(T0 + timedelta(seconds=1), T0) is None
    # Counters untouched by a rejected range.
    assert src.counts == (1, 0, 0)


def test_event_id_is_ms_precision_and_stable():
    src = _src(_FakeBackend())
    src._on_press(1)
    ev = src.drain_input_event(T0, T0 + timedelta(seconds=30))
    assert ev["id"] == f"input-inproc_host_{int(T0.timestamp() * 1000)}"


def test_event_includes_app_when_getter_available():
    src = InputSource(hostname="host", backend=_FakeBackend(),
                      frontmost_app_getter=lambda: "Code")
    src._on_press(1)
    ev = src.drain_input_event(T0, T0 + timedelta(seconds=30))
    assert ev["data"]["app"] == "Code"


def test_event_omits_app_when_getter_blank_or_fails():
    src = InputSource(hostname="host", backend=_FakeBackend(),
                      frontmost_app_getter=lambda: "")
    src._on_press(1)
    ev = src.drain_input_event(T0, T0 + timedelta(seconds=30))
    assert "app" not in ev["data"]

    def _boom():
        raise RuntimeError("probe failed")

    src2 = InputSource(hostname="host", backend=_FakeBackend(),
                       frontmost_app_getter=_boom)
    src2._on_click(1)
    ev2 = src2.drain_input_event(T0, T0 + timedelta(seconds=30))
    assert "app" not in ev2["data"]  # a failing probe must not fail the drain


def test_bucket_id_is_single_source_of_truth():
    src = _src(_FakeBackend())
    assert src.bucket_id == "bf-input-inproc_host"
