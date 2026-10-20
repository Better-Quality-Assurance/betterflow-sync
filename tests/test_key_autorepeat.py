"""A held key's auto-repeat is ONE key press, not thirty a second.

Device 34 (v1.5.140-beta.2) carried ``fraud_score_client = 30``
(``implausible_input_rate``, >400 presses/min) on every row once the Windows
hook installed: the hook counted every WM_KEYDOWN, and Windows emits one per
auto-repeat tick for a held key (arrow keys, backspace), so holding a key for
20 s added ~600 "presses". macOS's CGEventTap counted every kCGEventKeyDown the
same way, auto-repeats included.

The Windows cases drive the REAL ``_kbd_proc`` closure built inside
``_run_hooks`` (fake DLLs, real ctypes structs), so they run on every PR's
ubuntu job; the real-API counterpart is tests/test_windows_input_hook_real.py.
"""

import ctypes
import sys
import types
from ctypes import wintypes
from unittest.mock import patch

from src.sync import input_source as m
from src.sync import macos_input_watcher as miw

WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
VK_A, VK_B, VK_MENU = 0x41, 0x42, 0x12


class KBDLLHOOKSTRUCT(ctypes.Structure):
    # Written from the Win32 docs, NOT imported from the code under test, so a
    # wrong production layout cannot agree with itself.
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _FakeDLL:
    def __init__(self, **impls):
        self._impls = impls

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._impls.get(name, lambda *a: 0)


class _Harness:
    """A _WindowsHookBackend whose hooks 'install' into a dict, so the real
    callbacks can be invoked with real KBDLLHOOKSTRUCT pointers on any OS."""

    def __init__(self, monkeypatch):
        monkeypatch.setattr(m.platform, "system", lambda: "Windows")
        self.src = m.InputSource(hostname="host", backend=None, frontmost_app_getter=None)
        self.be = m._WindowsHookBackend(self.src)
        self.callbacks = {}
        self.chained = []

        def _set_hook(kind, cb, hmod, tid):
            self.callbacks[kind] = cb
            return 1

        def _next(hook, n, w, lp):
            self.chained.append((n, w, lp))
            return 0

        self.during_pump = None

        def _get_message(*a):
            # The hooks are live only while the pump runs; afterwards _run_hooks
            # unhooks. ``during_pump`` lets a test press keys inside that window.
            action, self.during_pump = self.during_pump, None
            if action:
                action()
            return 0

        self.user32 = _FakeDLL(SetWindowsHookExW=_set_hook, CallNextHookEx=_next,
                               GetMessageW=_get_message)
        kernel32 = _FakeDLL(GetModuleHandleW=lambda *a: 1, GetCurrentThreadId=lambda: 7)
        self.be._api_loader = lambda: (self.user32, kernel32, lambda fn: fn)
        self._keep = []   # keep structs alive while their address is in use

    def install(self):
        # GetMessageW -> 0: the pump returns at once, then the hooks unhook.
        self.be._run_hooks()
        return self.callbacks[self.be._WH_KEYBOARD_LL]

    def key(self, msg, vk):
        s = KBDLLHOOKSTRUCT(vkCode=vk)
        self._keep.append(s)
        return self.callbacks[self.be._WH_KEYBOARD_LL](0, msg, ctypes.addressof(s))

    @property
    def presses(self):
        return self.src.counts[0]


def test_held_key_auto_repeat_counts_once_and_release_rearms(monkeypatch):
    h = _Harness(monkeypatch)
    h.install()
    for _ in range(30):            # one press held through 29 auto-repeats
        h.key(WM_KEYDOWN, VK_A)
    assert h.presses == 1
    h.key(WM_KEYUP, VK_A)
    h.key(WM_KEYDOWN, VK_A)        # a real second press
    assert h.presses == 2


def test_repeat_of_a_key_held_under_another_key_is_not_counted(monkeypatch):
    h = _Harness(monkeypatch)
    h.install()
    h.key(WM_KEYDOWN, VK_A)
    h.key(WM_KEYDOWN, VK_B)
    h.key(WM_KEYDOWN, VK_A)        # A still held: auto-repeat
    assert h.presses == 2


def test_syskeydown_repeats_are_deduped_and_either_up_releases(monkeypatch):
    h = _Harness(monkeypatch)
    h.install()
    for _ in range(10):
        h.key(WM_SYSKEYDOWN, VK_MENU)
    assert h.presses == 1
    h.key(WM_SYSKEYUP, VK_MENU)
    h.key(WM_SYSKEYDOWN, VK_MENU)
    assert h.presses == 2
    h.key(WM_KEYUP, VK_MENU)       # Alt's release can arrive as plain WM_KEYUP
    h.key(WM_SYSKEYDOWN, VK_MENU)
    assert h.presses == 3


def test_unreadable_lparam_counts_the_press_and_still_chains(monkeypatch):
    """Fail toward today's behaviour: an unreadable struct is a counted press,
    never an exception inside the hook (which would skip CallNextHookEx)."""
    h = _Harness(monkeypatch)
    kbd = h.install()
    kbd(0, WM_KEYDOWN, 0)
    kbd(0, WM_KEYDOWN, 0)
    kbd(0, WM_KEYDOWN, None)
    assert h.presses == 3
    assert [c[1] for c in h.chained] == [WM_KEYDOWN] * 3


def test_install_clears_a_held_set_left_by_an_earlier_hook(monkeypatch):
    """A KEYUP missed while unhooked must not suppress the next real press.
    The press is made while the new hook is LIVE (inside the pump), so only
    the install-time clear can make it count."""
    h = _Harness(monkeypatch)
    h.be._held_keys.add(VK_A)      # stale: A's KEYUP happened while unhooked
    h.during_pump = lambda: h.key(WM_KEYDOWN, VK_A)
    h.install()
    assert h.presses == 1


def test_unhook_clears_the_held_set(monkeypatch):
    h = _Harness(monkeypatch)
    h.install()
    h.key(WM_KEYDOWN, VK_A)
    h.key(WM_KEYDOWN, VK_A)        # repeat: proves A is held
    assert h.presses == 1
    h.be._unhook(h.user32)
    h.key(WM_KEYDOWN, VK_A)
    assert h.presses == 2


def test_held_set_is_bounded_and_self_heals(monkeypatch):
    """Lost KEYUPs pile up; past the bound the set is cleared, so a stuck key
    is counted again rather than suppressed forever."""
    h = _Harness(monkeypatch)
    h.install()
    h.key(WM_KEYDOWN, VK_A)        # A's KEYUP will never arrive
    for vk in range(0x100, 0x100 + 40):
        h.key(WM_KEYDOWN, vk)
    assert len(h.be._held_keys) <= 32
    before = h.presses
    h.key(WM_KEYDOWN, VK_A)
    assert h.presses == before + 1


def test_key_identity_never_reaches_a_log(monkeypatch, caplog):
    """The vkCode exists only to recognise a repeat (CLAUDE.md: input is
    counts only)."""
    import logging

    h = _Harness(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        h.install()
        for vk in (VK_A, 0x5A, 0x5A):
            h.key(WM_KEYDOWN, vk)
            h.key(WM_KEYUP, vk)
    for rec in caplog.records:
        text = rec.getMessage()
        assert "0x41" not in text and "0x5a" not in text.lower() and "90" not in text


# --- macOS --------------------------------------------------------------------

class _Evt:
    def __init__(self, autorepeat):
        self.autorepeat = autorepeat


def _fake_quartz(reads):
    q = types.ModuleType("Quartz")

    def get_field(event, field):
        reads.append(field)
        if isinstance(event, _Evt):
            return event.autorepeat
        raise TypeError("not a CGEvent")

    q.CGEventGetIntegerValueField = get_field
    return q


def test_macos_autorepeat_keydown_is_not_counted_but_is_input():
    w = miw.MacOSInputWatcher(aw_client=object(), count_only=True)
    reads = []
    with patch.dict(sys.modules, {"Quartz": _fake_quartz(reads)}):
        w._event_callback(None, miw._kCGEventKeyDown, _Evt(0), None)
        w._last_input_at = None
        for _ in range(20):
            w._event_callback(None, miw._kCGEventKeyDown, _Evt(1), None)
    assert w._presses == 1
    assert w._last_input_at is not None, "a repeat is real input: liveness must move"
    assert set(reads) == {8}       # kCGKeyboardEventAutorepeat


def test_macos_unreadable_autorepeat_field_counts_the_press():
    w = miw.MacOSInputWatcher(aw_client=object(), count_only=True)
    with patch.dict(sys.modules, {"Quartz": _fake_quartz([])}):
        w._event_callback(None, miw._kCGEventKeyDown, object(), None)   # getter raises
    with patch.dict(sys.modules, {"Quartz": None}):                      # import fails
        w._event_callback(None, miw._kCGEventKeyDown, _Evt(1), None)
    assert w._presses == 2


def test_macos_mouse_counting_never_reads_the_autorepeat_field():
    w = miw.MacOSInputWatcher(aw_client=object(), count_only=True)
    reads = []
    with patch.dict(sys.modules, {"Quartz": _fake_quartz(reads)}):
        w._event_callback(None, miw._kCGEventLeftMouseDown, _Evt(1), None)
        w._event_callback(None, miw._kCGEventScrollWheel, _Evt(1), None)
    assert (w._presses, w._clicks, w._scrolls) == (0, 1, 1)
    assert reads == []
