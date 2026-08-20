"""The capture-permission state as something the user can LOOK AT.

#204: the agent asks for a missing Accessibility grant via `send_notification`,
and `_send_macos_pyobjc` returns True whenever `deliverNotification_` did not
raise -- which it never does, authorised or not. macOS silently discards
notifications from an app that has never been granted permission to post them,
so the one moment the message matters is the moment the channel is least likely
to work. Five devices sat with empty window titles for up to 21 days; four were
already running the release that added that prompt.

#204's own suggested direction leads with the durable half: "Do not just make
the toast louder. Sending the CAUSE to a surface that persists beats a
notification that may never render." This is that surface. A tray row cannot be
dropped by the notification centre, can be read at any time, and answers the
question the user actually has -- is my machine recording titles, and if not,
what do I do.

The wording carries the one non-obvious fact: on a signature-invalidated grant
System Settings shows the toggle ON, so "enable it" is a no-op instruction and
the remedy is to toggle it OFF and back ON.

**These must run headless.** pystray binds its display backend at import time,
so on the ubuntu PR runner `src.ui.tray.pystray` is None and TrayIcon() raises.
Same workaround as test_tray_hardware_serial: the label rule is asserted on the
pure `capture_permissions_row()`, and the callsite guard renders the REAL
`_create_menu()` with Item swapped for a recorder. Without that second half the
row could be perfect and unreferenced (Phantom 3).
"""

from unittest.mock import MagicMock, patch

from src.ui import tray as tray_mod
from src.ui.tray import TrayIcon, capture_permissions_row


class _RecordedItem:
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
    _RecordedItem.instances = []
    with patch.object(tray_mod, "Item", _RecordedItem), \
            patch.object(tray_mod, "pystray", MagicMock()):
        icon._create_menu()
    return list(_RecordedItem.instances)


def _make_tray() -> TrayIcon:
    with patch.object(tray_mod, "pystray", MagicMock()):
        return TrayIcon()


# ── The rule: capture_permissions_row() ────────────────────────────────

def test_row_is_quiet_when_both_grants_are_held():
    row = capture_permissions_row(system="Darwin", accessibility=True, input_monitoring=True)
    assert row == "Capture permissions: OK"


def test_row_names_accessibility_and_gives_the_off_then_on_remedy():
    """"Enable it" is the instruction that failed. This row must not repeat it."""
    row = capture_permissions_row(system="Darwin", accessibility=False, input_monitoring=True)
    assert "Accessibility" in row
    assert "window titles" in row.lower()
    assert "off" in row.lower() and "on" in row.lower()
    assert "Input Monitoring" not in row


def test_row_names_input_monitoring_without_blaming_titles():
    """Different grant, different consequence, different System Settings pane."""
    row = capture_permissions_row(system="Darwin", accessibility=True, input_monitoring=False)
    assert "Input Monitoring" in row
    assert "window titles" not in row.lower()
    assert "Accessibility" not in row


def test_row_names_BOTH_when_both_are_missing():
    """The state a fresh install or a post-update signature change produces."""
    row = capture_permissions_row(system="Darwin", accessibility=False, input_monitoring=False)
    assert "Accessibility" in row
    assert "Input Monitoring" in row


def test_row_is_omitted_off_macos():
    """These two grants are a macOS concept. A row about them elsewhere is noise."""
    assert capture_permissions_row(system="Windows", accessibility=True, input_monitoring=True) is None
    assert capture_permissions_row(system="Linux", accessibility=True, input_monitoring=True) is None


# ── The callsite guard: the row must reach the real menu ───────────────

def test_the_row_is_actually_in_the_diagnostics_menu(monkeypatch):
    """Phantom 3: a perfect row nothing renders is worth nothing."""
    monkeypatch.setattr(tray_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(tray_mod, "check_accessibility", lambda: False)
    monkeypatch.setattr(tray_mod, "check_input_monitoring", lambda: True)
    icon = _make_tray()
    texts = [str(i.text) for i in _render_real_menu(icon)]
    assert any("Accessibility" in t for t in texts), (
        f"capture-permission row missing from the rendered menu: {texts}"
    )


def test_no_capture_row_is_rendered_off_macos(monkeypatch):
    monkeypatch.setattr(tray_mod, "check_accessibility", lambda: False)
    monkeypatch.setattr(tray_mod, "check_input_monitoring", lambda: False)
    monkeypatch.setattr(tray_mod.platform, "system", lambda: "Linux")
    icon = _make_tray()
    texts = [str(i.text) for i in _render_real_menu(icon)]
    assert not any("Capture permissions" in t or "re-grant" in t for t in texts)
