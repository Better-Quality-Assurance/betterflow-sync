"""Tests for backfilling the queued activity-event tail on a logs_requested pull.

When the server requests this device's logs, the agent now also uploads a
bounded, read-only tail of recently-queued activity events (window/AFK/input)
so a QUIET device's real activity can be recovered — not just diagnostics. The
export must never mutate the queue (the events still sync normally).

Uses a real OfflineQueue (so we can assert the queue is left intact) with a
mocked BetterFlowClient.
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock

from src.config import Config
from src.sync.http_client import BetterFlowAuthError, BetterFlowClientError
from src.sync.queue import OfflineQueue
from src.sync.sync_engine import SyncEngine


def _make_engine(queue: OfflineQueue) -> SyncEngine:
    return SyncEngine(
        aw=Mock(),
        bf=Mock(),
        queue=queue,
        config=Config(),
        activity_analyzer=Mock(),
        time_tracker=Mock(),
    )


class TestEventTailUpload:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_queue.db"
        self.queue = OfflineQueue(db_path=self.db_path, max_size=1000)
        self.engine = _make_engine(self.queue)

    def teardown_method(self):
        self.queue.close()

    def test_uploads_event_tail_and_leaves_queue_intact(self):
        events = [
            {"id": f"e{i}", "bucket_id": "aw-watcher-window_host", "duration": 5}
            for i in range(5)
        ]
        self.queue.enqueue(events)

        self.engine._upload_event_tail()

        self.engine.bf.upload_events_tail.assert_called_once()
        sent = self.engine.bf.upload_events_tail.call_args[0][0]
        # The queued events were read and forwarded...
        assert {e["id"] for e in sent} == {f"e{i}" for i in range(5)}
        # ...and the queue was left fully intact for the normal sync path.
        assert self.queue.size() == 5
        assert len(self.queue.dequeue(batch_size=10)) == 5

    def test_no_upload_when_queue_empty(self):
        self.engine._upload_event_tail()
        self.engine.bf.upload_events_tail.assert_not_called()

    def test_upload_tail_is_bounded(self):
        # Even a large queue exports only a bounded tail (the default row cap is
        # 500) — never the whole DB.
        events = [{"id": f"e{i}", "bucket_id": "b", "duration": 1} for i in range(600)]
        self.queue.enqueue(events)

        self.engine._upload_event_tail()

        sent = self.engine.bf.upload_events_tail.call_args[0][0]
        assert len(sent) <= 500
        assert self.queue.size() == 600  # untouched

    def test_client_error_is_reported_not_raised(self):
        self.queue.enqueue([{"id": "e1", "bucket_id": "b", "duration": 1}])
        self.engine.bf.upload_events_tail.side_effect = BetterFlowClientError("boom")
        self.engine._report_upload_failure = Mock()

        # A transient upload failure must not escape (heartbeat continues); the
        # queue stays intact so the events retry via the normal route.
        self.engine._upload_event_tail()

        self.engine._report_upload_failure.assert_called_once()
        assert self.queue.size() == 1

    def test_auth_error_is_reraised_for_relogin(self):
        self.queue.enqueue([{"id": "e1", "bucket_id": "b", "duration": 1}])
        self.engine.bf.upload_events_tail.side_effect = BetterFlowAuthError("401")

        raised = False
        try:
            self.engine._upload_event_tail()
        except BetterFlowAuthError:
            raised = True
        assert raised  # surfaced so _send_heartbeat can trigger re-login
        assert self.queue.size() == 1

    def test_requested_logs_handler_also_uploads_events(self, monkeypatch):
        # The full logs_requested handler uploads text logs AND the event tail.
        self.queue.enqueue([{"id": "e1", "bucket_id": "b", "duration": 1}])
        monkeypatch.setattr(
            SyncEngine, "_read_log_tail", staticmethod(lambda path, **kw: b"log-bytes")
        )

        self.engine._upload_requested_logs()

        self.engine.bf.upload_logs.assert_called_once()
        self.engine.bf.upload_events_tail.assert_called_once()
        assert self.queue.size() == 1  # events still queued for normal sync
