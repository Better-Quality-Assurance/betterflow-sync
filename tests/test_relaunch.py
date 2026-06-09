"""Tests for BetterFlowApp._relaunch on macOS.

The permission gate's 'restart' outcome calls _relaunch. On a frozen .app it
must defer `open` until the current process has exited — otherwise `open`
reactivates the about-to-die instance and the app never reopens.
"""

from unittest.mock import MagicMock, patch

from src.main import BetterFlowApp


class TestRelaunch:
    def test_macos_frozen_defers_open_until_process_exits(self):
        fake_exe = "/Applications/BetterFlow.app/Contents/MacOS/BetterFlow"
        with patch("src.main.sys") as msys, \
             patch("src.main.subprocess.Popen") as popen, \
             patch("src.main.os._exit") as exit_mock:
            msys.frozen = True
            msys.executable = fake_exe

            BetterFlowApp._relaunch(MagicMock())

        popen.assert_called_once()
        cmd = popen.call_args.args[0]
        assert cmd[0] == "/bin/sh" and cmd[1] == "-c"
        script = cmd[2]
        # Waits for the current process to exit, THEN opens the bundle.
        assert "kill -0" in script
        assert "open " in script
        assert "BetterFlow.app" in script
        # Detached so the helper outlives our exit.
        assert popen.call_args.kwargs.get("start_new_session") is True
        exit_mock.assert_called_once_with(0)
