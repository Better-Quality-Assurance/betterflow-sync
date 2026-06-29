"""Tray wiring for the working-hours capture gate.

The gate decision is tested in test_work_schedule_gate; here we pin the thin
tray layer: set_schedule_state updates the model + rebuilds the menu only on a
real change, the "Work outside hours" item appears only when offered, and
clicking it delegates to the app callback.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.ui.tray import TrayIcon


class _FakeItem:
    """Stand-in for pystray.MenuItem exposing the .text the assertions read.

    The real pystray (and thus ``src.ui.tray.Item``) is None on a headless box
    where the backend can't bind — e.g. Linux CI — so calling the real
    ``_create_menu`` there raises ``'NoneType' object is not callable``. Faking
    Item/Menu keeps the test exercising the real menu-building logic without
    depending on a usable tray backend.
    """

    def __init__(self, text=None, action=None, *args, **kwargs):
        self.text = text
        self.action = action


class _FakeMenu:
    """Stand-in for pystray.Menu exposing .items as the passed tuple."""

    def __init__(self, *items):
        self.items = items


def _menu_labels(tray) -> list:
    """Build the menu under faked pystray/Item and return top-level item texts."""
    fake_pystray = MagicMock()
    fake_pystray.Menu = _FakeMenu
    with patch("src.ui.tray.pystray", fake_pystray), patch("src.ui.tray.Item", _FakeItem):
        menu = tray._create_menu()
    return [getattr(i, "text", None) for i in menu.items]


def _make_tray(on_work_outside_hours=None) -> TrayIcon:
    with patch("src.ui.tray.pystray"):
        tray = TrayIcon(
            on_login=lambda: None,
            on_logout=lambda: None,
            on_quit=lambda: None,
            on_work_outside_hours=on_work_outside_hours,
        )
    tray._icon = MagicMock()
    tray._update_icon = MagicMock()
    tray._update_menu = MagicMock()
    return tray


def test_set_schedule_state_updates_model_and_rebuilds():
    tray = _make_tray()
    with patch.object(tray, "_update_menu") as upd:
        tray.set_schedule_state(suspended=True, offer_override=True)
    assert tray.model.schedule_suspended is True
    assert tray.model.schedule_offer_override is True
    upd.assert_called_once()


def test_set_schedule_state_noop_when_unchanged():
    """The 60s gate check pushes the same state repeatedly; an unchanged push
    must not churn the menu."""
    tray = _make_tray()
    tray.set_schedule_state(suspended=False, offer_override=False)  # already the default
    with patch.object(tray, "_update_menu") as upd:
        tray.set_schedule_state(suspended=False, offer_override=False)
    upd.assert_not_called()


def test_override_item_present_only_when_offered():
    tray = _make_tray()
    tray.model.user_email = "x@y.co"  # logged-in so items render

    tray.set_schedule_state(suspended=True, offer_override=True)
    labels = _menu_labels(tray)
    assert any("Work outside hours" in (t or "") for t in labels)

    tray.set_schedule_state(suspended=False, offer_override=False)
    labels = _menu_labels(tray)
    assert not any("Work outside hours" in (t or "") for t in labels)


def test_clicking_override_delegates_to_callback():
    called = MagicMock()
    tray = _make_tray(on_work_outside_hours=called)
    tray._handle_work_outside_hours(tray._icon, MagicMock())
    called.assert_called_once()
