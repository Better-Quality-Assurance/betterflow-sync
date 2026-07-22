"""The hardware serial as a visible, readable thing in the tray menu.

Two jobs. Support ("read me your serial" instead of a System Information
walkthrough) and, the bigger one, transparency: the agent now reports a durable
device identifier, so showing the user the exact value we hold is a stronger
honesty signal than a wizard bullet they clicked past months ago. It also makes
the collection self-evidently about the machine rather than about them.

These drive the real ``_create_menu()`` and read the labels it produced — the
label string is never mocked, or the test would only prove that a format string
formats.
"""

import pytest

from src import hardware_serial as hw
from src.ui import tray as tray_mod
from src.ui.tray import TrayIcon


@pytest.fixture(autouse=True)
def _clear_serial_cache():
    hw.reset_cache_for_tests()
    yield
    hw.reset_cache_for_tests()


def _menu_labels(icon) -> list[str]:
    """Flatten every label in the menu, submenus included."""
    labels: list[str] = []

    def walk(menu):
        for item in menu.items:
            labels.append(str(item.text))
            submenu = getattr(item, "submenu", None)
            if submenu is not None:
                walk(submenu)

    walk(icon._create_menu())
    return labels


def _serial_items(icon):
    """Every menu item whose label mentions the device serial."""
    found = []

    def walk(menu):
        for item in menu.items:
            if "Device serial" in str(item.text):
                found.append(item)
            submenu = getattr(item, "submenu", None)
            if submenu is not None:
                walk(submenu)

    walk(icon._create_menu())
    return found


def test_menu_shows_the_real_serial(monkeypatch):
    monkeypatch.setattr(hw, "_probe_serial", lambda: "C02Z60U3LVCJ")
    labels = _menu_labels(TrayIcon())
    assert "Device serial: C02Z60U3LVCJ" in labels


def test_menu_says_unavailable_when_there_is_no_serial(monkeypatch):
    """A VM or a locked-down Linux box must read as "not readable", not broken.

    Specifically NOT blank, not an empty tail after the colon, and never the
    Python repr "None" — which reads as a bug in the app rather than an honest
    absence.
    """
    monkeypatch.setattr(hw, "_probe_serial", lambda: None)
    labels = _menu_labels(TrayIcon())

    assert "Device serial: unavailable" in labels
    assert "Device serial: None" not in labels
    assert "Device serial: " not in labels


def test_serial_item_is_rendered_once(monkeypatch):
    """One rule, one implementation — no second call site for the UI."""
    monkeypatch.setattr(hw, "_probe_serial", lambda: "C02Z60U3LVCJ")
    assert len(_serial_items(TrayIcon())) == 1


def test_menu_does_not_reprobe_per_render(monkeypatch):
    """The menu rebuilds on every state change; the probe must not follow it."""
    calls = []

    def probe():
        calls.append(1)
        return "C02Z60U3LVCJ"

    monkeypatch.setattr(hw, "_probe_serial", probe)
    icon = TrayIcon()
    _menu_labels(icon)
    _menu_labels(icon)
    _menu_labels(icon)
    assert len(calls) == 1


def test_serial_item_copies_when_a_clipboard_exists(monkeypatch):
    """Clicking hands the REAL serial to the clipboard writer."""
    monkeypatch.setattr(hw, "_probe_serial", lambda: "C02Z60U3LVCJ")
    monkeypatch.setattr(tray_mod, "clipboard_available", lambda: True)
    copied = []
    monkeypatch.setattr(
        tray_mod, "copy_to_clipboard", lambda text: (copied.append(text), True)[1]
    )

    (item,) = _serial_items(TrayIcon())
    assert item.enabled, "a copyable serial should not render greyed out"

    item(None)  # pystray invokes the item with the icon; it forwards to the action
    assert copied == ["C02Z60U3LVCJ"]


def test_serial_item_is_inert_without_a_clipboard(monkeypatch):
    """No clipboard tool on this box => an info row, not a dead button.

    Offering a Copy affordance that silently does nothing is worse than not
    offering one; the label still carries the value to read off.
    """
    monkeypatch.setattr(hw, "_probe_serial", lambda: "C02Z60U3LVCJ")
    monkeypatch.setattr(tray_mod, "clipboard_available", lambda: False)

    (item,) = _serial_items(TrayIcon())
    assert not item.enabled
    assert "C02Z60U3LVCJ" in str(item.text)


def test_unavailable_serial_is_never_copyable(monkeypatch):
    """Nothing to copy, so no affordance — clicking would copy the word "unavailable"."""
    monkeypatch.setattr(hw, "_probe_serial", lambda: None)
    monkeypatch.setattr(tray_mod, "clipboard_available", lambda: True)

    (item,) = _serial_items(TrayIcon())
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
