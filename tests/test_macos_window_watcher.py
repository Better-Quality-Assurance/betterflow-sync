"""Tests for macOS in-process window watcher."""

import threading
import time
from unittest.mock import MagicMock, patch

from src.sync.macos_window_watcher import MacOSWindowWatcher


class TestMacOSWindowWatcher:
    """Tests for MacOSWindowWatcher."""

    def _make_watcher(self, aw_client=None, poll_interval=0.1):
        aw = aw_client or MagicMock()
        watcher = MacOSWindowWatcher(aw, poll_interval=poll_interval)
        return watcher, aw

    @patch("src.sync.macos_window_watcher.MacOSWindowWatcher._get_active_window")
    def test_start_creates_bucket(self, mock_get_window):
        """Test start creates AW bucket."""
        mock_get_window.return_value = None
        watcher, aw = self._make_watcher()

        with patch.object(watcher, "_run"):
            # Mock the PyObjC imports in start()
            with patch("src.sync.macos_window_watcher.MacOSWindowWatcher.start", wraps=watcher.start):
                watcher.start()

        aw.create_bucket.assert_called_once()
        args = aw.create_bucket.call_args
        assert args[0][0].startswith("aw-watcher-window_")
        assert args[0][1] == "currentwindow"

        watcher.stop()

    @patch("src.sync.macos_window_watcher.MacOSWindowWatcher._get_active_window")
    def test_heartbeat_posted_with_correct_data(self, mock_get_window):
        """Test that heartbeat is posted with correct app/title data."""
        mock_get_window.return_value = {
            "app": "Terminal",
            "title": "zsh — 80×24",
        }

        watcher, aw = self._make_watcher(poll_interval=0.05)
        watcher._stop_event = threading.Event()

        watcher._thread = threading.Thread(target=watcher._run, daemon=True)
        watcher._thread.start()
        time.sleep(0.2)
        watcher.stop()

        assert aw.post_heartbeat.called
        call_args = aw.post_heartbeat.call_args
        assert call_args[0][0].startswith("aw-watcher-window_")
        data = call_args[0][2]
        assert data["app"] == "Terminal"
        assert data["title"] == "zsh — 80×24"
        assert "url" not in data

    @patch("src.sync.macos_window_watcher.MacOSWindowWatcher._get_active_window")
    def test_browser_url_included(self, mock_get_window):
        """Test that browser URL is included in heartbeat data."""
        mock_get_window.return_value = {
            "app": "Google Chrome",
            "title": "GitHub - Google Chrome",
            "url": "https://github.com",
            "incognito": False,
        }

        watcher, aw = self._make_watcher(poll_interval=0.05)
        watcher._stop_event = threading.Event()

        watcher._thread = threading.Thread(target=watcher._run, daemon=True)
        watcher._thread.start()
        time.sleep(0.2)
        watcher.stop()

        assert aw.post_heartbeat.called
        data = aw.post_heartbeat.call_args[0][2]
        assert data["url"] == "https://github.com"
        assert data["incognito"] is False

    @patch("src.sync.macos_window_watcher.MacOSWindowWatcher._get_active_window")
    def test_none_result_skips_heartbeat(self, mock_get_window):
        """Test that None from _get_active_window doesn't post heartbeat."""
        mock_get_window.return_value = None

        watcher, aw = self._make_watcher(poll_interval=0.05)
        watcher._stop_event = threading.Event()

        watcher._thread = threading.Thread(target=watcher._run, daemon=True)
        watcher._thread.start()
        time.sleep(0.2)
        watcher.stop()

        aw.post_heartbeat.assert_not_called()

    @patch("src.sync.macos_window_watcher.MacOSWindowWatcher._get_active_window")
    def test_exception_handled_gracefully(self, mock_get_window):
        """Test that exceptions in polling don't crash the watcher."""
        mock_get_window.side_effect = Exception("some error")

        watcher, aw = self._make_watcher(poll_interval=0.05)
        watcher._stop_event = threading.Event()

        watcher._thread = threading.Thread(target=watcher._run, daemon=True)
        watcher._thread.start()
        time.sleep(0.2)
        watcher.stop()

        aw.post_heartbeat.assert_not_called()
        assert not watcher._thread.is_alive()  # stopped cleanly

    @patch("src.sync.macos_window_watcher.MacOSWindowWatcher._get_active_window")
    def test_stop_event_exits_thread(self, mock_get_window):
        """Test that setting stop event cleanly exits the thread."""
        mock_get_window.return_value = None

        watcher, aw = self._make_watcher(poll_interval=0.5)

        watcher._thread = threading.Thread(target=watcher._run, daemon=True)
        watcher._thread.start()

        # Stop immediately
        watcher.stop()

        assert not watcher._thread.is_alive()

    def test_bucket_creation_failure_doesnt_prevent_start(self):
        """Test that bucket creation failure doesn't prevent watcher from starting."""
        watcher, aw = self._make_watcher()
        aw.create_bucket.side_effect = Exception("connection refused")

        with patch.object(watcher, "_run"):
            result = watcher.start()

        assert result is True
        watcher.stop()
