import sys
import threading
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.sync.aw_client import BUCKET_TYPE_INPUT
from src.sync.input_source import InputSource, _MacOSTapBackend

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


def test_available_delegates_to_the_backend_and_never_caches():
    """The backend owns the verdict; InputSource caches nothing.

    Replaces the old sticky-latch test. The latch defended against a FLAPPY
    probe, and neither real backend has one — each answers "capability present
    AND last install succeeded", which never varies with whether anyone typed.
    What it actually did was outlive the sensor it vouched for: a started hook
    that later died kept reporting healthy, and _should_skip_external_input went
    on dropping the external bucket while nothing counted.
    """
    b = _FakeBackend(available=False, start_ok=True)
    src = _src(b)
    assert src.available() is False, "backend says no -> source says no"
    assert src.start() is True
    assert b.started is True
    # A successful start does NOT override a backend that reports unusable.
    assert src.available() is False, "a start must not paper over the verdict"

    b._available = True
    assert src.available() is True, "and it tracks the backend back up again"


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


# -- macOS backend: drain (not delta) so nothing is lost to the emitter --------


class _StubWatcher:
    def __init__(self, presses=0, clicks=0, scrolls=0):
        self._lock = threading.Lock()
        self._presses = presses
        self._clicks = clicks
        self._scrolls = scrolls


def test_macos_backend_drains_and_zeroes_watcher_counters():
    backend = _MacOSTapBackend(_src(backend=None))
    backend._watcher = _StubWatcher(presses=5, clicks=2, scrolls=1)
    assert backend._drain_watcher_counts() == (5, 2, 1)
    # Zeroed, so this backend is the SOLE drainer and the next drain starts fresh.
    assert backend._drain_watcher_counts() == (0, 0, 0)


def test_macos_backend_drain_accumulates_without_loss():
    # Two poll ticks with fresh counts arriving between them: draining owns the
    # reset, so nothing is lost (the old delta+emitter-subtract path could lose
    # counts the watcher subtracted before the next poll).
    src = _src(backend=None)
    backend = _MacOSTapBackend(src)
    w = _StubWatcher()
    backend._watcher = w

    def poll_once():
        p, c, s = backend._drain_watcher_counts()
        if p:
            src._on_press(p)
        if c:
            src._on_click(c)
        if s:
            src._on_scroll(s)

    with w._lock:
        w._presses = 10
    poll_once()
    with w._lock:
        w._presses = 7  # more arrived after the first drain
    poll_once()
    assert src.counts == (17, 0, 0)


def test_macos_watcher_count_only_skips_emitter_flag():
    from src.sync.macos_input_watcher import MacOSInputWatcher
    assert MacOSInputWatcher(aw_client=object(), count_only=True)._count_only is True
    assert MacOSInputWatcher(aw_client=object())._count_only is False


def test_macos_watcher_reenables_tap_for_both_disable_reasons():
    """macOS disables a CGEventTap for timeout OR user input.

    Handling only the timeout constant left a user-input-disabled tap dead for
    the rest of the session: counters frozen while is_running() stayed True, so
    the agent reported the user idle instead of reporting itself blind.
    """
    from src.sync import macos_input_watcher as miw

    for event_type in (
        miw._kCGEventTapDisabledByTimeout,
        miw._kCGEventTapDisabledByUserInput,
    ):
        watcher = miw.MacOSInputWatcher(aw_client=object(), count_only=True)
        watcher._tap_ref = object()
        enabled = []

        fake_quartz = types.ModuleType("Quartz")
        fake_quartz.CGEventTapEnable = lambda tap, on: enabled.append((tap, on))

        with patch.dict(sys.modules, {"Quartz": fake_quartz}):
            returned = watcher._event_callback(None, event_type, "evt", None)

        assert returned == "evt"
        assert enabled == [(watcher._tap_ref, True)], (
            f"tap not re-enabled for event_type {event_type:#x}"
        )


def test_macos_watcher_disable_event_does_not_count_as_input():
    """A tap-disabled notification must not be mistaken for user activity."""
    from src.sync import macos_input_watcher as miw

    watcher = miw.MacOSInputWatcher(aw_client=object(), count_only=True)
    watcher._tap_ref = None  # no ref: exercises the warn-only branch

    watcher._event_callback(None, miw._kCGEventTapDisabledByUserInput, "evt", None)

    with watcher._lock:
        assert (watcher._presses, watcher._clicks, watcher._scrolls) == (0, 0, 0)
    # Also must not refresh the freshness timestamp: the idle-tracker health
    # check reads get_last_input_at() to spot a blind watcher, so a disable
    # notification that looked like input would mask the very outage it signals.
    assert watcher.get_last_input_at() is None


# -- a failed Windows hook install must not masquerade as a working sensor ----
#
# Origin 2026-07-23: Sachi/Claudia (Windows, 1.5.116) show "input sensor
# missing" and 0 keys/min. Windows bundles NO external input tracker, so this
# in-process hook is the only input source there. If SetWindowsHookEx is refused
# (UIPI / AV / EDR) the source must say so — otherwise the drain emits nothing,
# the dashboard is byte-identical to "the user typed nothing", and there is no
# way to tell a blocked hook from an idle employee.


class _RefusedHookBackend:
    """SetWindowsHookEx refused: probes True until the attempt, then False —
    the real _WindowsHookBackend's _install_failed contract in miniature."""

    def __init__(self):
        self._install_failed = False

    def available(self):
        return not self._install_failed

    def start(self):
        self._install_failed = True
        return False

    def stop(self):
        pass


def _win_backend(monkeypatch):
    """A real _WindowsHookBackend on a machine pretending to be Windows."""
    from src.sync import input_source as m

    monkeypatch.setattr(m.platform, "system", lambda: "Windows")
    return m._WindowsHookBackend(InputSource(hostname="host", backend=None,
                                             frontmost_app_getter=None))


def test_windows_backend_unavailable_after_failed_hook_install(monkeypatch):
    be = _win_backend(monkeypatch)
    assert be.available() is True, "capability probe before any install attempt"

    be._install_failed = True  # what _run() records when SetWindowsHookEx returns NULL

    assert be.available() is False, (
        "a backend whose hook install was refused must report unavailable"
    )


def test_failed_windows_hook_does_not_latch_available(monkeypatch):
    """The capability probe must not latch available() on.

    _should_skip_external_input's own docstring promises: 'If the backend fails
    to install at all, available() never latches on and this returns False, so
    the external tracker keeps its job.' Latching from the PROBE (which answers
    'is this Windows?', not 'did the hook install?') broke that promise.
    """
    from src.sync import input_source as m

    monkeypatch.setattr(m.platform, "system", lambda: "Windows")

    be = _RefusedHookBackend()
    src = _src(be)

    src.available()          # main.py probes this at startup, BEFORE start()
    assert src.start() is False

    assert src.available() is False, (
        "a refused hook must not report an available input sensor"
    )


def test_refused_hook_leaves_external_input_to_the_tracker(monkeypatch):
    """End of the chain: the sync engine must not suppress the external input
    bucket on the strength of a hook that never installed."""
    from src.sync import input_source as m
    from tests.test_sync_engine_inproc_input import _engine

    monkeypatch.setattr(m.platform, "system", lambda: "Windows")

    engine = _engine(True)  # in_process_input ON, as it will be on Windows
    engine.input_source = InputSource(hostname="host", backend=_RefusedHookBackend(),
                                      frontmost_app_getter=None)
    engine.input_source.available()   # the startup probe
    engine.input_source.start()       # refused

    assert engine._should_use_inproc_input() is False
    assert engine._should_skip_external_input() is False


def test_a_sensor_that_dies_after_a_good_start_stops_reporting_available():
    """A hook that installed at 09:00 and died at 14:00 must not keep reporting
    a working sensor. This is the case the old latch got wrong: it cached the
    morning's success, so _should_skip_external_input went on dropping the
    external input bucket while nothing in-process counted anything.

    Note the death is observed WITHOUT a re-start() — the real backends record
    it from their own thread (crashed hook thread, GetMessageW -1, a poll loop
    that raised), so available() has to see it immediately rather than at the
    next converge."""
    b = _FakeBackend(available=True, start_ok=True)
    src = _src(b)
    assert src.start() is True
    assert src.available() is True

    b._available = False  # the hook thread recorded its own death

    assert src.available() is False, (
        "a dead sensor must read as unavailable straight away, not one converge "
        "cycle later"
    )


def _boom():
    raise OSError("hook thread died before installing")


def test_windows_hook_thread_crash_is_recorded_as_install_failure(monkeypatch):
    """The _run wrapper is the whole reason _run_hooks was split out, and
    nothing else watches it: every path _run_hooks fails to reach leaves
    _install_failed False, which is indistinguishable from a working sensor.
    Windows has no external tracker to fall back to, so that reads as an
    employee who never touched the keyboard.
    """
    be = _win_backend(monkeypatch)
    assert be.available() is True, "capability probe before any install attempt"

    monkeypatch.setattr(be, "_run_hooks", _boom)

    assert be.start() is False, "a crashed hook thread must not report a start"
    assert be._started.is_set(), (
        "_started must be set on the crash path too, or start() blocks for its "
        "full timeout on a thread that has already gone"
    )
    assert be.available() is False, (
        "a hook thread that died must not read as a working input sensor"
    )


def test_clean_hook_thread_exit_is_not_an_install_failure(monkeypatch):
    """Negative control for the test above: the wrapper must only condemn a
    backend that actually escaped. Without this, `available() is False` would
    also pass on a _run that condemns unconditionally.
    """
    be = _win_backend(monkeypatch)
    monkeypatch.setattr(be, "_run_hooks", lambda: be._started.set())

    be.start()
    # Join before reading available(): this stub sets _started ITSELF, so start()
    # returns as soon as the stub fires — while the thread is still unwinding
    # _run's finally and is_alive() is still True. Without the join, available()
    # falls through to the capability probe and the assertion below flakes.
    # (The crash test above needs no join: _run's except sets _install_failed
    # before _started, so its verdict is already visible when start() returns.)
    be._thread.join(timeout=2.0)
    assert be._thread.is_alive() is False, "the stubbed hook thread must have exited"

    # THE discriminator: a _run that condemned unconditionally would set this.
    assert be._install_failed is False, "a clean hook thread is not a failure"
    # available() is False here for a DIFFERENT reason — this stub returns
    # immediately, so the thread has already exited and a thread that is not
    # running is not counting (see _listener_stopped). Not condemnation:
    # _install_failed above is what separates the two, and a live thread reads
    # available (test_a_live_sensor_still_reports_available).
    assert be.available() is False


# -- the same phantom sensor on the other two doors ---------------------------


def test_macos_backend_unavailable_after_a_refused_tap(monkeypatch):
    """A denied Input Monitoring grant must not report a working sensor.

    _MacOSTapBackend.available() probed "does Quartz import", which is true on
    every Mac — the same "is this the right platform?" answer that let a refused
    Windows hook masquerade as a healthy one. The grant is what actually fails,
    and MacOSInputWatcher.start() is where that shows up.
    """
    from src.sync import input_source as m

    monkeypatch.setattr(m.platform, "system", lambda: "Darwin")

    be = _MacOSTapBackend(InputSource(hostname="host", backend=None,
                                      frontmost_app_getter=None))

    class _DeniedWatcher:
        def start(self):
            return False  # tap not created: Input Monitoring not granted

        def stop(self):
            pass

    monkeypatch.setattr(be, "_build_watcher", lambda: _DeniedWatcher())

    assert be.start() is False
    assert be.available() is False, (
        "a refused CGEventTap must not report an available input sensor"
    )


def test_macos_backend_recovers_when_the_grant_is_restored(monkeypatch):
    """Negative control for the test above: the verdict must not be a one-way
    latch, or re-granting Input Monitoring would never take effect."""
    from src.sync import input_source as m

    monkeypatch.setattr(m.platform, "system", lambda: "Darwin")

    be = _MacOSTapBackend(InputSource(hostname="host", backend=None,
                                      frontmost_app_getter=None))

    class _Watcher:
        """Same surface _drain_watcher_counts() reaches into on the real one."""

        def __init__(self, ok):
            self._ok = ok
            self._lock = threading.Lock()
            self._presses = self._clicks = self._scrolls = 0

        def start(self):
            return self._ok

        def stop(self):
            pass

    monkeypatch.setattr(be, "_build_watcher", lambda: _Watcher(False))
    be.start()
    assert be.available() is False

    monkeypatch.setattr(be, "_build_watcher", lambda: _Watcher(True))
    assert be.start() is True
    assert be.available() is True, "a restored grant must clear the verdict"
    be.stop()


def test_windows_pump_treats_minus_one_as_an_error_not_a_message(monkeypatch):
    """GetMessageW returns -1 on error, and `!= 0` accepted it as a message.

    That dispatches an uninitialised MSG in a tight spin — the hooks are
    installed but nothing is being pumped correctly, so counting is dead while
    available() still says fine. Exit the pump and record the verdict instead.
    """
    from src.sync import input_source as m

    monkeypatch.setattr(m.platform, "system", lambda: "Windows")
    be = m._WindowsHookBackend(InputSource(hostname="host", backend=None,
                                           frontmost_app_getter=None))

    dispatched = []

    class _SpinDetectedError(Exception):
        pass

    class _FakeUser32:
        """Always returns the error code. A pump that treats -1 as a message
        never exits — so bound it: the pre-fix `!= 0` loop HANGS rather than
        fails, and a guard that hangs CI is worse than one that goes red."""

        def GetMessageW(self, *a):
            return -1

        def TranslateMessage(self, *a):
            dispatched.append("translate")
            raise _SpinDetectedError("pump dispatched an error return as a message")

        def DispatchMessageW(self, *a):  # pragma: no cover - spin trips first
            dispatched.append("dispatch")

    be._pump_messages(_FakeUser32(), object())

    assert dispatched == [], "an error return must not be dispatched as a message"
    assert be._install_failed is True, "a dead pump must not read as a live sensor"


def test_windows_pump_exits_cleanly_on_wm_quit(monkeypatch):
    """Negative control: a deliberate stop() (WM_QUIT -> 0) is not a failure."""
    from src.sync import input_source as m

    monkeypatch.setattr(m.platform, "system", lambda: "Windows")
    be = m._WindowsHookBackend(InputSource(hostname="host", backend=None,
                                           frontmost_app_getter=None))

    returns = iter([1, 0])  # one real message, then WM_QUIT
    dispatched = []

    class _FakeUser32:
        def GetMessageW(self, *a):
            return next(returns)

        def TranslateMessage(self, *a):
            dispatched.append("translate")

        def DispatchMessageW(self, *a):
            dispatched.append("dispatch")

    be._pump_messages(_FakeUser32(), object())

    assert dispatched == ["translate", "dispatch"]
    assert be._install_failed is False, "a clean WM_QUIT exit is not an install failure"


# -- lifecycle holes: every path that ends the sensor must record it ----------


def test_windows_backend_records_a_thread_that_could_not_start(monkeypatch):
    """Thread exhaustion means _run never runs, so nothing else records the
    verdict and available() would vouch for a hook never even attempted."""
    from src.sync import input_source as m

    monkeypatch.setattr(m.platform, "system", lambda: "Windows")
    be = m._WindowsHookBackend(InputSource(hostname="host", backend=None,
                                           frontmost_app_getter=None))

    def _refuse(*a, **kw):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(m.threading, "Thread", _refuse)

    assert be.start() is False
    assert be.available() is False


def test_windows_dead_thread_clears_start_ok(monkeypatch):
    """start()'s is_alive() fast-path returns _start_ok; a dead thread that left
    it True reports a working sensor for whatever window the caller observes."""
    from src.sync import input_source as m

    monkeypatch.setattr(m.platform, "system", lambda: "Windows")
    be = m._WindowsHookBackend(InputSource(hostname="host", backend=None,
                                           frontmost_app_getter=None))

    be._start_ok = True          # as if the hooks had installed
    be._thread_id = 4242         # and the pump had recorded its TID

    def _die():
        raise RuntimeError("pump exploded")

    monkeypatch.setattr(be, "_run_hooks", _die)
    be._run()

    assert be._start_ok is False, "a dead thread is not a started one"
    assert be._thread_id is None, "a recycled TID must never receive our WM_QUIT"
    assert be._install_failed is True
    assert be._started.is_set() is True


def test_macos_refused_tap_does_not_leak_a_watcher_per_converge(monkeypatch):
    """_apply_capture_policy converges every 60s. A Mac with Input Monitoring
    denied would otherwise build and abandon a MacOSInputWatcher every minute."""
    from src.sync import input_source as m

    monkeypatch.setattr(m.platform, "system", lambda: "Darwin")
    be = _MacOSTapBackend(InputSource(hostname="host", backend=None,
                                      frontmost_app_getter=None))

    stopped = []

    class _DeniedWatcher:
        def start(self):
            return False

        def stop(self):
            stopped.append(True)

    monkeypatch.setattr(be, "_build_watcher", lambda: _DeniedWatcher())

    assert be.start() is False
    assert stopped == [True], "the abandoned watcher must be released"
    assert be._watcher is None, "and dropped, so the next attempt builds fresh"


def test_macos_poll_thread_death_is_recorded_not_silent(monkeypatch):
    """The macOS twin of the Windows dead-thread case: an escape from the poll
    loop left available() reporting healthy while nothing drained."""
    from src.sync import input_source as m

    monkeypatch.setattr(m.platform, "system", lambda: "Darwin")
    src = InputSource(hostname="host", backend=None, frontmost_app_getter=None)
    be = _MacOSTapBackend(src)

    def _explode():
        raise RuntimeError("watcher vanished mid-drain")

    monkeypatch.setattr(be, "_drain_watcher_counts", _explode)
    be._stop.clear()
    monkeypatch.setattr(be._stop, "wait", lambda _timeout: False)

    be._poll_loop()  # must return, not propagate

    assert be._install_failed is True
    assert be.available() is False


def test_macos_restart_drains_the_watcher_the_dead_poll_thread_left_behind(monkeypatch):
    """Releasing the abandoned watcher must not throw its counts away.

    The CGEventTap goes on counting after the poll thread dies. available() is
    False for that whole span, so the checkpoint never advances and the next
    drain covers it — those keystrokes are real and belong in it. Dropping them
    under-reports input on exactly the devices this feature exists to stop
    under-reporting (a zero-keystroke day is what the fraud engine flags).
    """
    from src.sync import input_source as m

    monkeypatch.setattr(m.platform, "system", lambda: "Darwin")
    src = InputSource(hostname="host", backend=None, frontmost_app_getter=None)
    be = _MacOSTapBackend(src)

    class _Watcher:
        """The surface _drain_watcher_counts() reaches into on the real one."""

        def __init__(self, presses=0):
            self._lock = threading.Lock()
            self._presses = presses
            self._clicks = 0
            self._scrolls = 0

        def start(self):
            return True

        def stop(self):
            pass

    # What the tap counted while the poll thread was gone.
    be._watcher = _Watcher(presses=17)

    monkeypatch.setattr(be, "_build_watcher", lambda: _Watcher())
    assert be.start() is True
    be.stop()

    assert src.counts[0] == 17, (
        "counts left in the abandoned tap must reach the source, not vanish"
    )


def test_a_stopped_sensor_does_not_report_available(monkeypatch):
    """A deliberate stop() is not a failure, but it IS "not counting".

    _stop_watchers() calls this path to enforce the working-hours capture
    policy. While stopped, available() must be False or the engine goes on
    suppressing the external input bucket in favour of a sensor that is switched
    off — the same staleness the latch removal closed, arriving via teardown
    instead of via a crash.
    """
    from src.sync import input_source as m

    monkeypatch.setattr(m.platform, "system", lambda: "Windows")
    be = m._WindowsHookBackend(InputSource(hostname="host", backend=None,
                                           frontmost_app_getter=None))

    assert be.available() is True, "no start attempted yet -> capability probe"

    class _DeadThread:
        def is_alive(self):
            return False

    be._thread = _DeadThread()   # started once; the pump has since exited

    assert be._install_failed is False, "a clean stop is not an install failure"
    assert be.available() is False, "...but it is not an available sensor either"


def test_a_live_sensor_still_reports_available(monkeypatch):
    """Negative control for the test above — otherwise it would pass against an
    available() that simply always returned False after any start."""
    from src.sync import input_source as m

    monkeypatch.setattr(m.platform, "system", lambda: "Windows")
    be = m._WindowsHookBackend(InputSource(hostname="host", backend=None,
                                           frontmost_app_getter=None))

    class _LiveThread:
        def is_alive(self):
            return True

    be._thread = _LiveThread()

    assert be.available() is True


def test_discard_also_drops_what_the_backend_is_still_holding():
    """discard_counts() must reach the backend's own buffer, not just ours.

    The macOS backend wraps a MacOSInputWatcher that counts into ITS counters;
    a poll folds them onto the source once a second. Clearing only the source
    leaves up to a poll interval of Private Time input in the watcher — and if
    the poll thread has died, start()'s leftover-drain resurrects a whole
    window's counts on the next 60s converge. Windows increments the source
    directly and holds nothing, which is why this is expressed against the
    backend protocol rather than a platform.
    """
    src = _src(backend=None)
    backend = _MacOSTapBackend(src)
    backend._watcher = _StubWatcher(presses=250, clicks=9, scrolls=3)
    src._backend = backend

    src._on_press(500)  # already folded onto the source by an earlier poll

    dropped = src.discard_counts()

    assert dropped == (500, 0, 0), "reports what the SOURCE was holding"
    assert src.counts == (0, 0, 0)
    assert backend._drain_watcher_counts() == (0, 0, 0), (
        "the watcher's own buffer must be emptied too, or the next poll folds "
        "private-window input onto a post-resume span"
    )


def test_discard_is_safe_on_a_backend_with_nothing_buffered():
    """Negative control: a backend with no pending buffer (Windows, and every
    test fake) must not be required to implement the hook."""
    b = _FakeBackend()
    src = _src(b)
    src._on_press(4)

    assert src.discard_counts() == (4, 0, 0)
    assert src.counts == (0, 0, 0)
