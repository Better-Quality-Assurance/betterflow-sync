"""Tests for the tray's state-transition refresh path.

Recurring UX issue Tudor saw multiple times today:
1. bf-data-service blips → 1 failed sync → tray shows "App status: Error"
2. Next 30s tick succeeds → SyncCoordinator calls `set_state(SYNCING)`
3. Tray's icon color flips but the menu's "App status: Error" line stays
   stale until something else triggers `_update_menu` — usually the
   `update_stats` call right after, but if that races or the menu is
   already open, the user sees stale text on click.

Fix: `set_state` now calls `_update_menu` so the state transition
immediately refreshes the menu, and the stale `status_text` is cleared
on the ERROR/QUEUE_WARNING → healthy transition.
"""

from __future__ import annotations
from unittest.mock import MagicMock, patch

from src.ui.tray import TrayIcon, TrayState


def _make_tray() -> TrayIcon:
    """Construct a TrayIcon with the icon attribute pre-set to a MagicMock so
    `_update_icon` and `_update_menu` don't short-circuit on `self._icon`."""
    with patch("src.ui.tray.pystray"):
        tray = TrayIcon(
            on_login=lambda: None,
            on_logout=lambda: None,
            on_pause=lambda: None,
            on_resume=lambda: None,
            on_quit=lambda: None,
        )
    tray._icon = MagicMock()
    # Stub the actual menu rendering. set_state() now rebuilds the menu via
    # _create_menu() -> pystray.Menu, but pystray's backend can't initialize on
    # a headless CI box (no display), so the real call returns None and the
    # rebuild raises "NoneType object is not callable". These tests only assert
    # on model.status_text / state, not on rendered menu items, so stub the
    # render to keep them platform-independent.
    tray._create_menu = MagicMock(return_value=MagicMock())
    tray._apply_menu = MagicMock()
    return tray


def test_set_state_refreshes_menu_not_only_icon():
    """The bug: `set_state` only redrew the icon. The menu text stayed cached
    until something else triggered a rebuild. This pins that BOTH fire."""
    tray = _make_tray()

    with patch.object(tray, "_update_icon") as upd_icon, \
         patch.object(tray, "_update_menu") as upd_menu:
        tray.set_state(TrayState.SYNCING)

    upd_icon.assert_called_once()
    upd_menu.assert_called_once()


def test_recovery_from_error_clears_stale_status_text():
    """After an ERROR with status_text="ActivityWatch is not running", a
    recovery transition to SYNCING with no status_text must clear the
    stale text — not silently inherit it.
    """
    tray = _make_tray()

    tray.set_state(TrayState.ERROR, "ActivityWatch is not running")
    assert tray.model.status_text == "ActivityWatch is not running"

    # Recovery: sync succeeded, coordinator calls set_state(SYNCING) with
    # no explanatory text (the success branch passes no second arg).
    tray.set_state(TrayState.SYNCING)

    assert tray.model.state == TrayState.SYNCING
    assert tray.model.status_text is None, (
        "Stale error text from a previous transition must be cleared on "
        "recovery — otherwise it leaks into the next thing that displays it."
    )


def test_recovery_from_queue_warning_clears_stale_status_text():
    """Same rule for QUEUE_WARNING → healthy."""
    tray = _make_tray()

    tray.set_state(TrayState.QUEUE_WARNING, "Queue 80% full")
    tray.set_state(TrayState.SYNCING)

    assert tray.model.status_text is None


def test_passing_explicit_status_text_still_overrides():
    """The explicit-set path stays intact — passing a non-None status_text
    must write it, regardless of the previous state."""
    tray = _make_tray()

    tray.set_state(TrayState.ERROR, "First")
    tray.set_state(TrayState.SYNCING)  # clears
    tray.set_state(TrayState.ERROR, "Second")

    assert tray.model.status_text == "Second"


def test_transition_between_two_healthy_states_preserves_existing_text():
    """If status_text was set by something OTHER than ERROR/QUEUE_WARNING
    (e.g. permissions hint), a transition between two non-error states
    must NOT clear it — only the explicit recovery path clears."""
    tray = _make_tray()

    tray.set_state(TrayState.SYNCING)
    tray.model.status_text = "Some informational status"
    tray.set_state(TrayState.PAUSED)

    assert tray.model.status_text == "Some informational status"
