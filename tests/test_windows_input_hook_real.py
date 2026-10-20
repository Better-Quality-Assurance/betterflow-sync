"""The Windows low-level input hooks against the REAL Win32 API.

This is the one legitimate platform skip in the suite: it exercises the OS API
itself, which no fake can stand in for. Every unit test of _WindowsHookBackend
fakes ctypes, so the suite stayed green while every Windows device in the fleet
logged ``SetWindowsHookEx failed (kbd=0 mouse=0)`` once a minute and counted
zero keystrokes (device 34, 1.5.138: 480 occurrences over four days). The fakes
return small Python ints, so they cannot see a 64-bit HMODULE / HHOOK / lParam
being truncated to a 32-bit ``c_int`` by undeclared ctypes signatures — only a
64-bit Windows process can.

Runs on the windows-latest leg of build.yml (tags and workflow_dispatch).
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="real Win32 API: only a Windows process can exercise it"
)

_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_LBUTTONDOWN = 0x0201


def _backend():
    from src.sync import input_source as m

    src = m.InputSource(hostname="ci-host", backend=None, frontmost_app_getter=None)
    return src, m._WindowsHookBackend(src)


def _stop(be):
    be.stop()
    assert be.join(5.0), "hook thread did not exit after stop()"


def test_real_hooks_install_on_64bit_windows():
    """The fleet symptom, reproduced: start() must install BOTH hooks."""
    src, be = _backend()
    try:
        ok = be.start()
        assert ok is True, (
            f"start() returned {ok!r}: hooks kbd={be._kbd_hook!r} mouse={be._mouse_hook!r}"
        )
        assert be.available() is True
        assert be._kbd_hook, f"keyboard hook handle is {be._kbd_hook!r}"
        assert be._mouse_hook, f"mouse hook handle is {be._mouse_hook!r}"
    finally:
        _stop(be)
    assert be._kbd_hook is None and be._mouse_hook is None


def test_real_callbacks_chain_a_64bit_lparam_and_count(monkeypatch):
    """Drive the REAL callback thunks with lParam pointing at a real
    KBDLLHOOKSTRUCT / MSLLHOOKSTRUCT. The callback must chain it through
    CallNextHookEx without error and count the event.

    ctypes swallows an exception raised inside a callback (it is reported
    through sys.unraisablehook and the callback returns 0), so "no exception"
    is asserted by capturing that hook, not by a try/except."""
    import ctypes
    from ctypes import wintypes

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt", wintypes.POINT),
            ("mouseData", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    unraisable = []
    monkeypatch.setattr(sys, "unraisablehook", lambda u: unraisable.append(u))

    kbd_struct = KBDLLHOOKSTRUCT(vkCode=0x41)
    mouse_struct = MSLLHOOKSTRUCT()
    kbd_addr = ctypes.addressof(kbd_struct)
    mouse_addr = ctypes.addressof(mouse_struct)
    # Precondition, not the assertion under test: the witness only means
    # something if the pointer does not fit a 32-bit c_int.
    assert kbd_addr > 0x7FFFFFFF and mouse_addr > 0x7FFFFFFF, (
        f"heap pointers {kbd_addr:#x}/{mouse_addr:#x} fit in 32 bits; "
        "this run cannot witness lParam truncation"
    )

    src, be = _backend()
    try:
        be.start()
        assert be._kbd_cb is not None and be._mouse_cb is not None, (
            "callbacks were never built"
        )
        be._kbd_cb(0, _WM_KEYDOWN, kbd_addr)
        be._mouse_cb(0, _WM_LBUTTONDOWN, mouse_addr)
    finally:
        _stop(be)

    assert unraisable == [], [
        f"{u.exc_type.__name__}: {u.exc_value}" for u in unraisable
    ]
    assert src.counts == (1, 1, 0)


def test_real_callback_counts_a_held_key_once(monkeypatch):
    """A held key repeats WM_KEYDOWN for the same vkCode with no KEYUP. Through
    the REAL callback thunk and a real KBDLLHOOKSTRUCT pointer, the repeat must
    not be counted and a release must re-arm the key (device 34: every row
    scored implausible_input_rate once the hook started counting repeats)."""
    import ctypes
    from ctypes import wintypes

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    unraisable = []
    monkeypatch.setattr(sys, "unraisablehook", lambda u: unraisable.append(u))

    key_a = KBDLLHOOKSTRUCT(vkCode=0x41)
    addr = ctypes.addressof(key_a)

    src, be = _backend()
    try:
        be.start()
        assert be._kbd_cb is not None, "callback was never built"
        be._kbd_cb(0, _WM_KEYDOWN, addr)
        be._kbd_cb(0, _WM_KEYDOWN, addr)   # auto-repeat: same key, no KEYUP
        be._kbd_cb(0, _WM_KEYDOWN, addr)
        held = src.counts
        be._kbd_cb(0, _WM_KEYUP, addr)
        be._kbd_cb(0, _WM_KEYDOWN, addr)   # a real second press
    finally:
        _stop(be)

    assert unraisable == [], [
        f"{u.exc_type.__name__}: {u.exc_value}" for u in unraisable
    ]
    assert held == (1, 0, 0)
    assert src.counts == (2, 0, 0)
