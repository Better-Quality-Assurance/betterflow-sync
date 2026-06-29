"""Tray wiring for the working-hours capture gate.

The gate decision is tested in test_work_schedule_gate; here we pin the thin
tray layer: set_schedule_state updates the model + rebuilds the menu only on a
real change, the "Work outside hours" item appears only when offered, and
clicking it delegates to the app callback.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.ui.tray import TrayIcon


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
    labels = [getattr(i, "text", None) for i in tray._create_menu().items]
    assert any("Work outside hours" in (t or "") for t in labels)

    tray.set_schedule_state(suspended=False, offer_override=False)
    labels = [getattr(i, "text", None) for i in tray._create_menu().items]
    assert not any("Work outside hours" in (t or "") for t in labels)


def test_clicking_override_delegates_to_callback():
    called = MagicMock()
    tray = _make_tray(on_work_outside_hours=called)
    tray._handle_work_outside_hours(tray._icon, MagicMock())
    called.assert_called_once()
