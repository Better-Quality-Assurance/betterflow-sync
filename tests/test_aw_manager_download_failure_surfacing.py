"""Surfacing layer for a fail-closed tracker download.

_download_aw_binaries failing closed means the agent runs while capturing
NOTHING, so it must reach the user (notification) and the ops ingest (error
report) — and it must be throttled, because start() is retried from the ~30s
health-check tick and would otherwise toast every 30 seconds for as long as the
failure lasts.
"""

import threading
from unittest.mock import Mock

from src.aw_manager import AWManager


def _join_report_threads():
    for thread in threading.enumerate():
        if thread.name == "aw-download-failure-report":
            thread.join(timeout=5)


def _failing_manager(monkeypatch, *, port_in_use=False):
    mgr = AWManager(aw_port=5600)
    mgr.error_reporter = Mock()
    monkeypatch.setattr(mgr, "_get_binaries_dir", lambda: None)
    monkeypatch.setattr(mgr, "_port_in_use", lambda: port_in_use)
    monkeypatch.setattr("src.aw_manager._download_aw_binaries", lambda _dir: False)
    notify = Mock()
    monkeypatch.setattr("src.notifications.send_notification", notify)
    return mgr, notify


def test_download_failure_notifies_reports_and_fails(monkeypatch):
    mgr, notify = _failing_manager(monkeypatch)

    assert mgr.start() is False
    _join_report_threads()

    assert mgr.tracker_download_failed is True
    assert notify.call_count == 1
    assert mgr.error_reporter.capture.call_count == 1
    kwargs = mgr.error_reporter.capture.call_args.kwargs
    assert kwargs["fingerprint"] == "aw_manager:tracker_download_failed"


def test_repeat_failure_is_throttled(monkeypatch):
    """The health-check tick retries start() every ~30s — the second attempt
    must stay silent on both channels."""
    mgr, notify = _failing_manager(monkeypatch)

    mgr.start()
    mgr.start()
    _join_report_threads()

    assert notify.call_count == 1
    assert mgr.error_reporter.capture.call_count == 1


def test_health_snapshot_carries_the_flag(monkeypatch):
    mgr, _notify = _failing_manager(monkeypatch)
    monkeypatch.setattr(mgr, "_get_latest_afk_event_age", lambda: None)
    monkeypatch.setattr(mgr, "_get_latest_window_event_age", lambda: None)

    mgr.start()
    _join_report_threads()

    assert mgr.health_snapshot()["tracker_download_failed"] is True


def test_external_server_is_not_a_download_failure(monkeypatch):
    """An external server on the port means capture is happening — only our
    managed watchers are missing. No toast, no latched flag, and start() must
    not claim failure."""
    mgr, notify = _failing_manager(monkeypatch, port_in_use=True)

    assert mgr.start() is True
    _join_report_threads()

    assert mgr.tracker_download_failed is False
    assert notify.call_count == 0
    assert mgr.error_reporter.capture.call_count == 0


def test_flag_clears_when_binaries_resolve_without_download(monkeypatch, tmp_path):
    """A device that recovers via an app update (frozen-bundle install path)
    never re-enters the download branch, so the latch must clear on any route
    that resolves a usable binaries dir."""
    mgr, _notify = _failing_manager(monkeypatch)
    mgr.start()
    _join_report_threads()
    assert mgr.tracker_download_failed is True

    monkeypatch.setattr(mgr, "_get_binaries_dir", lambda: str(tmp_path))
    monkeypatch.setattr(mgr, "_reap_orphan_processes", lambda *a, **k: None)
    monkeypatch.setattr(mgr, "_start_component", lambda *a, **k: True)
    monkeypatch.setattr(mgr, "_wait_for_server", lambda: True)

    assert mgr.start() is True
    assert mgr.tracker_download_failed is False
