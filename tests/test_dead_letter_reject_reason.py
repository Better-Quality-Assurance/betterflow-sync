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
from datetime import datetime, timezone
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
        assert server_said in (summary.get("last_error") or ""), (
            f"summary carries no server reason: {summary!r}"
        )


class TestOpsReportNeverEchoesPayload:
    """The drop warning goes to the CROSS-TENANT ops ingest. A server validation
    error routinely quotes the value it rejected, and our event payloads carry
    window titles — so the reason must be reduced to a status code before it
    leaves the device, never merely truncated."""

    def test_status_code_extracted_without_the_surrounding_text(self):
        from src.sync.sync_engine import SyncEngine
        out = SyncEngine._dropped_reason_code(
            '422 Unprocessable: duration invalid for event '
            '{"title": "Re: Q3 layoffs - CONFIDENTIAL.xlsx"}'
        )
        assert "422" in out
        assert "CONFIDENTIAL" not in out, f"payload echoed into ops message: {out}"
        assert "layoffs" not in out
        assert "title" not in out

    def test_unrecognisable_reason_yields_pointer_not_the_text(self):
        from src.sync.sync_engine import SyncEngine
        out = SyncEngine._dropped_reason_code("weird: opened Passwords.kdbx")
        assert "Passwords" not in out, f"payload echoed: {out}"
        assert "local dead-letter" in out

    def test_absent_reason_adds_nothing(self):
        from src.sync.sync_engine import SyncEngine
        assert SyncEngine._dropped_reason_code(None) == ""
        assert SyncEngine._dropped_reason_code("") == ""
