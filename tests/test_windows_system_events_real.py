"""The Windows sleep/wake/lock listener against the REAL Win32 API (#277).

tests/test_windows_system_events_signatures.py models the ctypes failure with
fakes; only a 64-bit Windows process can show the real window gets created and
what Windows actually delivers to it.

Windows does not hand a power message to a window the way a test can. Lock and
unlock reach the window directly (WTSRegisterSessionNotification), but sleep and
wake (WM_POWERBROADCAST) are BROADCAST to top-level windows, and Microsoft
documents that a message-only window "does not receive broadcast messages". So
the power tests send through HWND_BROADCAST. Handing the message straight to our
own hwnd would pass for any window type and prove nothing.

Runs only where BETTERFLOW_REAL_WIN32_TESTS=1 is set, which is
.github/workflows/windows-tests.yml alone (build.yml's Windows leg skips them).
Never broadcast these on a
developer machine: HWND_BROADCAST reaches every program in the session.
"""

import logging
import os
import sys
import threading
import time

import pytest

# These BROADCAST PBT_APMSUSPEND/RESUME to every top-level window in the
# session. On a developer machine with the real agent installed that records a
# fake sleep. So they need an explicit opt-in, set only by
# .github/workflows/windows-tests.yml, never by a plain `pytest tests/`.
_OPT_IN_ENV = "BETTERFLOW_REAL_WIN32_TESTS"

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get(_OPT_IN_ENV) != "1",
    reason=(
        "real Win32 API: needs Windows AND " + _OPT_IN_ENV + "=1 "
        "(it broadcasts fake sleep/wake to every program on the desktop)"
    ),
)

_WM_POWERBROADCAST = 0x0218
_WM_WTSSESSION_CHANGE = 0x02B1
_PBT_APMSUSPEND = 0x0004
_PBT_APMRESUMEAUTOMATIC = 0x0012
_PBT_APMRESUMESUSPEND = 0x0007
_WTS_SESSION_LOCK = 0x7
_WTS_SESSION_UNLOCK = 0x8
_HWND_MESSAGE = -3
_HWND_BROADCAST = 0xFFFF
_SMTO_ABORTIFHUNG = 0x0002
_CLASS_NAME = "BetterFlowEvents"


class _Listener:
    def __init__(self):
        self.sleep = threading.Event()
        self.wake = threading.Event()
        self.shutdown = threading.Event()
        self.lock = threading.Event()
        self.unlock = threading.Event()
        self.log = ""
        self.records = []


@pytest.fixture(scope="module")
def listener():
    """Start the real listener ONCE: its window class is process-wide, so a
    second start in the same process would fail to register it."""
    from src import system_events as se

    state = _Listener()

    class _Capture(logging.Handler):
        def emit(self, record):
            state.records.append(record)

    handler = _Capture(level=logging.INFO)
    se_logger = logging.getLogger(se.__name__)
    old_level = se_logger.level
    se_logger.setLevel(logging.INFO)
    se_logger.addHandler(handler)
    try:
        se._start_windows_listener(
            state.sleep.set, state.wake.set, state.shutdown.set,
            on_screen_lock=state.lock.set, on_screen_unlock=state.unlock.set,
        )
        deadline = time.monotonic() + 10
        started = False
        while time.monotonic() < deadline and not started:
            started = any(r.getMessage() == "Windows system event listener started"
                          for r in state.records)
            time.sleep(0.05)
        state.log = " | ".join(f"{r.levelname} {r.getMessage()}" for r in state.records)
        assert started, f"listener never logged its start. Log: {state.log}"
        yield state
    finally:
        se_logger.removeHandler(handler)
        se_logger.setLevel(old_level)


def _user32():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowExW.argtypes = [
        wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
    ]
    user32.FindWindowExW.restype = wintypes.HWND
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.SendMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.SendMessageW.restype = wintypes.LPARAM
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
    ]
    user32.SendMessageTimeoutW.restype = wintypes.LPARAM
    return user32


def _broadcast(user32, msg, wparam):
    """Deliver ``msg`` the way Windows delivers a power broadcast: to every
    top-level window. A message-only window is not a top-level window."""
    import ctypes

    result = ctypes.c_size_t(0)
    user32.SendMessageTimeoutW(
        _HWND_BROADCAST, msg, wparam, 0, _SMTO_ABORTIFHUNG, 5000, ctypes.byref(result),
    )


def test_real_listener_creates_its_window_and_delivers_a_lock(listener):
    import ctypes

    user32 = _user32()
    # Top-level first; a message-only window is invisible to FindWindowW, so
    # fall back to it. Lock reaches either kind: this test is about lock only.
    hwnd = user32.FindWindowW(_CLASS_NAME, None) or user32.FindWindowExW(
        _HWND_MESSAGE, None, _CLASS_NAME, None,
    )
    assert hwnd, f"listener window not found (error {ctypes.get_last_error()})"
    # A 64-bit lParam, as Windows passes for many messages.
    user32.SendMessageW(hwnd, _WM_WTSSESSION_CHANGE, _WTS_SESSION_LOCK, 0x0000_00D1_2345_6780)
    assert listener.lock.wait(5), f"lock message did not reach on_screen_lock. Log: {listener.log}"
    user32.SendMessageW(hwnd, _WM_WTSSESSION_CHANGE, _WTS_SESSION_UNLOCK, 0)
    assert listener.unlock.wait(5), f"unlock did not reach on_screen_unlock. Log: {listener.log}"
    # WTS registration can be refused on a CI session; that is environment, not #277.
    assert not [r for r in listener.records
                if r.levelno >= logging.WARNING and "WTS" not in r.getMessage()]


def test_real_listener_receives_a_broadcast_sleep_and_wake(listener):
    """Sleep and wake arrive by broadcast, so a message-only window never sees them."""
    user32 = _user32()
    _broadcast(user32, _WM_POWERBROADCAST, _PBT_APMSUSPEND)
    assert listener.sleep.wait(5), (
        "WM_POWERBROADCAST/PBT_APMSUSPEND broadcast to top-level windows did not "
        f"reach on_sleep (a message-only window cannot receive it). Log: {listener.log}"
    )
    # A timer wake: RESUMEAUTOMATIC alone is "the system resumed", not "a
    # person is here". SendMessageTimeout is synchronous, so by now the window
    # proc has handled it and a wrongly-fired wake would already be set.
    _broadcast(user32, _WM_POWERBROADCAST, _PBT_APMRESUMEAUTOMATIC)
    assert not listener.wake.is_set(), (
        f"an unattended RESUMEAUTOMATIC must not resume tracking. Log: {listener.log}"
    )
    _broadcast(user32, _WM_POWERBROADCAST, _PBT_APMRESUMESUSPEND)
    assert listener.wake.wait(5), (
        "WM_POWERBROADCAST/PBT_APMRESUMESUSPEND broadcast did not reach on_wake. "
        f"Log: {listener.log}"
    )
