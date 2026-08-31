"""Tests for macOS in-process window watcher."""

import platform
import threading
from unittest.mock import MagicMock, patch

import pytest

# Guard: skip collection entirely if PyObjC is absent (Windows, Linux, Docker)
pytest.importorskip("AppKit", reason="PyObjC not installed - macOS-only")

from src.sync.macos_window_watcher import MacOSWindowWatcher

# Generous by design. Every wait below is on a CONDITION the watcher thread
# signals, so this bound is only ever reached when the thread genuinely never
# did the work — i.e. a real failure. A slow machine waits longer; it does not
# go red. See _PollSignal.
_WAIT_SECONDS = 10.0


class _PollSignal:
    """Wait for the watcher thread to have DONE the work, not for the clock.

    These tests used to start the poll thread and ``time.sleep(0.2)`` with a
    0.05s poll interval, betting that at least one iteration lands inside the
    window. That bet loses on a loaded runner: ``test_browser_url_included``
    failed the v1.5.129 release build on macos-latest/arm64 and passed on
    re-run with byte-identical code, having passed on macos-14-large/x86_64 in
    the same run. There is no sleep duration that fixes it — any wall-clock
    margin is a bet on the slowest machine that will ever run the suite
    (tests/test_watchdog_overrun_outcome.py makes the same argument).

    So the thread signals each poll and the test blocks until the count it
    needs has arrived. ``target=2`` is used by the negative tests, where
    "nothing was posted" is only evidence if the loop demonstrably ran.
    """

    def __init__(self, target: int = 1):
        self._target = target
        self._reached = threading.Event()
        self._lock = threading.Lock()
        self.calls = 0

    def note(self) -> None:
        with self._lock:
            self.calls += 1
            if self.calls >= self._target:
                self._reached.set()

    def wait(self) -> bool:
        return self._reached.wait(_WAIT_SECONDS)


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS-only watcher")
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
        posted = _PollSignal()
        aw.post_heartbeat.side_effect = lambda *a, **k: posted.note()

        watcher._thread = threading.Thread(target=watcher._run, daemon=True)
        watcher._thread.start()
        assert posted.wait(), "the watcher thread never posted a heartbeat"
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
        posted = _PollSignal()
        aw.post_heartbeat.side_effect = lambda *a, **k: posted.note()

        watcher._thread = threading.Thread(target=watcher._run, daemon=True)
        watcher._thread.start()
        assert posted.wait(), "the watcher thread never posted a heartbeat"
        watcher.stop()

        assert aw.post_heartbeat.called
        data = aw.post_heartbeat.call_args[0][2]
        assert data["url"] == "https://github.com"
        assert data["incognito"] is False

    @patch("src.sync.macos_window_watcher.MacOSWindowWatcher._get_active_window")
    def test_none_result_skips_heartbeat(self, mock_get_window):
        """Test that None from _get_active_window doesn't post heartbeat."""
        # Two polls, so "nothing was posted" is evidence about the loop having
        # RUN rather than about it never having started (the vacuous pass a
        # too-short sleep produces here).
        polled = _PollSignal(target=2)

        def _none(*_a, **_k):
            polled.note()
            return None

        mock_get_window.side_effect = _none

        watcher, aw = self._make_watcher(poll_interval=0.05)
        watcher._stop_event = threading.Event()

        watcher._thread = threading.Thread(target=watcher._run, daemon=True)
        watcher._thread.start()
        assert polled.wait(), "the watcher thread never polled twice"
        watcher.stop()

        aw.post_heartbeat.assert_not_called()

    @patch("src.sync.macos_window_watcher.MacOSWindowWatcher._get_active_window")
    def test_exception_handled_gracefully(self, mock_get_window):
        """Test that exceptions in polling don't crash the watcher thread."""
        # Two raising polls: surviving ONE proves nothing about a loop that
        # re-enters. Waiting on the count rather than on 0.2s of clock is what
        # makes "repeated" true on a loaded machine as well as an idle one.
        raised = _PollSignal(target=2)

        def _boom(*_a, **_k):
            raised.note()
            raise Exception("some error")

        mock_get_window.side_effect = _boom

        watcher, aw = self._make_watcher(poll_interval=0.05)
        watcher._stop_event = threading.Event()

        watcher._thread = threading.Thread(target=watcher._run, daemon=True)
        watcher._thread.start()
        assert raised.wait(), "the watcher thread never raised twice"

        # Thread should still be alive despite repeated exceptions
        assert watcher._thread.is_alive()

        watcher.stop()
        aw.post_heartbeat.assert_not_called()
        assert not watcher._thread.is_alive()

    @patch("src.sync.macos_window_watcher.MacOSWindowWatcher._get_active_window")
    def test_stop_event_exits_thread(self, mock_get_window):
        """Test that setting stop event cleanly exits the thread."""
        mock_get_window.return_value = None

        watcher, aw = self._make_watcher(poll_interval=0.5)

        watcher._thread = threading.Thread(target=watcher._run, daemon=True)
        watcher._thread.start()

        # Stop immediately
        watcher.stop()
        watcher._thread.join(timeout=2.0)

        assert not watcher._thread.is_alive()

    def test_bucket_creation_failure_doesnt_prevent_start(self):
        """Test that bucket creation failure doesn't prevent watcher from starting."""
        watcher, aw = self._make_watcher()
        aw.create_bucket.side_effect = Exception("connection refused")

        with patch.object(watcher, "_run"):
            result = watcher.start()

        assert result is True
        watcher.stop()
