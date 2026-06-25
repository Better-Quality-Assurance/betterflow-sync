"""Tests for MacOSWindowWatcher no-emit instrumentation + emit wiring.

Origin: Cristian Dragota / sync:67a77a43-787, 2026-06-25 — window/app data went
stale on the server for ~15 min while AFK/input kept flowing, and the agent log
said NOTHING about the window watcher going quiet. These tests cover the new
instrumentation that makes such a stall leave a one-shot fingerprint, and verify
_poll_once records emit / no-emit on both paths.

The module imports PyObjC only inside functions, so it imports fine off-macOS and
the no-emit bookkeeping is unit-testable without a real window server.
"""

import logging
from unittest.mock import Mock, patch

from src.sync.macos_window_watcher import MacOSWindowWatcher


def _watcher():
    return MacOSWindowWatcher(Mock(), poll_interval=2.0)


def test_no_emit_warns_once_after_threshold_then_recovers(caplog):
    w = _watcher()
    with patch("src.sync.macos_window_watcher.time.monotonic") as mono:
        with caplog.at_level(logging.WARNING):
            mono.return_value = 100.0
            w._note_no_emit("no frontmost app")  # streak starts

            mono.return_value = 100.0 + 89  # under the 90s threshold
            w._note_no_emit("no frontmost app")
            assert not any("no window event" in r.getMessage() for r in caplog.records)

            mono.return_value = 100.0 + 95  # crosses the threshold
            w._note_no_emit("no frontmost app")
            warns = [r for r in caplog.records if "no window event" in r.getMessage()]
            assert len(warns) == 1
            assert "no frontmost app" in warns[0].getMessage()

            # Further no-emits must NOT re-warn (one-shot per streak).
            mono.return_value = 100.0 + 300
            w._note_no_emit("no frontmost app")
            warns = [r for r in caplog.records if "no window event" in r.getMessage()]
            assert len(warns) == 1

        with caplog.at_level(logging.INFO):
            caplog.clear()
            mono.return_value = 100.0 + 310
            w._note_emit()  # a real heartbeat lands
            assert any("resumed posting" in r.getMessage() for r in caplog.records)

            # Streak reset: a fresh gap can warn again.
            caplog.clear()
            mono.return_value = 1000.0
            w._note_no_emit("no frontmost app")
            mono.return_value = 1000.0 + 95
            w._note_no_emit("no frontmost app")
            assert any("no window event" in r.getMessage() for r in caplog.records)


def test_emit_without_prior_warning_is_silent(caplog):
    w = _watcher()
    with patch("src.sync.macos_window_watcher.time.monotonic", return_value=5.0), caplog.at_level(
        logging.INFO
    ):
        w._note_emit()  # healthy steady state — no recovery line
        assert not any("resumed posting" in r.getMessage() for r in caplog.records)


def test_poll_once_records_no_emit_when_no_frontmost_app():
    w = _watcher()
    w._get_active_window = Mock(return_value=None)
    w._note_no_emit = Mock()
    w._note_emit = Mock()

    w._poll_once()

    w._note_no_emit.assert_called_once()
    w._note_emit.assert_not_called()
    w._aw.post_heartbeat.assert_not_called()


def test_poll_once_records_emit_when_a_window_is_posted():
    w = _watcher()
    w._get_active_window = Mock(return_value={"app": "Google Chrome", "title": "x"})
    w._note_no_emit = Mock()
    w._note_emit = Mock()

    w._poll_once()

    w._aw.post_heartbeat.assert_called_once()
    w._note_emit.assert_called_once()
    w._note_no_emit.assert_not_called()
