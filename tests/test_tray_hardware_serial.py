"""The hardware serial as a visible, readable thing in the tray menu.

Two jobs. Support ("read me your serial" instead of a System Information
walkthrough) and, the bigger one, transparency: the agent now reports a durable
device identifier, so showing the user the exact value we hold is a stronger
honesty signal than a wizard bullet they clicked past months ago. It also makes
the collection self-evidently about the machine rather than about them.

**These must run headless.** pystray binds its display backend at import time,
so on a CI Linux runner ``src.ui.tray.pystray`` is ``None`` and ``TrayIcon()``
raises — the repo already works around this in ``test_tray_state_transitions``
and ``test_tray_icon`` by patching ``src.ui.tray.pystray``. A test that skips or
errors in the only environment that gates merges is not a guard, so nothing here
needs a live backend:

- the label/copyable rule is asserted on ``serial_menu_row()``, which is pure;
- the callsite guard renders the REAL ``_create_menu()`` with ``Item`` swapped
  for a recorder, so dropping the row from the production menu fails a test.

The second half is what keeps the first honest: without it ``serial_menu_row``
could be perfect and unreferenced (test-fixture-discipline.md, Phantom 3).
"""

import inspect
from unittest.mock import MagicMock, patch

import pytest

from src import hardware_serial as hw
from src.ui import tray as tray_mod
from src.ui.tray import TrayIcon, serial_menu_row


@pytest.fixture(autouse=True)
def _clear_serial_cache():
    hw.reset_cache_for_tests()
    yield
    hw.reset_cache_for_tests()


class _RecordedItem:
    """Stand-in for pystray.MenuItem that records what production asked for.

    Mirrors the bits of the real MenuItem the menu code and these tests touch:
    ``text``, ``enabled``, and calling the item to fire its action.
    """

    instances: list = []

    def __init__(self, text, action=None, enabled=True, **kwargs):
        self.text = text
        self.action = action
        self.enabled = enabled
        _RecordedItem.instances.append(self)

    def __call__(self, icon=None):
        if self.action is not None:
            self.action(icon, self)


def _render_real_menu(icon) -> list:
    """Drive the production _create_menu() and return every item it built."""
    _RecordedItem.instances = []
    with patch.object(tray_mod, "Item", _RecordedItem), \
            patch.object(tray_mod, "pystray", MagicMock()):
        icon._create_menu()
    return list(_RecordedItem.instances)


def _make_tray() -> TrayIcon:
    """Construct a TrayIcon without a display backend (repo-standard pattern)."""
    with patch.object(tray_mod, "pystray", MagicMock()):
        return TrayIcon()


def _serial_items(icon) -> list:
    return [i for i in _render_real_menu(icon) if "Device serial" in str(i.text)]


# ── The rule: serial_menu_row() ─────────────────────────────────────────

def test_row_shows_the_real_serial(monkeypatch):
    monkeypatch.setattr(hw, "_probe_serial", lambda: "C02Z60U3LVCJ")
    monkeypatch.setattr(tray_mod, "clipboard_available", lambda: True)
    assert serial_menu_row() == ("Device serial: C02Z60U3LVCJ", True)


def test_row_says_unavailable_when_there_is_no_serial(monkeypatch):
    """A VM or a locked-down Linux box must read as "not readable", not broken.

    Specifically NOT blank, not an empty tail after the colon, and never the
    Python repr "None" — which reads as a bug in the app rather than an honest
    absence.
    """
    monkeypatch.setattr(hw, "_probe_serial", lambda: None)
    monkeypatch.setattr(tray_mod, "clipboard_available", lambda: True)

    label, copyable = serial_menu_row()
    assert label == "Device serial: unavailable"
    assert label != "Device serial: None"
    assert label != "Device serial: "
    # Nothing to copy, so no affordance — clicking would copy the word
    # "unavailable" and look like it had worked.
    assert copyable is False


def test_row_is_not_copyable_without_a_clipboard(monkeypatch):
    """No clipboard tool on this box => an info row, not a dead button.

    The label still carries the value to read off.
    """
    monkeypatch.setattr(hw, "_probe_serial", lambda: "C02Z60U3LVCJ")
    monkeypatch.setattr(tray_mod, "clipboard_available", lambda: False)

    label, copyable = serial_menu_row()
    assert "C02Z60U3LVCJ" in label
    assert copyable is False


def test_row_does_not_reprobe(monkeypatch):
    """The menu rebuilds on every state change; the probe must not follow it."""
    calls = []

    def probe():
        calls.append(1)
        return "C02Z60U3LVCJ"

    monkeypatch.setattr(hw, "_probe_serial", probe)
    serial_menu_row()
    serial_menu_row()
    serial_menu_row()
    assert len(calls) == 1


# ── The callsite guard: the production menu really uses it ──────────────

def test_production_menu_renders_the_serial_row(monkeypatch):
    """Fails if the row is dropped from _create_menu, not only if the rule breaks."""
    monkeypatch.setattr(hw, "_probe_serial", lambda: "C02Z60U3LVCJ")
    monkeypatch.setattr(tray_mod, "clipboard_available", lambda: True)

    (item,) = _serial_items(_make_tray())
    assert str(item.text) == "Device serial: C02Z60U3LVCJ"
    assert item.enabled, "a copyable serial should not render greyed out"


def test_production_menu_renders_unavailable(monkeypatch):
    monkeypatch.setattr(hw, "_probe_serial", lambda: None)
    monkeypatch.setattr(tray_mod, "clipboard_available", lambda: True)

    (item,) = _serial_items(_make_tray())
    assert str(item.text) == "Device serial: unavailable"
    assert not item.enabled


def test_production_menu_label_agrees_with_the_row_builder(monkeypatch):
    """The rendered label matches serial_menu_row()'s output."""
    monkeypatch.setattr(hw, "_probe_serial", lambda: "C02Z60U3LVCJ")
    monkeypatch.setattr(tray_mod, "clipboard_available", lambda: True)

    (item,) = _serial_items(_make_tray())
    assert str(item.text) == serial_menu_row()[0]


def test_serial_item_delegates_instead_of_re_deriving_the_label():
    """Callsite guard — the menu must not grow its own copy of the rule.

    Comparing outputs cannot catch this: a parallel builder that happens to emit
    the same string passes every behavioural assertion above (verified — a
    hand-rolled f-string lookalike kept all 11 of them green). Only reading the
    consumer's source sees it, which is the pattern
    one-rule-one-implementation.md prescribes. The failure this prevents is the
    two copies drifting later, when only one of them gets a fix.
    """
    source = inspect.getsource(TrayIcon._serial_menu_item)
    # Assert on code, not prose — the docstring is free to discuss the probe.
    doc = TrayIcon._serial_menu_item.__doc__
    if doc:
        source = source.replace(doc, "")

    assert "serial_menu_row()" in source, "the menu stopped calling the shared builder"
    assert "Device serial" not in source, "label text re-derived at the callsite"
    assert "get_hardware_serial" not in source, "probe read a second time at the callsite"


def test_clicking_the_row_copies_the_real_serial(monkeypatch):
    monkeypatch.setattr(hw, "_probe_serial", lambda: "C02Z60U3LVCJ")
    monkeypatch.setattr(tray_mod, "clipboard_available", lambda: True)
    monkeypatch.setattr(tray_mod, "send_notification", lambda *a, **k: None)
    copied = []
    monkeypatch.setattr(
        tray_mod, "copy_to_clipboard", lambda text: (copied.append(text), True)[1]
    )

    (item,) = _serial_items(_make_tray())
    item(None)
    assert copied == ["C02Z60U3LVCJ"]


def test_uncopyable_row_has_no_action(monkeypatch):
    monkeypatch.setattr(hw, "_probe_serial", lambda: "C02Z60U3LVCJ")
    monkeypatch.setattr(tray_mod, "clipboard_available", lambda: False)

    (item,) = _serial_items(_make_tray())
    assert item.action is None
    assert not item.enabled


# ── The clipboard writer itself ─────────────────────────────────────────

def test_clipboard_writer_feeds_the_platform_command(monkeypatch):
    from src import clipboard

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs.get("input")

        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(clipboard, "_clipboard_command", lambda: ["/usr/bin/pbcopy"])
    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)

    assert clipboard.copy_to_clipboard("C02Z60U3LVCJ") is True
    assert seen["cmd"] == ["/usr/bin/pbcopy"]
    assert seen["input"] == "C02Z60U3LVCJ"


def test_clipboard_writer_reports_failure_rather_than_raising(monkeypatch):
    from src import clipboard

    monkeypatch.setattr(clipboard, "_clipboard_command", lambda: None)
    assert clipboard.copy_to_clipboard("C02Z60U3LVCJ") is False
    assert clipboard.clipboard_available() is False
