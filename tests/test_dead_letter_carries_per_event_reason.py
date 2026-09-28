"""The server's PER-EVENT rejection reason must reach the dead-letter row and
the local warning log, even when the server gives no top-level `error` string.

Root cause (verified 2026-09-28): a batch rejected on per-event validation
(e.g. every event's timestamp is outside the server's 7-day/5-minute window)
comes back as a 200 with `failed > 0` and an empty `accepted_ids` list, but
with NO top-level error string — the server's explanation lives only in its
`errors` array (`internal-tool2`'s `AgentEventProcessor::processBatch`). At the
HTTP layer this is not an exception, so `bf_client.send_events()` never went
through the `except BetterFlowClientError` branch that captures `SyncResult.error`
— `SyncResult.error` stayed `None`.

`sync_engine.py`'s whole-batch definitive-rejection branch then called
``self.queue.increment_retry(event_ids, result.error)`` with `result.error is
None`. Per `increment_retry`'s own contract, `last_error=None` writes NOTHING
to the row — so a real server rejection dropped after max retries with an
empty `last_error` column and no warning ever logged, leaving nobody able to
tell why the event was lost.

Fix: `SyncResult` now carries the server's raw per-event `errors` (passive
plumbing from `send_events`'s response payload); `_process_queue`'s
whole-batch branch builds a real reason from them when `result.error` is
`None`, falls back to a fixed marker when the server gave neither, and always
logs a warning naming the reason.

Both tests below fail pre-fix: the dead-letter row's `last_error` is empty and
no warning is logged.
"""

import logging
import tempfile
from pathlib import Path
from unittest.mock import Mock

from src.config import Config
from src.sync.bf_client import SyncResult
from src.sync.queue import OfflineQueue
from src.sync.sync_engine import SyncEngine, SyncStats


def _engine(tmp: Path) -> SyncEngine:
    return SyncEngine(
        aw=Mock(),
        bf=Mock(),
        queue=OfflineQueue(db_path=tmp / "q.db", max_size=10000),
        config=Config(),
        time_tracker=Mock(),
    )


def _event(event_id: str = "e1") -> dict:
    from datetime import datetime, timezone

    return {
        "id": event_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration": 60.0,
        "bucket_id": "aw-watcher-window_host",
        "data": {"app": "Terminal"},
    }


def _run_one_drain_cycle(engine: SyncEngine) -> None:
    """Clear the backoff gate and run exactly one queue-processing cycle,
    mirroring test_queue_no_drop_on_outage.py's `_run_cycles` helper."""
    from datetime import datetime, timedelta, timezone

    engine._queue_backoff_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    engine._process_queue(SyncStats())


class TestDeadLetterCarriesPerEventReason:
    """The server names events individually in `errors`; that has to survive
    to the row `remove_failed` preserves, not just the generic
    "exceeded max retries" fallback `_process_queue` always passes."""

    def _whole_batch_rejection(self, errors: list) -> SyncResult:
        # Exactly what a 200-with-per-event-failures response produces once
        # bf_client.send_events reads the (currently ignored) `errors` field:
        # no top-level error, no accepted_ids, a definitive (non-transient)
        # rejection.
        return SyncResult(
            success=False,
            events_synced=0,
            events_queued=1,
            error=None,
            accepted_ids=[],
            transient=False,
            errors=errors,
        )

    def test_per_event_reason_reaches_the_dead_letter_row(self):
        tmp = Path(tempfile.mkdtemp())
        engine = _engine(tmp)
        engine.queue.enqueue([_event()])
        assert engine.queue.size() == 1

        engine.bf.send_events = Mock(
            return_value=self._whole_batch_rejection([
                {
                    "event": "e1",
                    "error": "Event timestamp out of acceptable range",
                    "reason": "too_old",
                }
            ])
        )

        # Push past the retry ceiling (5 definitive rejections).
        for _ in range(5):
            _run_one_drain_cycle(engine)
        assert engine.bf.send_events.called, "precondition: the batch was actually attempted"

        # One more cycle: failed_event_summary/remove_failed run at the TOP of
        # _process_queue and move the now-exhausted row to dead-letter.
        _run_one_drain_cycle(engine)

        rows = engine.queue.get_dead_letter_events()
        assert len(rows) == 1, "the event should be preserved in dead-letter"
        last_error = rows[0]["last_error"] or ""
        assert last_error, (
            "the server named a specific reason (too_old) but the dead-letter "
            "row's last_error is empty — result.error was None and nothing "
            "read the per-event errors instead"
        )
        assert "too_old" in last_error, (
            f"the server's specific per-event reason was dropped: {last_error!r}"
        )

    def test_falls_back_to_a_named_marker_when_server_gave_nothing_at_all(self):
        """No top-level error AND no per-event errors (a bare 200/failed body,
        or an older server) must still leave a diagnosable marker — never an
        empty column indistinguishable from 'never failed'."""
        tmp = Path(tempfile.mkdtemp())
        engine = _engine(tmp)
        engine.queue.enqueue([_event("e2")])

        engine.bf.send_events = Mock(
            return_value=self._whole_batch_rejection([])
        )

        for _ in range(5):
            _run_one_drain_cycle(engine)
        _run_one_drain_cycle(engine)

        rows = engine.queue.get_dead_letter_events()
        assert len(rows) == 1
        last_error = rows[0]["last_error"] or ""
        assert last_error, (
            "a reason-less server rejection must still leave a marker, not "
            "an empty last_error column"
        )
        assert "without a reason" in last_error, (
            f"expected the fixed fallback marker, got {last_error!r}"
        )


class TestPartialAcceptCarriesPerEventReason:
    """A mixed batch (server accepts e1, rejects e2 with too_old) takes the
    partial-accept branch. The rejected event's row must carry the server's
    own reason, not the generic no-reason-given marker — otherwise whether
    the cause survives depends on whether some other event in the same batch
    happened to be accepted."""

    def test_rejected_event_in_mixed_batch_carries_the_servers_reason(self):
        tmp = Path(tempfile.mkdtemp())
        engine = _engine(tmp)
        engine.queue.enqueue([_event("e1"), _event("e2")])

        engine.bf.send_events = Mock(
            return_value=SyncResult(
                success=False,
                events_synced=1,
                events_queued=1,
                error=None,
                accepted_ids=["e1"],
                transient=False,
                errors=[
                    {"event": "e2", "error": "out of range", "reason": "too_old"},
                ],
            )
        )

        for _ in range(5):
            _run_one_drain_cycle(engine)
        _run_one_drain_cycle(engine)

        rows = engine.queue.get_dead_letter_events()
        assert len(rows) == 1, "only the rejected event should be dead-lettered"
        last_error = rows[0]["last_error"] or ""
        assert "too_old" in last_error, (
            f"the server's per-event reason was lost on the partial-accept "
            f"branch: {last_error!r}"
        )


class TestPartialAcceptWarnsWithNoAttributableReason:
    """A partial-accept batch (non-empty `accepted_ids`) whose failed events
    have NO attributable reason — `errors` is empty, or every entry names an
    event that was actually accepted — must still warn on the rejecting
    cycle, and the dead-letter row must carry the SAME marker the warning
    named.

    Before the fix: `reason` was computed once, tested truthy only to decide
    whether to log, and a DIFFERENT literal string was passed to
    `increment_retry` regardless. So this exact case — a real per-event
    rejection the server gave no attributable cause for — logged nothing on
    the rejecting cycle while the dead-letter row still got a marker nobody
    had been warned about.
    """

    def _partial_accept_no_attributable_reason(self, errors: list) -> SyncResult:
        return SyncResult(
            success=False,
            events_synced=1,
            events_queued=1,
            error=None,
            accepted_ids=["e1"],
            transient=False,
            errors=errors,
        )

    def test_warns_on_the_rejecting_cycle_when_errors_is_empty(self, caplog):
        tmp = Path(tempfile.mkdtemp())
        engine = _engine(tmp)
        engine.queue.enqueue([_event("e1"), _event("e2")])

        engine.bf.send_events = Mock(
            return_value=self._partial_accept_no_attributable_reason([])
        )

        with caplog.at_level(logging.WARNING, logger="src.sync.sync_engine"):
            _run_one_drain_cycle(engine)

        assert any(
            "Server rejected" in r.message and "1 of 2" in r.message
            for r in caplog.records
        ), (
            "no warning was logged for the unattributable partial rejection "
            f"(errors=[]); records were: {[r.message for r in caplog.records]}"
        )

    def test_warns_on_the_rejecting_cycle_when_errors_name_only_accepted_events(
        self, caplog
    ):
        tmp = Path(tempfile.mkdtemp())
        engine = _engine(tmp)
        engine.queue.enqueue([_event("e1"), _event("e2")])

        # The only error entry names e1 — the event that WAS accepted — so it
        # cannot be attributed to e2, the one that failed.
        engine.bf.send_events = Mock(
            return_value=self._partial_accept_no_attributable_reason(
                [{"event": "e1", "error": "stale", "reason": "too_old"}]
            )
        )

        with caplog.at_level(logging.WARNING, logger="src.sync.sync_engine"):
            _run_one_drain_cycle(engine)

        assert any(
            "Server rejected" in r.message and "1 of 2" in r.message
            for r in caplog.records
        ), (
            "no warning was logged when the only error entry named an "
            f"accepted event, not the failed one; records were: "
            f"{[r.message for r in caplog.records]}"
        )

    def test_dead_letter_row_carries_the_same_marker_the_warning_named(self):
        tmp = Path(tempfile.mkdtemp())
        engine = _engine(tmp)
        engine.queue.enqueue([_event("e1"), _event("e2")])

        engine.bf.send_events = Mock(
            return_value=self._partial_accept_no_attributable_reason([])
        )

        for _ in range(5):
            _run_one_drain_cycle(engine)
        _run_one_drain_cycle(engine)

        rows = engine.queue.get_dead_letter_events()
        assert len(rows) == 1, "only the rejected event should be dead-lettered"
        last_error = rows[0]["last_error"] or ""
        assert last_error, (
            "an unattributable partial rejection left an empty last_error "
            "column"
        )
        assert "without a reason" in last_error, (
            f"expected the shared fixed no-reason-given marker (the same one "
            f"the whole-batch branch uses), got {last_error!r}"
        )


class TestDeadLetterReasonWarningIsLogged:
    """The reason must also reach the LOCAL log, per the task: an operator
    reading betterflow.log (or the error board, once the local reason passes
    through the existing sanitizer) needs to see it the cycle it happens, not
    only after the event eventually ages into dead-letter."""

    def test_warns_with_the_specific_reason_on_the_rejecting_cycle(self, caplog):
        tmp = Path(tempfile.mkdtemp())
        engine = _engine(tmp)
        engine.queue.enqueue([_event("e3")])

        engine.bf.send_events = Mock(
            return_value=SyncResult(
                success=False,
                error=None,
                accepted_ids=[],
                transient=False,
                errors=[
                    {"event": "e3", "error": "boom", "reason": "too_new"},
                ],
            )
        )

        with caplog.at_level(logging.WARNING, logger="src.sync.sync_engine"):
            _run_one_drain_cycle(engine)

        assert any("too_new" in r.message for r in caplog.records), (
            "no warning named the server's per-event reason; "
            f"records were: {[r.message for r in caplog.records]}"
        )
