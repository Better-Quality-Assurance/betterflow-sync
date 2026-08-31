"""The server's rejection reason must survive to the dead-letter row.

Before this fix the agent recorded its OWN generic string
("exceeded max retries (5); definitive rejection") on every dead-lettered
event, and discarded the server's actual 4xx body at
``bf_client.send_events``'s ``except BetterFlowClientError`` handler. So when
the fleet reported "Dropped N queued event(s) after max retries — the server
rejected them", nobody could tell a malformed event from a receiving-side
change, which are the two causes with opposite fixes.

The reason has to be recorded when the batch FAILS and read when the event is
DROPPED, because those happen on different sync cycles: ``remove_failed`` runs
at the top of ``_process_queue``, decoupled from the batch that failed.
"""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.sync.queue import OfflineQueue


class TestDeadLetterCarriesServerReason:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "q.db"
        self.queue = OfflineQueue(db_path=self.db_path, max_size=100)

    def teardown_method(self):
        self.queue.close()

    def _enqueue_one(self):
        self.queue.enqueue([{
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration": 60,
            "bucket_id": "bf-status_host",
            "bucket_type": "idle_time",
            "data": {},
        }])
        return [e.id for e in self.queue.dequeue(batch_size=10)]

    def test_server_reason_recorded_at_retry_reaches_dead_letter(self):
        """The reason the SERVER gave, not the agent's generic string."""
        ids = self._enqueue_one()
        server_said = "422 Unprocessable: duration 90000 exceeds maximum 86400"

        # Five definitive rejections, each carrying the server's reason.
        for _ in range(5):
            self.queue.increment_retry(ids, last_error=server_said)

        self.queue.remove_failed(
            max_retries=5,
            last_error="exceeded max retries (5); definitive rejection",
        )

        rows = self.queue.get_dead_letter_events()
        assert len(rows) == 1, "event should be preserved in dead-letter"
        assert server_said in (rows[0]["last_error"] or ""), (
            "dead-letter row lost the server's rejection reason; got "
            f"{rows[0]['last_error']!r}"
        )

    def test_falls_back_to_generic_when_server_gave_no_reason(self):
        """A transient-only history leaves no reason; the generic must remain
        so the row is never left with an empty explanation."""
        ids = self._enqueue_one()
        for _ in range(5):
            self.queue.increment_retry(ids)  # no reason available

        self.queue.remove_failed(max_retries=5, last_error="generic fallback")

        rows = self.queue.get_dead_letter_events()
        assert len(rows) == 1
        assert rows[0]["last_error"] == "generic fallback"

    def test_summary_surfaces_reason_for_the_ops_report(self):
        """_report_dropped_events builds its message from this summary, so the
        reason has to be reachable there or the warning stays uninformative."""
        ids = self._enqueue_one()
        server_said = "400 Bad Request: unknown bucket_type 'idle_time'"
        for _ in range(5):
            self.queue.increment_retry(ids, last_error=server_said)

        summary = self.queue.failed_event_summary(max_retries=5)
        assert summary["count"] == 1
        assert summary["last_errors"] == [server_said], (
            f"summary carries no server reason: {summary!r}"
        )


class TestOpsReportNeverEchoesPayload:
    """The drop warning goes to the CROSS-TENANT ops ingest. A server validation
    error routinely quotes the value it rejected, and our event payloads carry
    window titles — so the reason must be reduced to a status code before it
    leaves the device, never merely truncated."""

    def test_status_code_extracted_without_the_surrounding_text(self):
        from src.sync.sync_engine import SyncEngine
        out = SyncEngine._dropped_reason_code([
            'API error (422): duration invalid for event '
            '{"title": "Re: Q3 layoffs - CONFIDENTIAL.xlsx"}'
        ])
        assert "422" in out
        assert "CONFIDENTIAL" not in out, f"payload echoed into ops message: {out}"
        assert "layoffs" not in out
        assert "title" not in out

    def test_unrecognisable_reason_yields_pointer_not_the_text(self):
        from src.sync.sync_engine import SyncEngine
        out = SyncEngine._dropped_reason_code(["weird: opened Passwords.kdbx"])
        assert "Passwords" not in out, f"payload echoed: {out}"
        assert "local dead-letter" in out

    def test_absent_reason_adds_nothing(self):
        from src.sync.sync_engine import SyncEngine
        assert SyncEngine._dropped_reason_code(None) == ""
        assert SyncEngine._dropped_reason_code([]) == ""
        assert SyncEngine._dropped_reason_code([""]) == ""


class TestReasonProvenanceNotShape:
    """A 3-digit run is not a status code. Both were live before the anchor."""

    def test_our_own_shed_message_is_not_reported_as_a_server_status(self):
        from src.sync.sync_engine import SyncEngine
        # _queue_consecutive_failures is uncapped; ~17h of outage reaches 3 digits.
        msg = ("shed as unstorable after 137 transient failures "
               "— not a server rejection")
        out = SyncEngine._dropped_reason_code([msg])
        assert "server status" not in out, (
            f"a message saying it is NOT a rejection rendered as one: {out}"
        )

    def test_a_number_inside_a_rejected_title_is_not_a_status(self):
        from src.sync.sync_engine import SyncEngine
        out = SyncEngine._dropped_reason_code(["unknown title 'Bug 404 fix.txt'"])
        assert "server status" not in out, f"payload digits read as a status: {out}"
        assert "404" not in out

    def test_real_server_rejection_still_reports_its_status(self):
        from src.sync.sync_engine import SyncEngine
        out = SyncEngine._dropped_reason_code(["API error (422): duration"])
        assert "server status 422" in out

    def test_several_causes_are_all_named(self):
        """One drop cycle spans batches; naming one cause is confidently wrong."""
        from src.sync.sync_engine import SyncEngine
        out = SyncEngine._dropped_reason_code([
            "API error (400): unknown project",
            "API error (422): duration out of range",
        ])
        assert "400" in out and "422" in out, out


class TestSanitizerIsActuallyCalled:
    """Witness the CONSUMER. Testing _dropped_reason_code directly leaves the
    call site unguarded: deleting it, or replacing it with the raw string,
    kept the whole suite green."""

    def test_report_message_carries_status_not_server_text(self):
        from src.sync.sync_engine import SyncEngine

        captured = {}

        class FakeReporter:
            def capture(self, message, **kw):
                captured["msg"] = message

        engine = SyncEngine.__new__(SyncEngine)
        engine.error_reporter = FakeReporter()
        engine._hostname = "host"
        SyncEngine._report_dropped_events(engine, {
            "count": 1, "real_loss_count": 1, "unstorable_count": 0,
            "bucket_ids": ["bf-status_host"],
            "oldest": "2026-08-29T10:00:00+00:00",
            "newest": "2026-08-29T10:00:00+00:00",
            "last_errors": [
                'API error (422): rejected {"title": "Q3 layoffs CONFIDENTIAL.xlsx"}'
            ],
        })
        msg = captured.get("msg", "")
        assert "server status 422" in msg, msg
        assert "CONFIDENTIAL" not in msg, f"payload reached the ops ingest: {msg}"
        assert "layoffs" not in msg
        assert "title" not in msg


class TestLegacyQueueMigration:
    """The ALTER runs on ~57 live laptops and had no coverage: deleting both
    migration lines left every other test green, because a fresh OfflineQueue
    already CREATEs the column."""

    def test_pre_existing_four_column_queue_gains_the_column(self, tmp_path):
        import sqlite3
        from src.sync.queue import OfflineQueue

        db = tmp_path / "legacy.db"
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE queued_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "event_data TEXT NOT NULL, created_at TEXT NOT NULL, "
            "retry_count INTEGER DEFAULT 0)"
        )
        con.execute(
            "INSERT INTO queued_events (event_data, created_at, retry_count) "
            "VALUES (?, ?, ?)",
            (json.dumps({"bucket_id": "bf-status_host", "duration": 60,
                         "timestamp": datetime.now(timezone.utc).isoformat()}),
             datetime.now(timezone.utc).isoformat(), 5),
        )
        con.commit(); con.close()

        q = OfflineQueue(db_path=db, max_size=100)
        try:
            with q._cursor() as cur:
                cols = [r[1] for r in cur.execute(
                    "PRAGMA table_info(queued_events)")]
            assert "last_error" in cols, f"migration did not run: {cols}"
            assert q.failed_event_summary(max_retries=5)["count"] == 1, (
                "the pre-existing row did not survive the migration"
            )
        finally:
            q.close()


class TestReasonSurvivesTheOtherTwoDropPaths:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "q2.db"
        self.queue = OfflineQueue(db_path=self.db_path, max_size=100)

    def teardown_method(self):
        self.queue.close()

    def test_eviction_does_not_overwrite_a_real_server_rejection(self):
        """An event that took a definitive 4xx and LATER aged past retention is
        dead-lettered by evict_unstorable, whose generic string would otherwise
        erase the only evidence of why it failed."""
        stale = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        self.queue.enqueue([{
            "timestamp": stale, "duration": 60,
            "bucket_id": "bf-status_host", "bucket_type": "idle_time", "data": {},
        }])
        ids = [e.id for e in self.queue.dequeue(batch_size=10)]
        self.queue.increment_retry(ids, "API error (422): duration out of range")

        self.queue.evict_unstorable(last_error="unstorable — evicted before batching")

        rows = self.queue.get_dead_letter_events()
        assert len(rows) == 1
        assert "422" in (rows[0]["last_error"] or ""), (
            f"eviction erased the server's reason: {rows[0]['last_error']!r}"
        )


class TestPartialAcceptRecordsItsOwnReason:
    """The per-event rejection path is the ONLY one where the server names the
    specific event, and its SyncResult carries no error string — so passing
    result.error there records nothing and the event keeps a stale reason from
    an unrelated earlier whole-batch failure."""

    def test_partial_accept_branch_passes_a_real_reason(self):
        import inspect
        from src.sync import sync_engine

        src = inspect.getsource(sync_engine.SyncEngine._process_queue)
        assert "increment_retry(failed_ids, result.error)" not in src, (
            "the partial-accept branch passes result.error, which is None on "
            "that branch — the event keeps a stale reason from another cycle"
        )
        assert "per-event rejection" in src, (
            "the partial-accept branch records no reason of its own"
        )


class TestSummaryCollectsEveryDistinctCause:
    """Witnesses the COLLECTOR, not the renderer. Reverting
    failed_event_summary to keep one arbitrary row's reason reddened nothing
    while the multi-cause test above still passed, because that test hands
    _dropped_reason_code a list directly."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "q3.db"
        self.queue = OfflineQueue(db_path=self.db_path, max_size=100)

    def teardown_method(self):
        self.queue.close()

    def test_two_batches_two_causes_both_reported(self):
        now = datetime.now(timezone.utc).isoformat()
        base = {"duration": 60, "bucket_id": "bf-status_host",
                "bucket_type": "idle_time", "data": {}}
        self.queue.enqueue([{**base, "timestamp": now, "id": "a"}])
        self.queue.enqueue([{**base, "timestamp": now, "id": "b"}])
        queued = self.queue.dequeue(batch_size=10)
        id_a, id_b = queued[0].id, queued[1].id

        for _ in range(5):
            self.queue.increment_retry([id_a], "API error (422): duration out of range")
            self.queue.increment_retry([id_b], "API error (400): unknown project")

        summary = self.queue.failed_event_summary(max_retries=5)
        assert summary["count"] == 2
        assert summary["last_errors"] == [
            "API error (400): unknown project",
            "API error (422): duration out of range",
        ], f"summary collapsed several causes into one: {summary['last_errors']!r}"


class TestSyncFailureAlertDoesNotEchoServerText:
    """The OTHER path to the same cross-tenant ingest. `result.error` reaches
    _note_sync_failure verbatim via stats.errors, and on the 3rd consecutive
    failure it was captured as-is — so the drop report could be perfectly
    sanitized while the identical text left by a different door."""

    def test_only_the_status_reaches_the_ops_ingest(self):
        from src.main import SyncCoordinator

        captured = {}

        class FakeReporter:
            def capture(self, message, **kw):
                captured["msg"] = message

        app = SyncCoordinator.__new__(SyncCoordinator)
        app._consecutive_sync_failures = 2  # next one crosses the threshold
        app._SYNC_FAILURE_ALERT_THRESHOLD = 3
        app.error_reporter = FakeReporter()

        SyncCoordinator._note_sync_failure(
            app,
            'API error (422): rejected {"title": "Q3 layoffs CONFIDENTIAL.xlsx"}',
        )

        msg = captured.get("msg", "")
        assert msg, "no alert captured — threshold logic changed?"
        assert "422" in msg, msg
        assert "CONFIDENTIAL" not in msg, f"payload reached the ops ingest: {msg}"
        assert "layoffs" not in msg
        assert "title" not in msg
