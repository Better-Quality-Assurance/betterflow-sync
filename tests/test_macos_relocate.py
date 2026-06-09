"""Tests for BetterFlowApp._maybe_relocate_to_applications.

On macOS a frozen .app launched from outside /Applications (DMG, Downloads, or
a Gatekeeper translocation path) should offer to move into /Applications and
relaunch from there; everywhere else it's a no-op.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from src.main import BetterFlowApp

# These tests force sys.platform="darwin" to exercise the macOS-only relocate
# path. On Windows that path falls through to a real tkinter modal (pathlib
# uses backslashes, so the "/Applications/" guard never matches), which blocks
# CI forever. Production gates this on `sys.platform != "darwin"`, so the
# logic never runs off macOS anyway — skip the tests there.
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="exercises macOS-only relocate logic"
)


class TestRelocate:
    def test_noop_on_non_macos(self):
        self_mock = MagicMock()
        with patch("src.main.sys") as msys, \
             patch("src.main.subprocess.run") as run, \
             patch("src.main.os._exit") as exit_mock:
            msys.frozen = True
            msys.platform = "win32"
            BetterFlowApp._maybe_relocate_to_applications(self_mock)
        run.assert_not_called()
        exit_mock.assert_not_called()
        self_mock._spawn_deferred_open.assert_not_called()

    def test_noop_when_already_in_applications(self):
        self_mock = MagicMock()
        with patch("src.main.sys") as msys, \
             patch("src.main.subprocess.run") as run, \
             patch("src.main.os._exit") as exit_mock:
            msys.frozen = True
            msys.platform = "darwin"
            msys.executable = "/Applications/BetterFlow.app/Contents/MacOS/BetterFlow"
            BetterFlowApp._maybe_relocate_to_applications(self_mock)
        run.assert_not_called()
        exit_mock.assert_not_called()
        self_mock._spawn_deferred_open.assert_not_called()

    def test_declining_keeps_running_in_place(self):
        self_mock = MagicMock()
        with patch("src.main.sys") as msys, \
             patch("src.main.subprocess.run") as run, \
             patch("src.main.os._exit") as exit_mock, \
             patch("tkinter.Tk"), \
             patch("tkinter.messagebox.askyesno", return_value=False):
            msys.frozen = True
            msys.platform = "darwin"
            msys.executable = "/Volumes/BetterFlow/BetterFlow.app/Contents/MacOS/BetterFlow"
            BetterFlowApp._maybe_relocate_to_applications(self_mock)
        run.assert_not_called()
        exit_mock.assert_not_called()
        self_mock._spawn_deferred_open.assert_not_called()

    def test_accepting_copies_to_applications_and_relaunches(self):
        self_mock = MagicMock()
        with patch("src.main.sys") as msys, \
             patch("src.main.subprocess.run") as run, \
             patch("src.main.os._exit") as exit_mock, \
             patch("tkinter.Tk"), \
             patch("tkinter.messagebox.askyesno", return_value=True):
            msys.frozen = True
            msys.platform = "darwin"
            msys.executable = "/Volumes/BetterFlow/BetterFlow.app/Contents/MacOS/BetterFlow"
            BetterFlowApp._maybe_relocate_to_applications(self_mock)

        ditto = [c for c in run.call_args_list if c.args and "ditto" in c.args[0]]
        assert ditto, "expected a ditto copy into /Applications"
        # ditto src is the running bundle, dest is /Applications/BetterFlow.app
        assert ditto[0].args[0][1] == "/Volumes/BetterFlow/BetterFlow.app"
        assert ditto[0].args[0][2] == "/Applications/BetterFlow.app"
        self_mock._spawn_deferred_open.assert_called_once()
        assert str(self_mock._spawn_deferred_open.call_args.args[0]) == "/Applications/BetterFlow.app"
        exit_mock.assert_called_once_with(0)
