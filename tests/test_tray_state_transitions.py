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
    """Construct a TrayIcon with the icon attribute pre-set to a MagicMock,
    and stub `_update_icon` / `_update_menu` to MagicMocks too.

    The icon/menu refresh paths import PIL + AppKit + pystray; on CI Linux
    those aren't available. We don't need to assert anything about them
    here — these tests are about model state under set_state(), not about
    rendering. Patching at the instance level keeps the tests focused.
    """
    with patch("src.ui.tray.pystray"):
        tray = TrayIcon(
            on_login=lambda: None,
            on_logout=lambda: None,
            on_pause=lambda: None,
            on_resume=lambda: None,
            on_quit=lambda: None,
        )
    tray._icon = MagicMock()
    # Stub the refresh hooks instance-wide. test_set_state_refreshes_menu...
    # below re-patches them with `patch.object` to assert call counts.
    tray._update_icon = MagicMock()
    tray._update_menu = MagicMock()
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


def test_entering_a_state_that_renders_the_field_clears_a_foreign_sentence():
    """Inverted by #214, deliberately — and this test's own docstring is the
    argument for inverting it.

    It used to assert that SYNCING → PAUSED preserves a leftover sentence, on
    the grounds that the writer might be "a permissions hint". That was safe
    only while PAUSED rendered the constant "Paused": the field was carried but
    never shown. PAUSED now renders ``status_text``, so preserving a foreign
    sentence means a permissions hint appearing under a Paused icon on a
    sleeping laptop — the exact leak the entry-clear exists to stop.

    A state that renders the field must be cleared on entry to it. That is one
    rule, and STATUS_TEXT_STATES is where it is written.
    """
    tray = _make_tray()

    tray.set_state(TrayState.SYNCING)
    tray.model.status_text = "Some informational status"
    tray.set_state(TrayState.PAUSED)

    assert tray.model.status_text is None
    assert tray._get_status_text() == "Paused"


def test_two_states_that_ignore_the_field_still_leave_it_alone():
    """The control, and the half of the original test that is still true.

    Neither SYNCING nor PRIVATE renders ``status_text``, so neither clear
    applies and a hint written by something else survives. Without this, a
    clear-everything fix would pass the test above while quietly discarding
    state no render path was harming.
    """
    tray = _make_tray()

    tray.set_state(TrayState.SYNCING)
    tray.model.status_text = "Some informational status"
    tray.set_state(TrayState.PRIVATE)

    assert tray.model.status_text == "Some informational status"


def test_status_text_covers_private_hours_and_on_break():
    """PRIVATE_HOURS and a bare ON_BREAK both fell through to "Starting...",
    so a user outside their working-hours window saw what looked like a hung
    app instead of a deliberate not-recording state."""
    tray = _make_tray()

    def status_for(state):
        return tray._get_status_text(
            {"on_break": False, "private_mode": False,
             "state": state, "break_minutes_left": 0}
        )

    assert status_for(TrayState.PRIVATE_HOURS) == "Outside working hours"
    assert status_for(TrayState.ON_BREAK) == "On Break"
    assert status_for(TrayState.PRIVATE) == "Private Time"
    # The real starting state must still read as starting.
    assert status_for(TrayState.STARTING) == "Starting..."
