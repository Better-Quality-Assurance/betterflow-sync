"""The Windows sleep/wake/lock listener against the REAL Win32 API (#277).

tests/test_windows_system_events_signatures.py models the ctypes failure with
fakes; only a 64-bit Windows process can show the real window gets created.
Runs on the windows-latest leg of build.yml (tags and workflow_dispatch).
"""

import logging
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="real Win32 API: only a Windows process can exercise it"
)

_WM_WTSSESSION_CHANGE = 0x02B1
_WTS_SESSION_LOCK = 0x7
_HWND_MESSAGE = -3


def test_real_listener_creates_its_window_and_delivers_a_lock(caplog):
    import ctypes
    from ctypes import wintypes

    from src import system_events as se

    locked = threading.Event()
    with caplog.at_level(logging.INFO, logger=se.__name__):
        se._start_windows_listener(
            lambda: None, lambda: None, lambda: None,
            on_screen_lock=locked.set, on_screen_unlock=lambda: None,
        )
        deadline = time.monotonic() + 10
        started = False
        while time.monotonic() < deadline and not started:
            started = any(r.getMessage() == "Windows system event listener started"
                          for r in caplog.records)
            time.sleep(0.05)
        log = " | ".join(f"{r.levelname} {r.getMessage()}" for r in caplog.records)
        assert started, f"listener never logged its start. Log: {log}"

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowExW.argtypes = [
            wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
        ]
        user32.FindWindowExW.restype = wintypes.HWND
        user32.SendMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        user32.SendMessageW.restype = wintypes.LPARAM

        hwnd = user32.FindWindowExW(_HWND_MESSAGE, None, "BetterFlowEvents", None)
        assert hwnd, f"message window not found (error {ctypes.get_last_error()})"
        # A 64-bit lParam, as Windows passes for many messages.
        user32.SendMessageW(hwnd, _WM_WTSSESSION_CHANGE, _WTS_SESSION_LOCK, 0x0000_00D1_2345_6780)
        assert locked.wait(5), f"lock message did not reach on_screen_lock. Log: {log}"
    # WTS registration can be refused on a CI session; that is environment, not #277.
    assert not [r for r in caplog.records
                if r.levelno >= logging.WARNING and "WTS" not in r.getMessage()]
