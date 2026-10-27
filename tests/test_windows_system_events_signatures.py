"""The Windows sleep/wake/lock listener must survive 64-bit handles (#277).

There is no Windows box in PR CI, so these drive ``_start_windows_listener``
against fake DLLs that model the two ctypes behaviours that broke it, at the
DLL boundary only (the listener's own code runs unmodified):

* a call to a function with no ``argtypes`` converts every int to a 32-bit
  ``c_int`` and raises ``ArgumentError`` for a larger one (the real default);
* a function with no ``restype`` returns a ``c_int``, so a 64-bit HMODULE is
  truncated.

Windows sends WM_NCCREATE through the window procedure, with a pointer-sized
lParam, before CreateWindowExW returns; if the procedure answers 0 the window is
not created. The fake CreateWindowExW does the same. Whether real Windows
behaves this way for THIS listener is unverified until a windows-latest run.
"""

import ctypes
import logging
import threading
import types

import pytest

from src import system_events as se

WM_NCCREATE = 0x0081
WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK = 0x7
BIG_HMODULE = 0x7FF6_1234_0000      # a typical 64-bit image base
BIG_LPARAM = 0x0000_00D1_2345_6780  # a 64-bit CREATESTRUCT pointer
HWND = 0x1234


class _FakeFn:
    def __init__(self, impl):
        self._impl = impl

    def __call__(self, *args):
        if hasattr(self, "argtypes"):
            for a, t in zip(args, self.argtypes, strict=False):
                if isinstance(a, int) and ctypes.sizeof(t) < 8 and not -(2**31) <= a < 2**31:
                    raise ctypes.ArgumentError("int too long to convert")
        else:
            for a in args:
                if isinstance(a, int) and not -(2**31) <= a < 2**31:
                    raise ctypes.ArgumentError("int too long to convert")
        r = self._impl(*args)
        restype = getattr(self, "restype", ctypes.c_int)
        if isinstance(r, int) and restype is not None and ctypes.sizeof(restype) < 8:
            r = ctypes.c_int(r).value
        return r


class _FakeDLL:
    def __init__(self, impls):
        self._fns = {k: _FakeFn(v) for k, v in impls.items()}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._fns[name]


class _Win:
    """A minimal Windows: one class registry, one message pump."""

    def __init__(self):
        self.wndproc = None
        self.pump_entered = threading.Event()
        self.release = threading.Event()
        self.created_with_hinstance = None

    def register(self, pwc):
        self.wndproc = pwc._obj.lpfnWndProc
        return 0xC123

    def create(self, *args):
        self.created_with_hinstance = args[10]
        if not self.wndproc(HWND, WM_NCCREATE, 0, BIG_LPARAM):
            return 0
        return HWND

    def get_message(self, *args):
        self.pump_entered.set()
        self.release.wait(5)
        return 0

    def dlls(self):
        return {
            "user32": _FakeDLL({
                "DefWindowProcW": lambda h, m, w, lp: 1 if m == WM_NCCREATE else 0,
                "RegisterClassW": self.register,
                "CreateWindowExW": self.create,
                "GetMessageW": self.get_message,
                "TranslateMessage": lambda *a: 0,
                "DispatchMessageW": lambda *a: 0,
            }),
            "kernel32": _FakeDLL({"GetModuleHandleW": lambda name: BIG_HMODULE}),
            "wtsapi32": _FakeDLL({"WTSRegisterSessionNotification": lambda h, f: 1}),
        }


@pytest.fixture
def win(monkeypatch):
    w = _Win()
    dlls = w.dlls()
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(**dlls), raising=False)
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda name, use_last_error=False: dlls[name], raising=False,
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)
    yield w
    w.release.set()


def test_listener_starts_with_64bit_handles_and_reports_a_lock(win, caplog):
    locked = threading.Event()
    with caplog.at_level(logging.INFO, logger=se.__name__):
        se._start_windows_listener(
            on_sleep=lambda: None, on_wake=lambda: None, on_shutdown=lambda: None,
            on_screen_lock=locked.set, on_screen_unlock=lambda: None,
        )
        assert win.pump_entered.wait(5), (
            "message pump never reached: window creation failed. Log: "
            + " | ".join(r.getMessage() for r in caplog.records)
        )
        assert win.created_with_hinstance == BIG_HMODULE  # not truncated
        assert win.wndproc(HWND, WM_WTSSESSION_CHANGE, WTS_SESSION_LOCK, BIG_LPARAM) == 0
    assert locked.is_set()
    starts = [r for r in caplog.records if r.getMessage() == "Windows system event listener started"]
    assert starts and starts[0].levelno == logging.INFO


def test_listener_does_not_declare_signatures_on_the_shared_windll(monkeypatch, win):
    """Setting argtypes on ctypes.windll.* would change every other module's calls."""
    poisoned = types.SimpleNamespace()  # any attribute access raises AttributeError
    monkeypatch.setattr(ctypes, "windll", poisoned, raising=False)
    se._start_windows_listener(lambda: None, lambda: None, lambda: None)
    assert win.pump_entered.wait(5)


def test_getmessage_keeps_its_negative_error_return():
    """The pump's ``> 0`` loop guard needs a SIGNED restype to stop on -1."""
    from ctypes import wintypes
    u, k, w = (types.SimpleNamespace(**{n: types.SimpleNamespace() for n in names})
               for names in (("DefWindowProcW", "RegisterClassW", "CreateWindowExW",
                              "GetMessageW", "TranslateMessage", "DispatchMessageW"),
                             ("GetModuleHandleW",), ("WTSRegisterSessionNotification",)))
    wndproc, _ = se._declare_win32_listener_signatures(u, k, w, ctypes.CFUNCTYPE)
    assert u.GetMessageW.restype(-1).value == -1
    assert ctypes.sizeof(wndproc._restype_) == ctypes.sizeof(ctypes.c_void_p)
    assert ctypes.sizeof(k.GetModuleHandleW.restype) == ctypes.sizeof(ctypes.c_void_p)
    assert u.DefWindowProcW.argtypes[3] is wintypes.LPARAM


def test_no_32bit_long_types_in_the_declarations():
    """``c_long`` is 8 bytes on macOS/Linux but 4 on Windows, so on this CI a
    ``c_long`` LRESULT/LPARAM is indistinguishable by size from the right type
    and the #277 truncation would come back unseen (the pre-fix WNDPROC used
    ``c_long``). Pointer-sized slots must be spelled with ``wintypes``."""
    import inspect
    import re

    src = inspect.getsource(se._declare_win32_listener_signatures)
    code = "\n".join(line.split("#")[0] for line in src.splitlines() if '"""' not in line)
    assert not re.search(r"\bc_u?long\b", code)
