"""The drop alert calls preserved events "lost activity".

remove_failed() MOVES exhausted events to the dead-letter table rather than
deleting them, and requeue_storable_dead_letter() replays the ones that become
storable again. The events are undelivered and preserved. Telling ops they are
lost sends someone hunting for data that is in a table, and it inflates a
warning that is otherwise correctly scoped to genuine rejections.
"""

from unittest.mock import MagicMock

from src.sync.sync_engine import SyncEngine


def _engine_with_reporter():
    engine = SyncEngine.__new__(SyncEngine)
    engine.error_reporter = MagicMock()
    engine._dropped_window_age = lambda oldest, newest: "spanning 2h"
    return engine


def test_the_drop_warning_does_not_claim_the_events_are_lost():
    engine = _engine_with_reporter()

    engine._report_dropped_events(
        {"count": 3, "real_loss_count": 3, "bucket_ids": ["aw-watcher-window_host"], "oldest": 1, "newest": 2}
    )

    message = engine.error_reporter.capture.call_args[0][0]
    assert "lost activity" not in message
    assert "dead-letter" in message, "say where they are, or the reader cannot go look"


def test_it_still_warns_and_still_names_the_count():
    """The severity is right and must not be softened along with the wording:
    these are events the server rejected, and somebody should look."""
    engine = _engine_with_reporter()

    engine._report_dropped_events(
        {"count": 3, "real_loss_count": 3, "bucket_ids": ["aw-watcher-window_host"], "oldest": 1, "newest": 2}
    )

    kwargs = engine.error_reporter.capture.call_args.kwargs
    assert kwargs["level"] == "warning"
    assert kwargs["fingerprint"] == "offline-queue-events-dropped"
    assert "3" in engine.error_reporter.capture.call_args[0][0]


def test_the_benign_flush_path_is_untouched():
    engine = _engine_with_reporter()

    engine._report_dropped_events(
        {"count": 2, "real_loss_count": 0, "bucket_ids": [], "oldest": 1, "newest": 2}
    )

    kwargs = engine.error_reporter.capture.call_args.kwargs
    assert kwargs["level"] == "info"
