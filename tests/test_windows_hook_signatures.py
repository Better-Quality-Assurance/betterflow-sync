"""Cross-platform guards on the Windows hook backend's ctypes signatures.

The real proof is tests/test_windows_input_hook_real.py, which only runs on the
windows-latest build leg (tags / workflow_dispatch). These run on every PR's
ubuntu job, so a removed or narrowed declaration reddens CI before a release.

Background: with no argtypes/restype, ctypes truncated the 64-bit HMODULE from
GetModuleHandleW to a 32-bit c_int, SetWindowsHookExW refused both hooks, and
every Windows device counted zero input."""

import ast
import ctypes
import inspect
import logging
import textwrap
from ctypes import wintypes

import pytest

from src.sync import input_source as m

PTR = ctypes.sizeof(ctypes.c_void_p)


class _FakeFn:
    """A DLL function stand-in that records the declarations set on it."""

    def __init__(self, name, impl=None):
        self.name = name
        self._impl = impl
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self._impl(*args) if self._impl else 0


class _FakeDLL:
    def __init__(self, **impls):
        self._impls = impls
        self.fns = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        fn = self.fns.get(name)
        if fn is None:
            fn = self.fns[name] = _FakeFn(name, self._impls.get(name))
        return fn


def _declared():
    user32, kernel32 = _FakeDLL(), _FakeDLL()
    hookproc = m._declare_win32_hook_signatures(user32, kernel32, ctypes.CFUNCTYPE)
    return user32, kernel32, hookproc


def _backend_win32_calls():
    """Every ``user32.X(...)`` / ``kernel32.X(...)`` call in _WindowsHookBackend,
    read from the source, so a call added later without a declaration fails."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(m._WindowsHookBackend)))
    calls = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in ("user32", "kernel32")):
            calls.add((node.func.value.id, node.func.attr))
    return calls


def test_every_win32_call_in_the_backend_has_declared_signatures():
    user32, kernel32, _ = _declared()
    calls = _backend_win32_calls()
    # Control: the source scan must see the calls that broke the fleet.
    assert {("kernel32", "GetModuleHandleW"), ("user32", "SetWindowsHookExW"),
            ("user32", "CallNextHookEx"), ("user32", "PostThreadMessageW"),
            ("user32", "GetMessageW")} <= calls
    dlls = {"user32": user32, "kernel32": kernel32}
    undeclared = sorted(
        f"{dll}.{fn}" for dll, fn in calls
        if not (hasattr(dlls[dll].fns.get(fn), "argtypes")
                and hasattr(dlls[dll].fns.get(fn), "restype"))
    )
    assert undeclared == []


def test_handles_and_lparam_are_pointer_sized():
    user32, kernel32, hookproc = _declared()
    assert ctypes.sizeof(kernel32.GetModuleHandleW.restype) == PTR
    set_hook = user32.SetWindowsHookExW
    assert ctypes.sizeof(set_hook.restype) == PTR          # HHOOK
    assert set_hook.argtypes[1] is hookproc
    assert ctypes.sizeof(set_hook.argtypes[2]) == PTR      # HINSTANCE
    nxt = user32.CallNextHookEx
    assert ctypes.sizeof(nxt.argtypes[0]) == PTR           # HHOOK
    assert ctypes.sizeof(nxt.argtypes[2]) == PTR           # WPARAM
    assert ctypes.sizeof(nxt.argtypes[3]) == PTR           # LPARAM
    assert ctypes.sizeof(nxt.restype) == PTR               # LRESULT
    assert ctypes.sizeof(user32.UnhookWindowsHookEx.argtypes[0]) == PTR


def test_hookproc_returns_lresult_and_takes_pointer_sized_lparam():
    _, _, hookproc = _declared()
    assert ctypes.sizeof(hookproc._restype_) == PTR
    assert hookproc._argtypes_[0] is ctypes.c_int
    assert ctypes.sizeof(hookproc._argtypes_[1]) == PTR
    assert ctypes.sizeof(hookproc._argtypes_[2]) == PTR


def test_getmessage_keeps_its_negative_error_return():
    """The pump's -1 branch depends on a SIGNED restype."""
    user32, _, _ = _declared()
    assert user32.GetMessageW.restype(-1).value == -1


def test_loader_uses_private_windlls_never_the_global_windll(monkeypatch):
    made = []

    def _fake_windll_ctor(name, use_last_error=False):
        made.append((name, use_last_error))
        return _FakeDLL()

    class _Poisoned:
        def __getattr__(self, name):
            raise AssertionError(f"global ctypes.windll.{name} touched")

    monkeypatch.setattr(ctypes, "WinDLL", _fake_windll_ctor, raising=False)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    monkeypatch.setattr(ctypes, "windll", _Poisoned(), raising=False)

    user32, kernel32, hookproc = m._load_win32_hook_api()

    assert sorted(made) == [("kernel32", True), ("user32", True)]
    assert hasattr(user32.SetWindowsHookExW, "argtypes")
    assert hasattr(kernel32.GetModuleHandleW, "restype")
    assert ctypes.sizeof(hookproc._restype_) == PTR


def _windows_backend(monkeypatch):
    monkeypatch.setattr(m.platform, "system", lambda: "Windows")
    src = m.InputSource(hostname="host", backend=None, frontmost_app_getter=None)
    return src, m._WindowsHookBackend(src)


def test_refused_install_logs_the_windows_error_number(monkeypatch, caplog):
    """A field report must name the Windows error, not just "kbd=0"."""
    _, be = _windows_backend(monkeypatch)
    user32 = _FakeDLL(SetWindowsHookExW=lambda *a: None)
    kernel32 = _FakeDLL(GetModuleHandleW=lambda *a: 0x7FF6_0000_0000)
    be._api_loader = lambda: (user32, kernel32, lambda fn: fn)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 126, raising=False)

    with caplog.at_level(logging.WARNING, logger=m.logger.name):
        be._run_hooks()

    assert be._install_failed is True
    msgs = [r.getMessage() for r in caplog.records]
    assert any("kbd=None err=126 mouse=None err=126" in s for s in msgs), msgs
    # The full 64-bit module handle reached SetWindowsHookExW unmodified.
    assert [c[2] for c in user32.SetWindowsHookExW.calls] == [0x7FF6_0000_0000] * 2


def test_stop_posts_wm_quit_through_the_backend_dll_not_global_windll(monkeypatch):
    _, be = _windows_backend(monkeypatch)

    class _Poisoned:
        def __getattr__(self, name):
            raise AssertionError(f"global ctypes.windll.{name} touched")

    monkeypatch.setattr(ctypes, "windll", _Poisoned(), raising=False)
    user32 = _FakeDLL()
    be._user32 = user32
    be._thread_id = 4242
    be.stop()
    assert user32.PostThreadMessageW.calls == [(4242, 0x0012, 0, 0)]


def test_stop_without_a_loaded_dll_is_a_no_op(monkeypatch):
    _, be = _windows_backend(monkeypatch)
    be._thread_id = 4242   # id without the DLL: nothing to post through
    be.stop()              # must not raise


@pytest.mark.parametrize("name", ["HHOOK", "HMODULE", "HINSTANCE", "LPMSG"])
def test_wintypes_used_exist_on_this_python(name):
    """The declarations import these at hook-install time on Windows; a
    missing name would fail every install, so pin their presence."""
    assert isinstance(getattr(wintypes, name), type)

