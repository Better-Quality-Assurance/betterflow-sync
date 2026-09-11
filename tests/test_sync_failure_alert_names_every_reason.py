"""The ops alert must see EVERY failure reason, not only the first.

#254 made `server_status_summary` name local failure kinds instead of a bare
count, and every test of it passes a LIST. The production caller in
`SyncCoordinator._do_sync` passed `stats.errors[0]` — one string. So when the
first reason is the one producer that is deliberately never named
(sync_engine appends `result.error` verbatim, provenance unknown), a nameable
reason behind it was dropped before the summary ever saw it, and ops got the
pre-#254 bare count again.

Measured against the shipped function on origin/main (9227939):

    full list      -> '; 2 local reason(s) [bucket-sync-failed] recorded in local dead-letter'
    errors[0] only -> '; 1 local reason(s) recorded in local dead-letter'

This test drives the REAL caller (`_do_sync`), not the helper, because a helper
test hand-supplies exactly the input the caller got wrong.
"""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

from src.config import Config
from src.main import SyncCoordinator
from src.reminders import ReminderManager
from src.sync.sync_engine import SyncEngine

UNNAMED_FIRST = "Bank details 1234-5678 rejected by upstream"
NAMEABLE_SECOND = "Failed to sync bucket aw-watcher-window_SECRETHOST: boom"


def _failed_stats(errors):
    return SimpleNamespace(
        success=False,
        events_sent=0,
        events_queued=0,
        events_filtered=0,
        gaps_filled=0,
        errors=list(errors),
        aw_bucket_fetch_failed=False,
    )


def _coordinator(stats):
    queue = Mock()
    queue.get_checkpoint.return_value = None
    queue.size.return_value = 0
    queue.is_near_capacity.return_value = False
    sync_engine = Mock(spec=SyncEngine)
    sync_engine.is_paused = False
    sync_engine.is_private = False
    sync_engine.sync.return_value = stats
    tray = Mock()
    tray.model = Mock()
    tray.model.lock = threading.RLock()
    aw_manager = Mock()
    aw_manager.is_managing = False
    aw = Mock()
    aw.is_running.return_value = True
    coord = SyncCoordinator(
        config=Config(),
        aw=aw,
        bf=Mock(),
        queue=queue,
        sync_engine=sync_engine,
        tray=tray,
        aw_manager=aw_manager,
        reminder_manager=Mock(spec=ReminderManager),
    )
    coord.scheduler = Mock()
    coord.scheduler.running = True
    coord.error_reporter = Mock()
    coord._fetch_hours_today = Mock(return_value="1:00")
    coord._monitor_capture_health = Mock(return_value=True)
    # Next failure crosses the alert threshold, so exactly one capture fires.
    coord._consecutive_sync_failures = coord._SYNC_FAILURE_ALERT_THRESHOLD - 1
    return coord


def _repeated_failure_messages(coord):
    return [
        c.args[0]
        for c in coord.error_reporter.capture.call_args_list
        if c.kwargs.get("fingerprint") == "sync-repeated-failure"
    ]


def test_a_nameable_reason_behind_an_unnamed_one_reaches_the_ops_alert():
    coord = _coordinator(_failed_stats([UNNAMED_FIRST, NAMEABLE_SECOND]))

    coord._do_sync()

    msgs = _repeated_failure_messages(coord)
    # Precondition: the caller path actually reached the ops alert. Without it
    # a negative assertion below would pass on silence (Phantom 4).
    assert len(msgs) == 1, coord.error_reporter.capture.call_args_list
    msg = msgs[0]
    assert "bucket-sync-failed" in msg, f"the nameable reason was dropped: {msg}"
    assert "2 local reason(s)" in msg, msg


def test_the_caller_path_still_ships_no_interpolated_text():
    # Passing the whole list must not widen what leaves the device: every token
    # still comes from _LOCAL_REASON_KINDS or is a count.
    coord = _coordinator(_failed_stats([UNNAMED_FIRST, NAMEABLE_SECOND]))

    coord._do_sync()

    (msg,) = _repeated_failure_messages(coord)
    for leaked in ("1234-5678", "Bank details", "SECRETHOST", "aw-watcher-window", "boom"):
        assert leaked not in msg, f"{leaked!r} reached the cross-tenant ingest: {msg}"


def test_NEGATIVE_CONTROL_no_errors_still_reports_the_fallback():
    # Empty `stats.errors` keeps the old "Sync failed" fallback: counted, unnamed.
    coord = _coordinator(_failed_stats([]))

    coord._do_sync()

    (msg,) = _repeated_failure_messages(coord)
    assert "1 local reason(s)" in msg, msg
    assert "[" not in msg, msg
