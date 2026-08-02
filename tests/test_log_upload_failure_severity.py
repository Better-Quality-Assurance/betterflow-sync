"""Severity guard for the logs_requested upload failure report.

Origin: 2026-07-27, BUG-003541 — "Agent log upload failed: upload POST failed:
Cannot connect to BetterFlow API", one occurrence, auto-filed from live runtime
telemetry. The condition is a device that could not reach the BetterFlow API
while an admin had asked for its log tail.

Nothing is lost when this happens: ``_upload_requested_logs`` clears nothing
client-side, and the server clears its ``logs_requested`` flag only on a
successful upload, so the next heartbeat retries. A single handled, self-healing
transient must therefore report as ``warning`` — reporting it as ``error`` pages
humans and draws the autofix drafter onto a non-problem, which is the same noise
failure ``_do_sync``'s watchdog classification fixed for the sibling report
(#151, tests/test_sync_watchdog_outcome_classification.py).

The level was already correct when this guard was written; what was missing was
anything WATCHING it. ``test_failed_upload_post_reports_to_ops`` in
tests/test_sync_engine.py pins the message text and the call count but never the
severity, so a change from warning to error passed the suite. Per
``rules/test-fixture-discipline.md`` Phantom 5, a guard is only real once it has
been watched failing — this one was, by flipping the production level to "error"
and confirming these tests go red.

Drives the REAL ``_send_heartbeat`` -> ``_upload_requested_logs`` path with a
stubbed transport and asserts on what the error reporter actually captured,
never on arguments forwarded between functions.
"""

from unittest.mock import Mock, patch

from src.config import Config
from src.sync.http_client import BetterFlowAuthError, BetterFlowClientError
from src.sync.sync_engine import SyncEngine

# The exact transport failure BUG-003541 was filed from.
_OFFLINE = "Cannot connect to BetterFlow API"

# Dedup fingerprint the report is grouped under. Pinned so a rename has to be
# deliberate: the reporter's dedup window is what stops a persistent outage
# flooding ops on every heartbeat.
_FINGERPRINT = "log-upload-failed"


class _Recorder:
    """Records what was actually captured, instead of asserting on a Mock's args."""

    def __init__(self):
        self.captures = []

    def capture(self, message, **kwargs):
        self.captures.append({"message": message, **kwargs})

    def by_level(self, level):
        return [c for c in self.captures if c.get("level") == level]

    def by_fingerprint(self, fingerprint):
        return [c for c in self.captures if c.get("fingerprint") == fingerprint]


class TestLogUploadFailureSeverity:
    def setup_method(self):
        self.aw = Mock()
        self.bf = Mock()
        self.queue = Mock()
        self.queue.get_checkpoint.return_value = None
        self.queue.evict_unstorable.return_value = {
            "count": 0, "bucket_ids": [], "oldest": None, "newest": None,
            "real_loss_count": 0, "unstorable_count": 0,
        }
        self.config = Config()
        self.config.working_hours.known = True

        self.engine = SyncEngine(
            aw=self.aw,
            bf=self.bf,
            queue=self.queue,
            config=self.config,
            activity_analyzer=Mock(),
            time_tracker=Mock(),
        )
        self.recorder = _Recorder()
        self.engine.error_reporter = self.recorder

        # An admin has requested this device's logs.
        self.bf.heartbeat.return_value = {
            "success": True, "data": {"logs_requested": True},
        }

    def _run_heartbeat(self):
        with patch.object(SyncEngine, "_read_log_tail", return_value=b"log-bytes"):
            return self.engine._send_heartbeat()

    def test_unreachable_api_reports_the_upload_failure_as_warning(self):
        """The BUG-003541 condition itself: an offline device reports at
        warning, never error."""
        self.bf.upload_logs.side_effect = BetterFlowClientError(_OFFLINE)

        self._run_heartbeat()

        reports = self.recorder.by_fingerprint(_FINGERPRINT)
        assert len(reports) == 1, self.recorder.captures
        assert reports[0]["level"] == "warning"
        assert _OFFLINE in reports[0]["message"]

    def test_unreachable_api_emits_no_error_level_report_at_all(self):
        """The noise property stated end-to-end: a handled transient must not
        put ANY error-level event on the ops ingest, whatever its fingerprint."""
        self.bf.upload_logs.side_effect = BetterFlowClientError(_OFFLINE)

        self._run_heartbeat()

        assert self.recorder.by_level("error") == [], self.recorder.captures

    def test_a_5xx_upload_failure_is_also_warning(self):
        """Not special-cased to connection errors — any retryable POST failure
        is the same self-healing condition."""
        self.bf.upload_logs.side_effect = BetterFlowClientError("500")

        self._run_heartbeat()

        reports = self.recorder.by_fingerprint(_FINGERPRINT)
        assert len(reports) == 1, self.recorder.captures
        assert reports[0]["level"] == "warning"

    def test_upload_failure_does_not_break_the_heartbeat_cycle(self):
        """No data loss / no wedge: the failure is swallowed at the heartbeat
        boundary, so the cycle completes and the next one retries. A returned
        exception here means the caller would fire a re-login instead."""
        self.bf.upload_logs.side_effect = BetterFlowClientError(_OFFLINE)

        assert self._run_heartbeat() is None
        # Nothing was consumed or cleared client-side by the failed attempt.
        self.queue.clear.assert_not_called()

    def test_auth_failure_still_surfaces_for_relogin(self):
        """The downgrade must not swallow the one upload failure that is NOT
        self-healing. An expired session needs re-login, so _send_heartbeat
        returns the auth error to its caller rather than reporting and moving
        on."""
        self.bf.upload_logs.side_effect = BetterFlowAuthError("expired")

        result = self._run_heartbeat()

        assert isinstance(result, BetterFlowAuthError)
        assert self.recorder.by_fingerprint(_FINGERPRINT) == []

    def test_successful_upload_reports_nothing(self):
        """Negative control: the happy path must produce no capture at all, so
        the assertions above are responding to the failure and not to the
        harness always capturing something."""
        self.bf.upload_logs.side_effect = None

        self._run_heartbeat()

        assert self.recorder.captures == []
