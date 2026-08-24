"""A failed sync while offline must read "Offline", not "Error".

When there's no internet (incl. Wi-Fi "connected" but no route, which the OS
network monitor doesn't always flag), a sync failure should put the tray into
the queued/Offline state — we're still tracking and queuing locally — instead
of a scary "App status: Error".
"""

from unittest.mock import MagicMock, patch

from src.main import SyncCoordinator
from src.ui.tray import STATUS_TEXT_STATES, TrayIcon, TrayState


def _make_coordinator() -> SyncCoordinator:
    tray = MagicMock()
    tray.model = MagicMock()
    return SyncCoordinator(
        config=MagicMock(),
        aw=MagicMock(),
        bf=MagicMock(),
        queue=MagicMock(),
        sync_engine=MagicMock(),
        tray=tray,
        aw_manager=MagicMock(),
    )


def test_unreachable_shows_offline_not_error():
    coord = _make_coordinator()
    coord.bf.is_reachable.return_value = False
    coord._set_sync_failure_state("Sync failed")
    coord.tray.set_state.assert_called_once_with(TrayState.QUEUED)


def test_reachable_failure_shows_error():
    coord = _make_coordinator()
    coord.bf.is_reachable.return_value = True
    coord._set_sync_failure_state("Boom")
    coord.tray.set_state.assert_called_once_with(TrayState.ERROR, "Boom")


def test_reachability_probe_raising_is_treated_as_offline():
    coord = _make_coordinator()
    coord.bf.is_reachable.side_effect = RuntimeError("dns")
    coord._set_sync_failure_state("Sync error")
    coord.tray.set_state.assert_called_once_with(TrayState.QUEUED)


def test_the_queued_state_actually_reads_offline_on_the_surface():
    """The file's title, asserted where the user reads it.

    The three tests above drive a MagicMock tray and assert on call arguments,
    so they answer "which state was requested" and cannot answer "what does it
    say" — a mock renders nothing (Phantom 7). They used to pass the literal
    "Offline" as an argument, which made the claim look witnessed while still
    only checking what the producer said.

    The label now has one home (STATUS_TEXT_STATES), so the callers pass no text
    and this drives the REAL TrayIcon to check the row. Asserted against the
    hardcoded word rather than the dict entry: importing the constant for both
    sides would move them together and prove nothing (Rule 6a).
    """
    with patch("src.ui.tray.pystray"):
        tray = TrayIcon(
            on_login=lambda: None, on_logout=lambda: None, on_pause=lambda: None,
            on_resume=lambda: None, on_quit=lambda: None,
        )
    tray._icon = MagicMock()
    tray._update_icon = MagicMock()
    tray._update_menu = MagicMock()

    tray.set_state(TrayState.QUEUED)

    assert tray._get_status_text() == "Offline"
    assert STATUS_TEXT_STATES[TrayState.QUEUED] == "Offline"
