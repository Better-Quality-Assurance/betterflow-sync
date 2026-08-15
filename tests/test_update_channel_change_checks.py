"""Changing the update channel must actually check for updates (#199).

`BetterFlowApp._on_preferences` passed `callback=self._on_update_available`, but
that method lives on `UpdateHandler`, not on the app. Evaluating the argument
raised `AttributeError` before `check_for_update` was ever called, and the
surrounding `except Exception` logged "Failed to check for updates after channel
change" — a message that points at GitHub or the network, which is where anyone
investigating would have looked.

The saved setting was always correct, so the next scheduled check 30 minutes
later did use the new channel. That is what kept this invisible: the feature
appears to work, just late, and the log blames something else.
"""

import logging
import threading
from unittest.mock import MagicMock, patch

import pytest

import src.main as main_mod
from src.main import BetterFlowApp
from src.update_handler import UpdateHandler


def _make_app() -> BetterFlowApp:
    """Only the attributes `_on_preferences` touches on the update_channel path."""
    app = BetterFlowApp.__new__(BetterFlowApp)
    app.config = MagicMock()
    app.config.update_channel = "stable"
    # A REAL UpdateHandler, not a MagicMock: a mock answers to any attribute
    # name, so it would happily supply `_on_update_available` even if the method
    # were renamed or moved away again. The bug being pinned here is precisely
    # "this attribute does not exist on the object we asked".
    handler = UpdateHandler.__new__(UpdateHandler)
    handler.tray = MagicMock()
    handler.config = app.config
    handler.coordinator = MagicMock()
    handler._version = "1.5.123"
    handler._notified_version = None
    handler._managed_warned_version = None
    handler._arch_undetermined_version = None
    handler._staged_version = None
    handler._staged_lock = threading.Lock()
    app.update_handler = handler
    return app


def test_changing_the_update_channel_actually_checks_for_updates():
    app = _make_app()

    with patch.object(main_mod, "check_for_update") as check:
        app._on_preferences("update_channel", "beta")

    check.assert_called_once()
    assert check.call_args.kwargs["channel"] == "beta"
    assert app.config.update_channel == "beta"
    app.config.save.assert_called_once()


def test_the_callback_is_the_one_that_can_receive_the_result():
    """Not just "a callback" — the bound method that handles an available update.

    A fix that passed any callable at all would satisfy the test above. This
    pins WHICH object answers, because the defect was an attribute lookup on the
    wrong one.
    """
    app = _make_app()

    with patch.object(main_mod, "check_for_update") as check:
        app._on_preferences("update_channel", "beta")

    cb = check.call_args.kwargs["callback"]
    assert cb.__self__ is app.update_handler, "callback must be bound to the UpdateHandler"
    assert cb.__func__ is UpdateHandler._on_update_available

    # And it must accept what check_for_update actually passes it:
    # callback(latest_tag.lstrip("v"), html_url, asset_url) — three positionals.
    with patch.object(app.update_handler, "_stage_and_maybe_apply"), \
         patch.object(main_mod, "send_notification", create=True), \
         patch("src.update_handler.send_notification"):
        cb("1.5.124", "http://rel", "http://x/a.dmg")


def test_a_channel_change_stages_rather_than_restarting_under_the_user():
    """apply_now defaults False, unlike the launch check.

    `ensure_update_checks_started` wraps its callback to force `apply_now=True`
    (catch-on-launch). A preference toggle is not a launch: the user is sitting
    in the menu, and relaunching the app out from under them because they
    switched channel would be its own bug report. Staging is the periodic-check
    behaviour and the right one here.
    """
    app = _make_app()

    with patch.object(main_mod, "check_for_update") as check:
        app._on_preferences("update_channel", "beta")
    cb = check.call_args.kwargs["callback"]

    with patch.object(app.update_handler, "_stage_and_maybe_apply") as stage, \
         patch("src.update_handler.send_notification"):
        cb("1.5.124", "http://rel", "http://x/a.dmg")

    stage.assert_called_once()
    assert stage.call_args[0][2] is False, "apply_now must be False for a channel change"


def test_no_failure_is_logged_when_the_check_is_wired_correctly(caplog):
    """The swallow is what made this survive: the warning read as a network fault.

    Asserted as an ABSENCE, so it needs the positive control above it — without
    `check.assert_called_once()` in the sibling test, this would also pass
    against code that never reached the call at all.
    """
    app = _make_app()

    with caplog.at_level(logging.WARNING), patch.object(main_mod, "check_for_update"):
        app._on_preferences("update_channel", "beta")

    assert "Failed to check for updates" not in caplog.text


@pytest.mark.parametrize("channel", ["stable", "beta", "canary"])
def test_every_channel_is_forwarded_verbatim(channel):
    app = _make_app()

    with patch.object(main_mod, "check_for_update") as check:
        app._on_preferences("update_channel", channel)

    assert check.call_args.kwargs["channel"] == channel
