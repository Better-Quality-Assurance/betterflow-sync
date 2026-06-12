"""A failed sync while offline must read "Offline", not "Error".

When there's no internet (incl. Wi-Fi "connected" but no route, which the OS
network monitor doesn't always flag), a sync failure should put the tray into
the queued/Offline state — we're still tracking and queuing locally — instead
of a scary "App status: Error".
"""

from unittest.mock import MagicMock

from src.main import SyncCoordinator
from src.ui.tray import TrayState


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
    coord.tray.set_state.assert_called_once_with(TrayState.QUEUED, "Offline")


def test_reachable_failure_shows_error():
    coord = _make_coordinator()
    coord.bf.is_reachable.return_value = True
    coord._set_sync_failure_state("Boom")
    coord.tray.set_state.assert_called_once_with(TrayState.ERROR, "Boom")


def test_reachability_probe_raising_is_treated_as_offline():
    coord = _make_coordinator()
    coord.bf.is_reachable.side_effect = RuntimeError("dns")
    coord._set_sync_failure_state("Sync error")
    coord.tray.set_state.assert_called_once_with(TrayState.QUEUED, "Offline")
