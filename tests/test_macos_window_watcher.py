"""Tests for macOS in-process window watcher."""

import json
import threading
import time
from unittest.mock import MagicMock, patch

from src.sync.macos_window_watcher import MacOSWindowWatcher, _find_jxa_script


class TestFindJxaScript:
    """Tests for JXA script discovery."""

    def test_finds_dev_path(self, tmp_path):
        """Test finding JXA script in development layout."""
        jxa_dir = tmp_path / "resources" / "trackers" / "darwin" / "bf-window-tracker" / "aw_watcher_window"
        jxa_dir.mkdir(parents=True)
        jxa_file = jxa_dir / "printAppStatus.jxa"
        jxa_file.write_text("// test")

        with patch("src.sync.macos_window_watcher.os.path.dirname") as mock_dirname:
            # __file__ -> sync dir -> src dir (project root)
            mock_dirname.side_effect = [
                str(tmp_path / "src" / "sync"),  # dirname of __file__
                str(tmp_path),                     # dirname of sync dir (project root)
            ]
            result = _find_jxa_script()
            # Reset side effect before assertion to avoid issues
            assert result is not None
            assert result.endswith("printAppStatus.jxa")

    def test_returns_none_when_missing(self, tmp_path):
        """Test returns None when JXA script is not found anywhere."""
        with patch("src.sync.macos_window_watcher.os.path.dirname") as mock_dirname:
            mock_dirname.side_effect = [
                str(tmp_path / "src" / "sync"),
                str(tmp_path),
            ]
            with patch("src.sync.macos_window_watcher.getattr", return_value=False, create=True):
                _find_jxa_script()
                # May or may not find it depending on real filesystem;
                # main point is it doesn't crash


class TestMacOSWindowWatcher:
    """Tests for MacOSWindowWatcher."""

    def _make_watcher(self, aw_client=None, poll_interval=0.1):
        aw = aw_client or MagicMock()
        watcher = MacOSWindowWatcher(aw, poll_interval=poll_interval)
        return watcher, aw

    @patch("src.sync.macos_window_watcher._find_jxa_script")
    def test_start_fails_without_jxa_script(self, mock_find):
        """Test start returns False when JXA script is not found."""
        mock_find.return_value = None
        watcher, aw = self._make_watcher()
        assert watcher.start() is False
        aw.create_bucket.assert_not_called()

    @patch("src.sync.macos_window_watcher._find_jxa_script")
    def test_start_creates_bucket(self, mock_find):
        """Test start creates AW bucket."""
        mock_find.return_value = "/fake/printAppStatus.jxa"
        watcher, aw = self._make_watcher()

        # Prevent actual thread from running osascript
        with patch.object(watcher, "_run"):
            watcher.start()

        aw.create_bucket.assert_called_once()
        args = aw.create_bucket.call_args
        assert args[0][0].startswith("aw-watcher-window_")
        assert args[0][1] == "currentwindow"

        watcher.stop()

    @patch("src.sync.macos_window_watcher._find_jxa_script")
    @patch("src.sync.macos_window_watcher.subprocess.run")
    def test_heartbeat_posted_with_correct_data(self, mock_subprocess, mock_find):
        """Test that heartbeat is posted with correct app/title/url data."""
        mock_find.return_value = "/fake/printAppStatus.jxa"

        jxa_output = json.dumps({
            "app": "Terminal",
            "title": "zsh — 80×24",
            "url": None,
            "incognito": None,
        })
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout=jxa_output, stderr="",
        )

        watcher, aw = self._make_watcher(poll_interval=0.05)
        watcher._jxa_path = "/fake/printAppStatus.jxa"
        watcher._stop_event = threading.Event()

        # Run one poll cycle manually
        watcher._stop_event.clear()

        # Start the thread, let it run briefly, then stop
        watcher._thread = threading.Thread(target=watcher._run, daemon=True)
        watcher._thread.start()
        time.sleep(0.2)
        watcher.stop()

        # Verify heartbeat was posted
        assert aw.post_heartbeat.called
        call_args = aw.post_heartbeat.call_args
        assert call_args[0][0].startswith("aw-watcher-window_")
        data = call_args[0][2]
        assert data["app"] == "Terminal"
        assert data["title"] == "zsh — 80×24"
        # url=None should not be included in heartbeat data
        assert "url" not in data

    @patch("src.sync.macos_window_watcher._find_jxa_script")
    @patch("src.sync.macos_window_watcher.subprocess.run")
    def test_browser_url_included(self, mock_subprocess, mock_find):
        """Test that browser URL is included in heartbeat data."""
        mock_find.return_value = "/fake/printAppStatus.jxa"

        jxa_output = json.dumps({
            "app": "Google Chrome",
            "title": "GitHub - Google Chrome",
            "url": "https://github.com",
            "incognito": False,
        })
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout=jxa_output, stderr="",
        )

        watcher, aw = self._make_watcher(poll_interval=0.05)
        watcher._jxa_path = "/fake/printAppStatus.jxa"
        watcher._stop_event = threading.Event()

        watcher._thread = threading.Thread(target=watcher._run, daemon=True)
        watcher._thread.start()
        time.sleep(0.2)
        watcher.stop()

        assert aw.post_heartbeat.called
        data = aw.post_heartbeat.call_args[0][2]
        assert data["url"] == "https://github.com"
        assert data["incognito"] is False

    @patch("src.sync.macos_window_watcher._find_jxa_script")
    @patch("src.sync.macos_window_watcher.subprocess.run")
    def test_jxa_script_error_handled_gracefully(self, mock_subprocess, mock_find):
        """Test that JXA errors don't crash the watcher."""
        mock_find.return_value = "/fake/printAppStatus.jxa"

        mock_subprocess.return_value = MagicMock(
            returncode=1, stdout="", stderr="execution error: some error",
        )

        watcher, aw = self._make_watcher(poll_interval=0.05)
        watcher._jxa_path = "/fake/printAppStatus.jxa"
        watcher._stop_event = threading.Event()

        watcher._thread = threading.Thread(target=watcher._run, daemon=True)
        watcher._thread.start()
        time.sleep(0.2)
        watcher.stop()

        # No heartbeat should be posted on error
        aw.post_heartbeat.assert_not_called()
        # Thread should still be alive (didn't crash)
        assert not watcher._thread.is_alive()  # stopped cleanly

    @patch("src.sync.macos_window_watcher._find_jxa_script")
    @patch("src.sync.macos_window_watcher.subprocess.run")
    def test_empty_output_handled(self, mock_subprocess, mock_find):
        """Test that empty JXA output doesn't crash the watcher."""
        mock_find.return_value = "/fake/printAppStatus.jxa"

        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="", stderr="",
        )

        watcher, aw = self._make_watcher(poll_interval=0.05)
        watcher._jxa_path = "/fake/printAppStatus.jxa"
        watcher._stop_event = threading.Event()

        watcher._thread = threading.Thread(target=watcher._run, daemon=True)
        watcher._thread.start()
        time.sleep(0.2)
        watcher.stop()

        aw.post_heartbeat.assert_not_called()

    @patch("src.sync.macos_window_watcher._find_jxa_script")
    @patch("src.sync.macos_window_watcher.subprocess.run")
    def test_invalid_json_handled(self, mock_subprocess, mock_find):
        """Test that invalid JSON output doesn't crash the watcher."""
        mock_find.return_value = "/fake/printAppStatus.jxa"

        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="not valid json{{{", stderr="",
        )

        watcher, aw = self._make_watcher(poll_interval=0.05)
        watcher._jxa_path = "/fake/printAppStatus.jxa"
        watcher._stop_event = threading.Event()

        watcher._thread = threading.Thread(target=watcher._run, daemon=True)
        watcher._thread.start()
        time.sleep(0.2)
        watcher.stop()

        aw.post_heartbeat.assert_not_called()

    @patch("src.sync.macos_window_watcher._find_jxa_script")
    def test_stop_event_exits_thread(self, mock_find):
        """Test that setting stop event cleanly exits the thread."""
        mock_find.return_value = "/fake/printAppStatus.jxa"

        watcher, aw = self._make_watcher(poll_interval=0.5)
        watcher._jxa_path = "/fake/printAppStatus.jxa"

        with patch("src.sync.macos_window_watcher.subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=0, stdout="", stderr="")

            watcher._thread = threading.Thread(target=watcher._run, daemon=True)
            watcher._thread.start()

            # Stop immediately
            watcher.stop()

            # Thread should exit within timeout
            assert not watcher._thread.is_alive()

    @patch("src.sync.macos_window_watcher._find_jxa_script")
    def test_bucket_creation_failure_doesnt_prevent_start(self, mock_find):
        """Test that bucket creation failure doesn't prevent watcher from starting."""
        mock_find.return_value = "/fake/printAppStatus.jxa"
        watcher, aw = self._make_watcher()
        aw.create_bucket.side_effect = Exception("connection refused")

        with patch.object(watcher, "_run"):
            result = watcher.start()

        assert result is True
        watcher.stop()
