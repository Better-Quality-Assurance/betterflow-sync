"""A managed (root-owned) install must be told ONCE that it can't self-update,
and must NOT loop on a doomed download+apply. See UpdateHandler._on_update_available.
"""

import threading
from unittest.mock import MagicMock, patch

import src.update_handler as uh
from src.update_handler import UpdateHandler


def _handler() -> UpdateHandler:
    h = UpdateHandler.__new__(UpdateHandler)
    h.tray = MagicMock()
    h.config = MagicMock()
    h.config.auto_install_updates = True
    h.coordinator = MagicMock()
    h._version = "1.5.106"
    h._notified_version = None
    h._managed_warned_version = None
    h._staged_version = None
    h._staged_lock = threading.Lock()
    return h


def test_managed_install_warns_once_and_never_stages():
    h = _handler()
    with patch.object(h, "_managed_install_ok", return_value=False), \
         patch.object(uh, "send_notification") as notify, \
         patch.object(h, "_stage_and_maybe_apply") as stage, \
         patch.object(h, "_report_managed_blocked") as report:
        # Two checks of the same version (the 30-min re-check).
        h._on_update_available("1.5.118", "http://x", asset_url="http://x/a.dmg", apply_now=True)
        h._on_update_available("1.5.118", "http://x", asset_url="http://x/a.dmg", apply_now=True)

    stage.assert_not_called()          # never attempt the doomed apply
    assert notify.call_count == 1      # told once, not every check
    report.assert_called_once()        # ops told once
    # The toast is the manual-step one, not the normal "available" one.
    title, body = notify.call_args[0][0], notify.call_args[0][1]
    assert "manual step" in title.lower()
    assert "reinstall" in body.lower()


def test_replaceable_install_proceeds_to_stage():
    h = _handler()
    with patch.object(h, "_managed_install_ok", return_value=True), \
         patch.object(uh, "send_notification"), \
         patch.object(h, "_stage_and_maybe_apply") as stage:
        h._on_update_available("1.5.118", "http://x", asset_url="http://x/a.dmg", apply_now=True)

    stage.assert_called_once()          # normal path unaffected
