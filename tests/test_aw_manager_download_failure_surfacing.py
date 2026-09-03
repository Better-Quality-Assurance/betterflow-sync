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


def _failing_manager(
    monkeypatch, *, port_in_use=False, download=None, server_responding=None
):
    mgr = AWManager(aw_port=5600)
    mgr.error_reporter = Mock()
    monkeypatch.setattr(mgr, "_get_binaries_dir", lambda: None)
    monkeypatch.setattr(mgr, "_port_in_use", lambda: port_in_use)
    # These tests predate the attach points asking /api/0/info (#246). They
    # stubbed the port alone because a held socket was all the code read, and
    # every one of them MEANT "an external server is running" -- so the default
    # answers that question the same way. Pass server_responding=False for the
    # held-but-dead case, which the fixture could not express before.
    if server_responding is None:
        server_responding = port_in_use
    monkeypatch.setattr(mgr, "_server_responding", lambda: server_responding)
    monkeypatch.setattr(
        "src.aw_manager._download_aw_binaries", download or (lambda _dir: False)
    )
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


def test_download_itself_is_backed_off_across_ticks(monkeypatch):
    """The ~60s capture-policy tick re-enters _start_locked; without a backoff on
    the DOWNLOAD (not just its notification) every tick re-pulls a 115-207 MB
    archive forever."""
    download = Mock(return_value=False)
    mgr, _notify = _failing_manager(monkeypatch, download=download)

    for _ in range(5):
        assert mgr.start() is False
    _join_report_threads()

    assert download.call_count == 1, "retries inside the backoff window must not refetch"

    # Backoff escalates, so honouring only the initial 5 min is not enough.
    assert mgr._download_retry_interval > 300


def test_backoff_expiry_allows_a_retry(monkeypatch):
    download = Mock(return_value=False)
    mgr, _notify = _failing_manager(monkeypatch, download=download)

    mgr.start()
    mgr._last_download_attempt -= mgr._download_retry_interval + 1
    mgr.start()
    _join_report_threads()

    assert download.call_count == 2


def test_external_attach_is_flagged_as_unmanaged(monkeypatch):
    """Attaching to an external server returns True, but nothing here can
    restart it — the backend needs to see that this device cannot self-heal."""
    mgr, _notify = _failing_manager(monkeypatch, port_in_use=True)
    monkeypatch.setattr(mgr, "_get_latest_afk_event_age", lambda: None)
    monkeypatch.setattr(mgr, "_get_latest_window_event_age", lambda: None)

    assert mgr.start() is True
    _join_report_threads()

    snapshot = mgr.health_snapshot()
    assert snapshot["managed_components_unavailable"] is True
    assert snapshot["tracker_download_failed"] is False
