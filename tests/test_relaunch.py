"""Tests for BetterFlowApp._relaunch and the shared _spawn_deferred_open helper.

The permission gate's 'restart' outcome calls _relaunch, which (on a frozen
.app) must defer `open` until the current process has exited — otherwise `open`
reactivates the about-to-die instance and the app never reopens.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from src.main import BetterFlowApp


class TestRelaunch:
    def test_macos_frozen_delegates_to_deferred_open(self):
        self_mock = MagicMock()
        with patch("src.main.sys") as msys, patch("src.main.os._exit") as exit_mock:
            msys.frozen = True
            msys.executable = "/Applications/BetterFlow.app/Contents/MacOS/BetterFlow"
            BetterFlowApp._relaunch(self_mock)

        self_mock._spawn_deferred_open.assert_called_once()
        target = self_mock._spawn_deferred_open.call_args.args[0]
        # Assert on the basename, not the full string: _relaunch passes the
        # .app bundle (parents[2]) and on Windows CI pathlib renders it with
        # backslashes, so an exact "/Applications/..." compare would falsely
        # fail. The bundle's name ending in ".app" is the real invariant.
        assert target.name == "BetterFlow.app"
        exit_mock.assert_called_once_with(0)


class TestSpawnDeferredOpen:
    def test_builds_deferred_open_command(self):
        with patch("src.main.subprocess.Popen") as popen:
            BetterFlowApp._spawn_deferred_open(MagicMock(), Path("/Applications/BetterFlow.app"))

        popen.assert_called_once()
        cmd = popen.call_args.args[0]
        assert cmd[0] == "/bin/sh" and cmd[1] == "-c"
        script = cmd[2]
        # Waits for the current process to exit, THEN opens the target.
        assert "kill -0" in script
        assert "open " in script
        assert "BetterFlow.app" in script
        # Detached so the helper outlives our exit.
        assert popen.call_args.kwargs.get("start_new_session") is True
