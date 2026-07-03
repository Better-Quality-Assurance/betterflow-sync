"""Tests for sync engine."""

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.config import Config
from src.main import SyncCoordinator
from src.reminders import ReminderManager
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.aw_client import BUCKET_TYPE_AFK, BUCKET_TYPE_INPUT, BUCKET_TYPE_WINDOW, AWEvent
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.sync_engine import SyncEngine, SyncStats, _SyncCycleContext
from src.ui.tray import TrayState


class TestSyncEngine:
    """Tests for SyncEngine."""

    def setup_method(self):
        """Set up test fixtures."""
        self.aw = Mock()
        self.bf = Mock()
        self.queue = Mock()
        # Mock the exclude_apps as a list so "in" checks work
        self.queue.get_checkpoint.return_value = None
        self.config = Config()

        # Create mock activity analyzer and time tracker
        self.activity_analyzer = Mock(spec=ActivityAnalyzer)
        self.activity_analyzer.get_activity_state.return_value = "active"
        self.activity_analyzer.get_raw_metrics.return_value = Mock(
            to_dict=lambda: {"presses": 0, "clicks": 0, "scrolls": 0, "window_changes": 0}
        )
        self.activity_analyzer.get_fraud_assessment.return_value = Mock(
            score=0,
            signals=[],
            extra_metrics={"unique_apps": 0, "keystroke_variance": None},
        )

        self.time_tracker = Mock(spec=DailyTimeTracker)
        self.time_tracker.get_today_active_time.return_value = timedelta(hours=1)

        self.engine = SyncEngine(
            aw=self.aw,
            bf=self.bf,
            queue=self.queue,
            config=self.config,
            activity_analyzer=self.activity_analyzer,
            time_tracker=self.time_tracker,
        )

    def test_pause_resume(self):
        """Test pause and resume functionality."""
        assert self.engine.is_paused is False

        self.engine.pause()
        assert self.engine.is_paused is True

        self.engine.resume()
        assert self.engine.is_paused is False

    def test_sync_when_paused(self):
        """Test that sync does nothing when paused."""
        self.engine.pause()
        stats = self.engine.sync()

        assert stats.events_fetched == 0
        self.aw.is_running.assert_not_called()

    def test_sync_when_aw_not_running(self):
        """Test sync fails gracefully when AW is down."""
        self.aw.is_running.return_value = False
        self.bf.is_reachable.return_value = False

        stats = self.engine.sync()

        assert "ActivityWatch is not running" in stats.errors

    def test_transform_event_filters_short_window_events(self):
        """Test that window events below min_window_event_seconds (5s) are filtered."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=4.0,  # Below default 5s threshold
            data={"app": "Test"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)
        assert result is None

    def test_transform_event_short_input_not_filtered(self):
        """Test that short input events (< 5s) are NOT filtered (only window events are)."""
        event = AWEvent(
            id=2,
            timestamp=datetime.now(timezone.utc),
            duration=1.0,  # Below 5s but above 0.5s
            data={"presses": 10, "clicks": 5, "scrolls": 2},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_INPUT)
        assert result is not None
        assert result["data"]["presses"] == 10

    def test_transform_event_short_afk_not_filtered(self):
        """Test that short AFK events (< 5s) are NOT filtered."""
        event = AWEvent(
            id=3,
            timestamp=datetime.now(timezone.utc),
            duration=2.0,  # Below 5s but above 0.5s
            data={"status": "not-afk"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_AFK)
        assert result is not None
        assert result["data"]["status"] == "not-afk"

    def test_transform_event_window_at_threshold_passes(self):
        """Test that a window event exactly at the 5s threshold passes."""
        event = AWEvent(
            id=4,
            timestamp=datetime.now(timezone.utc),
            duration=5.0,
            data={"app": "Firefox", "title": "Test"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)
        assert result is not None

    def test_status_at_returns_covering_afk_status(self):
        """_status_at should return the status covering a timestamp."""
        now = datetime.now(timezone.utc)
        afk_events = [
            AWEvent(
                id=10,
                timestamp=now - timedelta(minutes=5),
                duration=600,
                data={"status": "not-afk"},
            ),
        ]

        status = self.engine._status_at(now, afk_events)

        assert status == "not-afk"

    def test_transform_event_uses_not_afk_fallback_without_input(self):
        """Window events should stay active when AFK data says not-afk at event end."""
        now = datetime.now(timezone.utc)
        cycle = _SyncCycleContext(has_input_data=False, afk_events=[
            AWEvent(
                id=11,
                timestamp=now - timedelta(minutes=1),
                duration=120,
                data={"status": "not-afk"},
            ),
        ])
        event = AWEvent(
            id=12,
            timestamp=now,
            duration=30.0,
            data={"app": "Terminal", "title": "Work"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW, cycle=cycle)

        assert result is not None
        assert result["activity_state"] == "active"

    def test_transform_and_checkpoint_uploads_full_window_no_client_afk_split(self):
        """1.5.43: the client no longer splits/drops window events around AFK.
        The full event is uploaded every cycle; active-vs-idle (AFK overlap) is
        decided server-side. This prevents the client from stranding real work
        when input detection lags or events arrive zero-duration."""
        self.engine._afk_watcher_available = True
        now = datetime.now(timezone.utc)
        cycle = _SyncCycleContext(has_input_data=False, afk_events=[
            AWEvent(
                id=20,
                timestamp=now + timedelta(seconds=30),
                duration=30,
                data={"status": "afk"},
            ),
        ])
        event = AWEvent(
            id=21,
            timestamp=now,
            duration=90.0,
            data={"app": "Terminal", "title": "Work"},
        )
        stats = SyncStats()

        transformed, checkpoint = self.engine._transform_and_checkpoint(
            [event],
            "bucket-123",
            BUCKET_TYPE_WINDOW,
            stats,
            cycle,
        )

        assert checkpoint == ("bucket-123", now, 21)
        # Full event uploaded as a single slice — no client-side AFK split.
        assert len(transformed) == 1
        assert transformed[0]["id"] == "21:0"
        assert transformed[0]["duration"] == 90.0
        assert transformed[0]["activity_state"] == "active"
        self.time_tracker.add_active_time.assert_called_once()
        assert self.time_tracker.add_active_time.call_args[0][0] == 90.0

    def test_transform_and_checkpoint_counts_full_window_without_afk(self):
        """Without AFK overlap, no-input windows should still count fully."""
        self.engine._afk_watcher_available = True
        cycle = _SyncCycleContext(has_input_data=False, afk_events=[])
        now = datetime.now(timezone.utc)
        event = AWEvent(
            id=22,
            timestamp=now,
            duration=60.0,
            data={"app": "Terminal", "title": "Work"},
        )
        stats = SyncStats()

        transformed, _ = self.engine._transform_and_checkpoint(
            [event],
            "bucket-123",
            BUCKET_TYPE_WINDOW,
            stats,
            cycle,
        )

        assert len(transformed) == 1
        assert transformed[0]["duration"] == 60.0
        assert transformed[0]["activity_state"] == "active"
        self.time_tracker.add_active_time.assert_called_once()

    def test_transform_and_checkpoint_uploads_full_duration_no_input_cap(self):
        """1.5.43: no client-side input-timeout cap. The full event duration is
        uploaded; the server trims idle from the AFK stream. The old cap dropped
        genuinely-active work whenever input detection lagged."""
        cycle = _SyncCycleContext(has_input_data=True)
        now = datetime.now(timezone.utc) - timedelta(minutes=5)
        event = AWEvent(
            id=23,
            timestamp=now,
            duration=20 * 60.0,
            data={"app": "Terminal", "title": "Work"},
        )
        stats = SyncStats()

        transformed, _ = self.engine._transform_and_checkpoint(
            [event],
            "bucket-123",
            BUCKET_TYPE_WINDOW,
            stats,
            cycle,
        )

        assert len(transformed) == 1
        assert transformed[0]["duration"] == 20 * 60.0
        self.time_tracker.add_active_time.assert_called_once()
        assert self.time_tracker.add_active_time.call_args[0][0] == 20 * 60.0

    def test_transform_and_checkpoint_uploads_window_even_after_input_timeout(self):
        """1.5.43: a window beyond last_input + afk_timeout is still uploaded —
        the client never drops it. The server decides if it counts as active.
        Previously this returned [] and silently stranded real activity."""
        cycle = _SyncCycleContext(has_input_data=True)
        self.engine._afk_watcher_available = False
        event = AWEvent(
            id=24,
            timestamp=datetime.now(timezone.utc),
            duration=120.0,
            data={"app": "Terminal", "title": "Work"},
        )
        stats = SyncStats()

        transformed, _ = self.engine._transform_and_checkpoint(
            [event],
            "bucket-123",
            BUCKET_TYPE_WINDOW,
            stats,
            cycle,
        )

        assert len(transformed) == 1
        assert transformed[0]["duration"] == 120.0
        self.time_tracker.add_active_time.assert_called_once()

    def test_within_working_hours_gate(self):
        """1.5.43: B2E/Trainee working-hours enforcement. When enforced, events
        outside [work_start, work_end] or on non-working days are rejected; when
        unenforced (B2B/others), everything passes."""
        wh = self.engine.config.working_hours
        wh.enforced = True
        wh.work_start = "08:00"
        wh.work_end = "22:00"
        wh.working_days = [1, 2, 3, 4, 5]  # Mon-Fri
        wh.timezone = "UTC"

        def ev(dt):
            return AWEvent(id=1, timestamp=dt, duration=60, data={})

        wed = lambda h, m=0: datetime(2026, 6, 17, h, m, tzinfo=timezone.utc)  # Wednesday
        assert self.engine._within_working_hours(ev(wed(7, 30))) is False  # before 08:00
        assert self.engine._within_working_hours(ev(wed(9, 0))) is True    # inside
        assert self.engine._within_working_hours(ev(wed(22, 30))) is False  # after 22:00
        # Saturday 2026-06-20 — non-working day
        sat = datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc)
        assert self.engine._within_working_hours(ev(sat)) is False

        # Unrestricted (B2B): any time passes.
        wh.enforced = False
        assert self.engine._within_working_hours(ev(wed(3, 0))) is True

    def test_transform_and_checkpoint_uses_afk_when_input_is_stale(self):
        """AFK data should remain authoritative when input watcher goes stale."""
        now = datetime.now(timezone.utc)
        self.engine._afk_watcher_available = True
        cycle = _SyncCycleContext(has_input_data=True, afk_events=[
            AWEvent(
                id=25,
                timestamp=now - timedelta(minutes=40),
                duration=40 * 60.0,
                data={"status": "not-afk"},
            ),
        ])
        event = AWEvent(
            id=26,
            timestamp=now - timedelta(minutes=5),
            duration=300.0,
            data={"app": "Terminal", "title": "Work"},
        )
        stats = SyncStats()

        transformed, _ = self.engine._transform_and_checkpoint(
            [event],
            "bucket-123",
            BUCKET_TYPE_WINDOW,
            stats,
            cycle,
        )

        assert len(transformed) == 1
        assert transformed[0]["duration"] == 300.0
        self.time_tracker.add_active_time.assert_called_once()

    def test_transform_window_forces_active_without_input_data(self):
        """A window event with no input data is forced 'active' (we never drop
        real activity when input detection is unavailable). This is the contract
        the backlog reconcile relies on for its fresh, empty context."""
        cycle = _SyncCycleContext(has_input_data=False)
        event = AWEvent(
            id=1, timestamp=datetime.now(timezone.utc), duration=120.0,
            data={"app": "Code", "title": "x"},
        )
        out = self.engine._transform_window_event_with_timeout(
            event, "bucket-1", BUCKET_TYPE_WINDOW, cycle
        )
        assert len(out) == 1
        assert out[0]["activity_state"] == "active"

    def test_reconcile_backlog_replays_window_events_as_active(self):
        """Reconcile builds its OWN fresh _SyncCycleContext (input analysis has
        not run yet), so replayed window events are deterministically classified
        'active' — never inheriting a prior cycle's state. With the per-cycle
        context now threaded explicitly there is no instance field to leak."""
        now = datetime.now(timezone.utc)
        win_event = AWEvent(
            id=1, timestamp=now - timedelta(minutes=5), duration=120.0,
            data={"app": "Code", "title": "x"},
        )
        # Return the event from the reconcile window that actually covers its
        # timestamp (match on the real time range, not a call counter — the
        # latter is fragile near midnight / as the day-length changes).
        def _get_events(bucket_id, start=None, end=None, limit=None):
            if start is not None and end is not None and start <= win_event.timestamp < end:
                return [win_event]
            return []

        self.aw.get_events.side_effect = _get_events
        self.queue.size.return_value = 0
        enqueued: list[dict] = []
        self.queue.enqueue.side_effect = lambda batch: (enqueued.extend(batch), len(batch))[1]

        bucket = Mock(id="win-bucket", type=BUCKET_TYPE_WINDOW)
        self.engine._reconcile_backlog([bucket])

        assert enqueued, "reconcile should enqueue the replayed window event"
        assert all(e["activity_state"] == "active" for e in enqueued)

    def test_heartbeat_uploads_logs_when_requested(self):
        """When the heartbeat response sets logs_requested, the agent uploads
        the betterflow.log tail (relaunch log optional).

        The response MUST be the real server envelope ({"success", "data": {...}}),
        not a top-level flag — _send_heartbeat reads response["data"]. An earlier
        version of this test mocked {"logs_requested": True} at the top level,
        which matched the buggy reader and so passed against code that never
        actually uploaded (live repro: Emilian's 1.5.57 agent, 2026-06-18)."""
        self.bf.heartbeat.return_value = {"success": True, "data": {"logs_requested": True}}

        def fake_tail(path, max_bytes=512 * 1024):
            return b"log-bytes" if path.name == "betterflow.log" else None

        with patch.object(SyncEngine, "_read_log_tail", side_effect=fake_tail):
            result = self.engine._send_heartbeat()

        assert result is None  # no auth error surfaced
        self.bf.upload_logs.assert_called_once_with(b"log-bytes", None)

    def test_heartbeat_does_not_upload_logs_when_not_requested(self):
        """No logs_requested flag -> no upload (the common case)."""
        self.bf.heartbeat.return_value = {"success": True, "data": {"logs_requested": False}}
        self.engine._send_heartbeat()
        self.bf.upload_logs.assert_not_called()

    def test_heartbeat_log_upload_auth_error_propagates(self):
        """A BetterFlowAuthError during the log upload must surface from
        _send_heartbeat (the re-login signal), not be swallowed by the generic
        client-error handler."""
        from src.sync.bf_client import BetterFlowAuthError
        self.bf.heartbeat.return_value = {"success": True, "data": {"logs_requested": True}}
        self.bf.upload_logs.side_effect = BetterFlowAuthError("expired")
        with patch.object(SyncEngine, "_read_log_tail", return_value=b"log-bytes"):
            result = self.engine._send_heartbeat()
        assert isinstance(result, BetterFlowAuthError)

    def test_heartbeat_unwraps_envelope_for_commands(self):
        """Not just logs: every heartbeat-driven field lives under response["data"].
        A 'pause' command in the enveloped response must reach pause() — proving
        the unwrap covers remote commands (silently no-op'd before the fix), not
        only the log upload."""
        self.bf.heartbeat.return_value = {
            "success": True,
            "data": {"commands": [{"type": "pause", "reason": "admin"}]},
        }
        with patch.object(self.engine, "pause") as mock_pause:
            self.engine._send_heartbeat()
        mock_pause.assert_called_once()

    def test_heartbeat_tolerates_unenveloped_response(self):
        """Defensive: the response.get("data", response) fallback means a future
        un-enveloped (top-level) response still works rather than silently
        no-op'ing again."""
        self.bf.heartbeat.return_value = {"logs_requested": True}
        with patch.object(SyncEngine, "_read_log_tail", return_value=b"x"):
            self.engine._send_heartbeat()
        self.bf.upload_logs.assert_called_once()

    def test_unreadable_log_reports_upload_failure_to_ops(self):
        """The remote log fetch exists to diagnose a sick agent — so if the
        upload fails (here: the log is unreadable, the suspected Windows wedge
        case), the failure must surface via the error_reporter (a channel that
        works when the log fetch doesn't). Otherwise the failure is only in the
        local log we couldn't fetch — circular blindness."""
        self.bf.heartbeat.return_value = {"success": True, "data": {"logs_requested": True}}
        self.engine.error_reporter = MagicMock()
        with patch.object(SyncEngine, "_read_log_tail", return_value=None):
            self.engine._send_heartbeat()
        self.bf.upload_logs.assert_not_called()
        self.engine.error_reporter.capture.assert_called_once()
        assert "unreadable" in self.engine.error_reporter.capture.call_args[0][0]

    def test_failed_upload_post_reports_to_ops(self):
        """A POST failure on the upload is likewise surfaced to ops, not just
        logged locally where we can't reach it."""
        from src.sync.bf_client import BetterFlowClientError
        self.bf.heartbeat.return_value = {"success": True, "data": {"logs_requested": True}}
        self.bf.upload_logs.side_effect = BetterFlowClientError("500")
        self.engine.error_reporter = MagicMock()
        with patch.object(SyncEngine, "_read_log_tail", return_value=b"log-bytes"):
            self.engine._send_heartbeat()
        self.engine.error_reporter.capture.assert_called_once()
        assert "POST failed" in self.engine.error_reporter.capture.call_args[0][0]

    def test_successful_upload_does_not_report_failure(self):
        """The happy path must NOT fire a failure report."""
        self.bf.heartbeat.return_value = {"success": True, "data": {"logs_requested": True}}
        self.engine.error_reporter = MagicMock()
        with patch.object(SyncEngine, "_read_log_tail", return_value=b"log-bytes"):
            self.engine._send_heartbeat()
        self.bf.upload_logs.assert_called_once()
        self.engine.error_reporter.capture.assert_not_called()

    def test_heartbeat_stamps_last_beat_time(self):
        """_send_heartbeat records a monotonic stamp so the 60s-tick heartbeat
        floor (main loop) can measure how long since the sync-cadence heartbeat
        last fired. Exercised through the real engine, not a forwarded mock:
        None before the first beat, a small non-negative age after."""
        self.bf.heartbeat.return_value = {"success": True, "data": {}}
        assert self.engine.seconds_since_last_heartbeat() is None
        self.engine._send_heartbeat()
        since = self.engine.seconds_since_last_heartbeat()
        assert since is not None and since >= 0.0

    def test_heartbeat_stamps_even_on_client_error(self):
        """The stamp updates on ATTEMPT (before the HTTP), so a down server
        can't leave the floor thinking a beat never happened and re-firing every
        tick — one attempt per interval, mirroring the paused-liveness throttle."""
        from src.sync.http_client import BetterFlowClientError
        self.bf.heartbeat.side_effect = BetterFlowClientError("500")
        self.engine._send_heartbeat()
        assert self.engine.seconds_since_last_heartbeat() is not None

    def test_concurrent_send_heartbeat_no_double_dispatch(self):
        """_send_heartbeat is reachable from two scheduler threads at once (the
        sync-cadence path and the 60s-tick floor), which for an idle device are
        both live with harmonic periods. The non-blocking in-flight guard must
        make a second concurrent caller no-op instead of double-POSTing and
        double-processing commands (e.g. a double logs_requested upload)."""
        entered = threading.Event()
        release = threading.Event()

        def slow_heartbeat(*_a, **_k):
            entered.set()
            release.wait(timeout=1.0)
            return {"success": True, "data": {"logs_requested": True}}

        self.bf.heartbeat.side_effect = slow_heartbeat
        with patch.object(SyncEngine, "_read_log_tail", return_value=b"x"):
            t1 = threading.Thread(target=self.engine._send_heartbeat)
            t1.start()
            assert entered.wait(1.0)  # t1 now holds the guard, inside heartbeat()
            entered.clear()
            t2 = threading.Thread(target=self.engine._send_heartbeat)
            t2.start()
            # Guarded: t2 no-ops fast. Unguarded (the bug): t2 re-enters
            # heartbeat() and blocks on release, re-setting `entered`.
            t2.join(0.3)
            second_re_entered = entered.is_set()
            release.set()
            t1.join(2.0)
            t2.join(2.0)

        assert self.bf.heartbeat.call_count == 1
        self.bf.upload_logs.assert_called_once()
        assert not second_re_entered, (
            "second concurrent _send_heartbeat must no-op, not re-enter the body"
        )

    def test_seconds_since_last_heartbeat_frozen_clock_stays_fresh(self):
        """monotonic can freeze across macOS sleep. If it does, the beat's age
        stays ~0 so the floor reads 'fresh' and won't fire a burst on wake — the
        lid-flap protection the audit added, now at the engine stamp (its single
        source of truth) instead of a separate main.py throttle."""
        self.bf.heartbeat.return_value = {"success": True, "data": {}}
        with patch("src.sync.sync_engine.time.monotonic", return_value=1000.0):
            self.engine._send_heartbeat()  # stamp = 1000
            assert self.engine.seconds_since_last_heartbeat() == 0.0

    def test_seconds_since_last_heartbeat_backward_clock_reads_fresh(self):
        """A backward-jumping monotonic yields a negative age, which is below any
        positive floor, so it reads as fresh — never a spurious stale-fire."""
        self.bf.heartbeat.return_value = {"success": True, "data": {}}
        with patch(
            "src.sync.sync_engine.time.monotonic", side_effect=[1000.0, 500.0]
        ):
            self.engine._send_heartbeat()  # stamp = 1000
            since = self.engine.seconds_since_last_heartbeat()  # reads 500
        assert since == -500.0  # < any positive floor -> treated fresh

    def test_heartbeat_below_min_version_triggers_update(self):
        """When the server advertises a minimum_agent_version above ours, the
        heartbeat fires on_update_required so the handler can stage the build —
        the fleet-push lever (not just a log line)."""
        self.bf.heartbeat.return_value = {
            "success": True, "data": {"minimum_agent_version": "999.0.0"},
        }
        self.engine.on_update_required = MagicMock()
        self.engine._send_heartbeat()
        self.engine.on_update_required.assert_called_once_with("999.0.0")

    def test_heartbeat_at_or_above_min_version_does_not_trigger(self):
        """An up-to-date agent must not be told to update."""
        self.bf.heartbeat.return_value = {
            "success": True, "data": {"minimum_agent_version": "0.0.1"},
        }
        self.engine.on_update_required = MagicMock()
        self.engine._send_heartbeat()
        self.engine.on_update_required.assert_not_called()

    def test_dropped_queue_events_are_reported_to_ops(self):
        """Events that exhaust their retries and get dropped are permanent data
        loss (captured activity the server never accepted). With the server now
        confirming delivery per-event, anything reaching the ceiling is a genuine
        rejection — it must surface to ops, not vanish behind a local log line."""
        from src.sync.sync_engine import SyncStats
        self.queue.failed_event_summary.return_value = {
            "count": 3,
            "bucket_ids": ["aw-watcher-window_host"],
            "oldest": "2026-06-23T05:00:00Z",
            "newest": "2026-06-23T05:10:00Z",
        }
        self.queue.dequeue.return_value = []  # nothing left to drain
        self.engine.error_reporter = MagicMock()

        self.engine._process_queue(SyncStats())

        self.engine.error_reporter.capture.assert_called_once()
        msg = self.engine.error_reporter.capture.call_args[0][0]
        assert "Dropped 3 queued event" in msg
        assert self.engine.error_reporter.capture.call_args.kwargs["fingerprint"] == (
            "offline-queue-events-dropped"
        )
        self.queue.remove_failed.assert_called_once_with(max_retries=5)

    def test_no_dropped_events_does_not_report(self):
        """A clean queue (nothing past the retry ceiling) must not fire a report."""
        from src.sync.sync_engine import SyncStats
        self.queue.failed_event_summary.return_value = {
            "count": 0, "bucket_ids": [], "oldest": None, "newest": None,
        }
        self.queue.dequeue.return_value = []
        self.engine.error_reporter = MagicMock()

        self.engine._process_queue(SyncStats())

        self.engine.error_reporter.capture.assert_not_called()

    def test_read_log_tail_normalizes_invalid_utf8(self):
        """A cp1252 byte from a Windows log (e.g. \\x97) must come back as VALID
        UTF-8. Otherwise the server's INSERT into the utf8 content column fails
        with MySQL 1366 'Incorrect string value' and the upload is silently
        dropped — the real reason Windows logs never landed (Sachi, 2026-06-18)."""
        import os
        import tempfile
        from pathlib import Path

        fd, p = tempfile.mkstemp()
        os.write(fd, b"hello \x97 world")  # \x97 is not valid UTF-8
        os.close(fd)
        try:
            tail = SyncEngine._read_log_tail(Path(p))
        finally:
            os.unlink(p)

        assert tail is not None
        tail.decode("utf-8")  # must not raise — the whole point of the fix
        assert b"\x97" not in tail
        assert b"hello" in tail and b"world" in tail

    def test_get_category_db_only_returns_none_for_unmapped(self):
        """Test that _get_category only checks DB, returns None for unmapped apps."""
        self.queue.get_all_categories.return_value = {}
        self.engine._category_cache = None

        result = self.engine._get_category("Claude")
        assert result is None
        self.queue.set_category.assert_not_called()

    def test_transform_event_persists_fallback_category(self):
        """Test that fallback categories are persisted with source='fallback' via _transform_event."""
        self.queue.get_all_categories.return_value = {}
        self.engine._category_cache = None

        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "Claude", "title": "Chat"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)
        assert result is not None
        assert result["data"]["app_category"] == "development"
        self.queue.set_category.assert_called_once_with("Claude", "development", source="fallback")

    def test_transform_event_persists_fallback_only_once(self):
        """Test that repeated events for the same app don't re-persist fallback."""
        self.queue.get_all_categories.return_value = {}
        self.engine._category_cache = None

        for i in range(3):
            event = AWEvent(
                id=i + 10,
                timestamp=datetime.now(timezone.utc),
                duration=60,
                data={"app": "Claude", "title": "Chat"},
            )
            self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)

        # Only one DB write despite three events
        self.queue.set_category.assert_called_once()

    def test_get_category_db_value_takes_precedence(self):
        """Test that a DB-cached value takes precedence over fallback."""
        self.queue.get_all_categories.return_value = {"Claude": "productivity"}
        self.engine._category_cache = None

        result = self.engine._get_category("Claude")
        assert result == "productivity"
        self.queue.set_category.assert_not_called()

    def test_get_category_double_miss_returns_none(self):
        """Test that _get_category returns None when both DB and fallback miss."""
        self.queue.get_all_categories.return_value = {}
        self.engine._category_cache = None

        result = self.engine._get_category("SomeObscureApp")
        assert result is None

    def test_get_category_empty_string_in_db_is_not_bypassed(self):
        """Test that an empty-string category from DB is returned (not treated as miss)."""
        self.queue.get_all_categories.return_value = {"Claude": ""}
        self.engine._category_cache = None

        result = self.engine._get_category("Claude")
        assert result == ""

    def test_db_sourced_category_not_repersisted_as_fallback(self):
        """Test that a DB-sourced category does NOT trigger fallback persistence."""
        self.queue.get_all_categories.return_value = {"Claude": "productivity"}
        self.engine._category_cache = None

        event = AWEvent(
            id=1, timestamp=datetime.now(timezone.utc), duration=60,
            data={"app": "Claude", "title": "Chat"},
        )
        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)
        assert result["data"]["app_category"] == "productivity"
        self.queue.set_category.assert_not_called()

    def test_persisted_fallbacks_cleared_on_shutdown(self):
        """_persisted_fallbacks must be cleared on shutdown for clean re-login."""
        self.queue.get_all_categories.return_value = {}
        self.engine._category_cache = None

        event = AWEvent(
            id=1, timestamp=datetime.now(timezone.utc), duration=60,
            data={"app": "Claude", "title": "Chat"},
        )
        self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)
        assert "Claude" in self.engine._persisted_fallbacks

        self.engine.shutdown()
        assert len(self.engine._persisted_fallbacks) == 0

    def test_invalidate_cache_clears_persisted_fallbacks(self):
        """invalidate_category_cache must also clear _persisted_fallbacks."""
        self.engine._persisted_fallbacks.add("Claude")
        self.engine.invalidate_category_cache()
        assert len(self.engine._persisted_fallbacks) == 0

    def test_transform_event_nan_duration_returns_none(self):
        """Test that NaN duration is rejected (would crash timedelta otherwise)."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=float("nan"),
            data={"app": "Test"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)
        assert result is None

    def test_transform_event_inf_duration_returns_none(self):
        """Test that infinite duration is rejected."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=float("inf"),
            data={"app": "Test"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)
        assert result is None

    def test_transform_event_filters_excluded_apps(self):
        """Test that excluded apps are filtered."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "1Password"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)
        assert result is None

    def test_transform_event_includes_title(self):
        """Test that window events include title."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "Firefox", "title": "Some Title - Mozilla Firefox"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)

        assert result is not None
        # Title is passed through (server handles privacy)
        assert result["data"]["title"] == "Some Title - Mozilla Firefox"

    def test_transform_event_extracts_domain_from_url(self):
        """Test that URLs are stripped to domain only."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={
                "app": "Chrome",
                "title": "Test",
                "url": "https://github.com/BetterQA/betterflow/pull/123",
            },
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)

        assert result is not None
        assert result["data"]["url"] == "github.com"

    def test_transform_event_handles_afk_bucket(self):
        """Test transforming AFK events."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=300,
            data={"status": "not-afk"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_AFK)

        assert result is not None
        assert result["data"]["status"] == "not-afk"

    def test_transform_event_enriches_browser_url_from_tracker(self):
        """A browser window event with no URL is enriched from the browser
        tracker, then domain-stripped by the default privacy policy."""

        class _StubTracker:
            def url_at(self, ts):
                return "https://github.com/Better-Quality-Assurance/x/pull/1"

        self.engine._browser_tracker = _StubTracker()
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "Google Chrome", "title": "PR"},
        )

        result = self.engine._transform_event(event, "bucket-1", BUCKET_TYPE_WINDOW)

        assert result is not None
        # domain_only_urls=True by default -> domain, not the full URL
        assert result["data"]["url"] == "github.com"

    def test_transform_event_no_tracker_url_for_non_browser_app(self):
        """Non-browser apps are never queried for a URL, even with a tracker."""

        class _StubTracker:
            def url_at(self, ts):
                raise AssertionError("url_at must not be called for non-browsers")

        self.engine._browser_tracker = _StubTracker()
        event = AWEvent(
            id=2,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "Terminal", "title": "zsh"},
        )

        result = self.engine._transform_event(event, "bucket-1", BUCKET_TYPE_WINDOW)

        assert result is not None
        assert "url" not in result["data"]

    def test_transform_event_afk_status_not_relabeled_as_break(self):
        """AFK (away-from-keyboard) must NOT be sent as a 'break'.

        A break is an intentional user-initiated pause (break_time). Blanket-
        relabeling AFK as break turned ordinary no-input work (reading, meetings,
        watching a screen) into phantom 'Break' cards for people who took no
        break. AFK keeps its real bucket_type so the backend classifies long
        spans as Idle.
        """
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=600,
            data={"status": "afk"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_AFK)

        assert result is not None
        assert result["data"]["status"] == "afk"
        assert result["bucket_type"] == BUCKET_TYPE_AFK
        assert result["bucket_type"] != "break"

    def test_transform_event_adds_activity_state_for_window_events(self):
        """Test that window events include activity state and metrics."""
        cycle = _SyncCycleContext(has_input_data=True)
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "Firefox", "title": "Test"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW, cycle=cycle)

        assert result is not None
        assert "activity_state" in result
        assert "activity_metrics" in result
        assert result["activity_state"] == "active"

    def test_transform_event_tracks_active_time_for_active_events(self):
        """Test that active events add time to tracker."""
        cycle = _SyncCycleContext(has_input_data=True)
        self.activity_analyzer.get_activity_state.return_value = "active"

        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "Firefox", "title": "Test"},
        )

        self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW, cycle=cycle)

        # Should call add_active_time with duration and date
        self.time_tracker.add_active_time.assert_called_once()
        args = self.time_tracker.add_active_time.call_args[0]
        assert args[0] == 60  # duration

    def test_transform_event_tracks_idle_active_time(self):
        """Test that idle-active events still add counted time to tracker."""
        cycle = _SyncCycleContext(has_input_data=True)
        self.activity_analyzer.get_activity_state.return_value = "idle-active"

        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "Firefox", "title": "Test"},
        )

        self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW, cycle=cycle)

        self.time_tracker.add_active_time.assert_called_once()

    def test_transform_event_no_input_data_uses_afk_for_active(self):
        """Test that without input data, AFK 'not-afk' events mark window as active."""
        self.engine._afk_watcher_available = True
        now = datetime.now(timezone.utc)
        # AFK says user is active during this window
        cycle = _SyncCycleContext(has_input_data=False, afk_events=[
            AWEvent(id=10, timestamp=now - timedelta(seconds=10), duration=120, data={"status": "not-afk"}),
        ])
        event = AWEvent(id=1, timestamp=now, duration=60, data={"app": "Firefox", "title": "Test"})

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW, cycle=cycle)

        assert result is not None
        assert result["activity_state"] == "active"
        self.time_tracker.add_active_time.assert_called_once()

    def test_transform_event_no_input_data_uses_afk_for_inactive(self):
        """Test that without input data, AFK 'afk' events mark window as inactive."""
        self.engine._afk_watcher_available = True
        now = datetime.now(timezone.utc)
        # AFK says user is idle during this window
        cycle = _SyncCycleContext(has_input_data=False, afk_events=[
            AWEvent(id=10, timestamp=now - timedelta(seconds=10), duration=120, data={"status": "afk"}),
        ])
        event = AWEvent(id=1, timestamp=now, duration=60, data={"app": "Firefox", "title": "Test"})

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW, cycle=cycle)

        assert result is not None
        assert result["activity_state"] == "inactive"
        self.time_tracker.add_active_time.assert_not_called()

    def test_transform_event_no_input_data_no_afk_watcher_defaults_active(self):
        """Test that without AFK watcher, defaults to active.

        When the AFK watcher is completely down, window events still prove
        the user was at the computer. Defaulting to inactive would cause
        silent zero-hour days which is worse than slightly inflated counts.
        """
        cycle = _SyncCycleContext(has_input_data=False, afk_events=[])
        self.engine._afk_watcher_available = False
        event = AWEvent(
            id=1, timestamp=datetime.now(timezone.utc), duration=60,
            data={"app": "Firefox", "title": "Test"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW, cycle=cycle)

        assert result is not None
        assert result["activity_state"] == "active"

    def test_transform_event_no_input_data_afk_watcher_no_events_defaults_inactive(self):
        """Test that with AFK watcher running but no matching events, defaults to inactive.

        When the AFK watcher is available but has no events covering this
        window event's time range, the user was genuinely idle.
        """
        cycle = _SyncCycleContext(has_input_data=False, afk_events=[])
        self.engine._afk_watcher_available = True
        event = AWEvent(
            id=1, timestamp=datetime.now(timezone.utc), duration=60,
            data={"app": "Firefox", "title": "Test"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW, cycle=cycle)

        assert result is not None
        assert result["activity_state"] == "inactive"
        self.time_tracker.add_active_time.assert_not_called()

    def test_transform_event_input_bucket_no_activity_state(self):
        """Test that input events don't get activity state."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=1,
            data={"presses": 10, "clicks": 5, "scrolls": 2},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_INPUT)

        assert result is not None
        assert "activity_state" not in result
        assert result["data"]["presses"] == 10

    def test_get_status(self):
        """Test getting sync status."""
        self.aw.is_running.return_value = True
        self.bf.is_reachable.return_value = True
        self.queue.size.return_value = 10
        self.queue.get_all_checkpoints.return_value = {}

        status = self.engine.get_status()

        assert status["aw_running"] is True
        assert status["bf_reachable"] is True
        assert status["queue_size"] == 10

    def test_get_today_active_time(self):
        """Test getting today's active time from tracker."""
        expected = timedelta(hours=2, minutes=30)
        self.time_tracker.get_today_active_time.return_value = expected

        result = self.engine.get_today_active_time()

        assert result == expected

    def test_shutdown_ends_session(self):
        """Test that shutdown ends the active session."""
        self.engine._session_active = True

        self.engine.shutdown()

        self.bf.end_session.assert_called_once_with("app_quit")
        assert self.engine._session_active is False
        self.time_tracker.close.assert_called_once()

    def test_report_dropped_events_warns_on_real_loss(self):
        """A drop with real_loss_count > 0 escalates at warning level, and the
        message carries NO PII to the cross-tenant ops ingest: bucket TYPES only
        (no hostname suffix) and NO wall-clock activity timestamps (age only)."""
        self.engine.error_reporter = MagicMock()
        self.engine._report_dropped_events({
            "count": 2, "real_loss_count": 1, "unstorable_count": 1,
            "bucket_ids": ["aw-watcher-window_Razvan-MacBook-Pro"],
            "oldest": "2026-06-24T07:30:00+00:00", "newest": "2026-06-24T07:31:00+00:00",
        })
        self.engine.error_reporter.capture.assert_called_once()
        call = self.engine.error_reporter.capture.call_args
        msg = call.args[0]
        assert call.kwargs["level"] == "warning"
        # F4: hostname (often a person's name) must be stripped to the bucket type.
        assert "aw-watcher-window" in msg
        assert "Razvan-MacBook-Pro" not in msg
        # F3: no raw wall-clock activity timestamps in the ops report.
        assert "2026-06-24T07:30:00" not in msg and "2026-06-24T07:31:00" not in msg

    def test_report_dropped_events_info_when_all_unstorable(self):
        """All-unstorable drops (stale / no-bucket) are a benign flush — reported
        at info with a distinct fingerprint so they don't page ops as warnings."""
        self.engine.error_reporter = MagicMock()
        self.engine._report_dropped_events({
            "count": 20, "real_loss_count": 0, "unstorable_count": 20,
            "bucket_ids": ["aw-watcher-input_h"],
            "oldest": "2026-06-17T06:04:00+00:00", "newest": "2026-06-17T06:07:00+00:00",
        })
        self.engine.error_reporter.capture.assert_called_once()
        kwargs = self.engine.error_reporter.capture.call_args.kwargs
        assert kwargs["level"] == "info"
        assert kwargs["fingerprint"] != "offline-queue-events-dropped"

    def test_sync_flags_bucket_fetch_failure(self):
        """When AW answers /info (is_running True) but get_buckets fails (503 from
        a half-hung bf-data-service), sync() flags aw_bucket_fetch_failed so the
        coordinator can force_restart — is_running() alone can't see this."""
        from src.sync.aw_client import AWClientError
        self.aw.is_running.return_value = True
        self.bf.is_reachable.return_value = True
        self.engine._config_fetched = True
        self.aw.get_window_buckets.side_effect = AWClientError("ActivityWatch API error: 503")

        stats = self.engine.sync()

        assert stats.aw_bucket_fetch_failed is True
        assert stats.success is False

    def test_transform_event_delta_time_tracking_on_refetch(self):
        """Test that re-fetched events with grown duration only add the delta."""
        cycle = _SyncCycleContext(has_input_data=True)
        self.activity_analyzer.get_activity_state.return_value = "active"

        event_v1 = AWEvent(
            id=42,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "VSCode", "title": "editor"},
        )

        # First fetch — full 60s should be added
        self.engine._transform_event(event_v1, "bucket-1", BUCKET_TYPE_WINDOW, cycle=cycle)
        self.time_tracker.add_active_time.assert_called_once()
        assert self.time_tracker.add_active_time.call_args[0][0] == 60

        self.time_tracker.add_active_time.reset_mock()

        # Re-fetch with grown duration (heartbeat extension: 60 → 90)
        event_v2 = AWEvent(
            id=42,
            timestamp=event_v1.timestamp,
            duration=90,
            data={"app": "VSCode", "title": "editor"},
        )
        self.engine._transform_event(event_v2, "bucket-1", BUCKET_TYPE_WINDOW, cycle=cycle)
        self.time_tracker.add_active_time.assert_called_once()
        assert self.time_tracker.add_active_time.call_args[0][0] == 30  # delta only

    def test_transform_event_no_double_count_same_duration(self):
        """Test that re-fetched events with same duration don't add time."""
        cycle = _SyncCycleContext(has_input_data=True)
        self.activity_analyzer.get_activity_state.return_value = "active"

        event = AWEvent(
            id=99,
            timestamp=datetime.now(timezone.utc),
            duration=120,
            data={"app": "Firefox", "title": "Test"},
        )

        # First fetch
        self.engine._transform_event(event, "bucket-1", BUCKET_TYPE_WINDOW, cycle=cycle)
        self.time_tracker.add_active_time.reset_mock()

        # Re-fetch with identical duration — delta is 0, should not call
        self.engine._transform_event(event, "bucket-1", BUCKET_TYPE_WINDOW, cycle=cycle)
        self.time_tracker.add_active_time.assert_not_called()

    def test_transform_event_different_buckets_track_separately(self):
        """Test that same event ID in different buckets tracks time independently."""
        cycle = _SyncCycleContext(has_input_data=True)
        self.activity_analyzer.get_activity_state.return_value = "active"

        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "Chrome", "title": "Test"},
        )

        # Same event ID, different bucket — both should add full duration
        self.engine._transform_event(event, "bucket-A", BUCKET_TYPE_WINDOW, cycle=cycle)
        self.engine._transform_event(event, "bucket-B", BUCKET_TYPE_WINDOW, cycle=cycle)

        assert self.time_tracker.add_active_time.call_count == 2
        for call in self.time_tracker.add_active_time.call_args_list:
            assert call[0][0] == 60

    def test_fetch_server_config_updates_analyzer_thresholds(self):
        """Test that fetching server config updates analyzer thresholds."""
        self.bf.get_config.return_value = {
            "engagement": {
                "sustained_typing_presses": 100,
                "window_changes_min": 3,
            }
        }

        self.engine.fetch_server_config()

        self.activity_analyzer.update_thresholds.assert_called_once()

    def test_time_cache_uses_cache_lock(self):
        """Test that _time_cache operations are protected by _cache_lock."""
        import threading

        cycle = _SyncCycleContext(has_input_data=True)
        self.activity_analyzer.get_activity_state.return_value = "active"

        event = AWEvent(
            id=50,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "VSCode", "title": "editor"},
        )

        # Replace _cache_lock with a counting wrapper
        acquire_count = 0
        real_lock = threading.Lock()

        class CountingLock:
            def acquire(self, *args, **kwargs):
                nonlocal acquire_count
                acquire_count += 1
                return real_lock.acquire(*args, **kwargs)

            def release(self, *args, **kwargs):
                return real_lock.release(*args, **kwargs)

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *args):
                self.release()

        self.engine._cache_lock = CountingLock()

        self.engine._transform_event(event, "bucket-1", BUCKET_TYPE_WINDOW, cycle=cycle)

        # _cache_lock should have been acquired for _time_cache operations
        # _transform_event accesses _time_cache when activity_state is "active"
        assert acquire_count >= 1, f"Expected >= 1 lock acquisition for _time_cache, got {acquire_count}"


class TestSendEventsDecoupleBuckets:
    """#4: a transient failure in one bucket must not taint another."""

    def setup_method(self):
        self.aw = Mock()
        self.bf = Mock()
        self.queue = Mock()
        self.queue.get_checkpoint.return_value = None
        self.config = Config()
        self.engine = SyncEngine(
            aw=self.aw,
            bf=self.bf,
            queue=self.queue,
            config=self.config,
            activity_analyzer=Mock(spec=ActivityAnalyzer),
            time_tracker=Mock(spec=DailyTimeTracker),
        )

    @staticmethod
    def _ev(bucket_id, eid):
        return {"id": f"{bucket_id}_{eid}", "bucket_id": bucket_id, "duration": 60}

    def test_afk_failure_does_not_taint_window(self):
        """AFK send fails transiently; window send succeeds → only AFK is
        queued/back-off-marked. Previously the mixed batch re-queued both."""
        from src.sync.bf_client import SyncResult
        from src.sync.sync_engine import SyncStats

        def fake_send(batch):
            if any("afk" in e.get("bucket_id", "") for e in batch):
                return SyncResult(success=False, error="transient failure")
            return SyncResult(success=True, events_synced=len(batch))

        self.bf.send_events.side_effect = fake_send

        events = [
            self._ev("aw-watcher-window_host", 1),
            self._ev("bf-afk-inproc_host", 2),
            self._ev("aw-watcher-window_host", 3),
        ]
        stats = SyncStats()
        self.engine._send_events(events, stats)

        # Only the AFK bucket is queued/tainted.
        assert "bf-afk-inproc_host" in stats.queued_bucket_ids
        assert "aw-watcher-window_host" not in stats.queued_bucket_ids
        # Exactly the two AFK events were enqueued, window events were not.
        enqueued = [e for call in self.queue.enqueue.call_args_list for e in call.args[0]]
        assert {e["id"] for e in enqueued} == {"bf-afk-inproc_host_2"}
        assert stats.events_queued == 1
        assert stats.events_sent == 2

    def test_no_batch_mixes_buckets(self):
        """Each send_events call carries a single bucket's events only."""
        from src.sync.bf_client import SyncResult
        from src.sync.sync_engine import SyncStats

        self.bf.send_events.return_value = SyncResult(success=True, events_synced=1)
        events = [
            self._ev("aw-watcher-window_host", 1),
            self._ev("bf-afk-inproc_host", 2),
            self._ev("aw-watcher-input_host", 3),
        ]
        self.engine._send_events(events, SyncStats())

        for call in self.bf.send_events.call_args_list:
            buckets = {e["bucket_id"] for e in call.args[0]}
            assert len(buckets) == 1, f"batch mixed buckets: {buckets}"

    def test_single_bucket_transient_failure_unchanged(self):
        """Regression: a single-bucket cycle still queues that whole bucket."""
        from src.sync.bf_client import SyncResult
        from src.sync.sync_engine import SyncStats

        self.bf.send_events.return_value = SyncResult(success=False, error="down")
        events = [self._ev("aw-watcher-window_host", i) for i in range(3)]
        stats = SyncStats()
        self.engine._send_events(events, stats)

        assert stats.queued_bucket_ids == {"aw-watcher-window_host"}
        assert stats.events_queued == 3
        assert stats.events_sent == 0

    def test_afk_failure_advances_window_checkpoint(self):
        """End-to-end goal: with AFK failing and window succeeding, the window
        checkpoint advances while the AFK checkpoint is withheld. This is the
        actual decouple win — exercises _send_and_advance_checkpoints, not just
        _send_events."""
        from src.sync.bf_client import SyncResult
        from src.sync.sync_engine import SyncStats

        def fake_send(batch):
            if any("afk" in e.get("bucket_id", "") for e in batch):
                return SyncResult(success=False, error="transient failure")
            return SyncResult(success=True, events_synced=len(batch))

        self.bf.send_events.side_effect = fake_send

        win_ts = datetime(2026, 6, 22, 10, 0, 0, tzinfo=timezone.utc)
        afk_ts = datetime(2026, 6, 22, 10, 0, 5, tzinfo=timezone.utc)
        all_events = [
            self._ev("aw-watcher-window_host", 1),
            self._ev("bf-afk-inproc_host", 2),
        ]
        pending = [
            ("aw-watcher-window_host", win_ts, 1),
            ("bf-afk-inproc_host", afk_ts, 2),
        ]
        stats = SyncStats()
        self.engine._send_and_advance_checkpoints(all_events, pending, stats)

        advanced = {
            call.args[0] for call in self.queue.set_checkpoint_forward.call_args_list
        }
        assert "aw-watcher-window_host" in advanced  # healthy bucket advances
        assert "bf-afk-inproc_host" not in advanced  # failed bucket withheld


class TestStatusSpanEvents:
    """Tests for break_time / idle_time / sleep_time / private_time emission."""

    def setup_method(self):
        from src.sync.bf_client import SyncResult
        self.aw = Mock()
        self.bf = Mock()
        self.bf.send_events.return_value = SyncResult(success=True, events_synced=1)
        self.queue = Mock()
        self.queue.get_checkpoint.return_value = None
        self.config = Config()
        self.activity_analyzer = Mock(spec=ActivityAnalyzer)
        self.time_tracker = Mock(spec=DailyTimeTracker)
        self.engine = SyncEngine(
            aw=self.aw, bf=self.bf, queue=self.queue, config=self.config,
            activity_analyzer=self.activity_analyzer, time_tracker=self.time_tracker,
        )

    def _captured_event(self):
        assert self.bf.send_events.call_count == 1
        events = self.bf.send_events.call_args[0][0]
        assert len(events) == 1
        return events[0]

    def test_send_sleep_event_emits_sleep_time_bucket(self):
        """sleep_time bucket distinguishes machine-asleep from user-idle."""
        start = datetime(2026, 6, 9, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 9, 6, 30, 0, tzinfo=timezone.utc)

        self.engine.send_sleep_event(start, end)

        ev = self._captured_event()
        assert ev["bucket_type"] == "sleep_time"
        assert ev["data"]["status"] == "sleep"
        assert ev["duration"] == pytest.approx(6.5 * 3600)
        assert ev["id"].startswith("sleep_")

    def test_send_sleep_event_queues_on_network_failure(self):
        """Network failure paths through queue, not silent drop."""
        from src.sync.bf_client import SyncResult
        self.bf.send_events.return_value = SyncResult(success=False, error="network down")
        start = datetime(2026, 6, 9, 0, 0, 0, tzinfo=timezone.utc)

        self.engine.send_sleep_event(start, start + timedelta(hours=6))

        self.queue.enqueue.assert_called_once()
        queued = self.queue.enqueue.call_args[0][0]
        assert queued[0]["bucket_type"] == "sleep_time"

    def test_send_sleep_event_with_end_before_start_is_dropped_with_warning(self, caplog):
        """NTP step backwards during a long Mac sleep could make end<start.

        Without the explicit log, the existing `if duration < 1: return`
        silently discarded the entire span (potentially hours long). We
        still drop the event — submitting negative durations would poison
        the server aggregator — but the warning makes the drop observable.
        """
        import logging
        start = datetime(2026, 6, 9, 8, 0, 0, tzinfo=timezone.utc)
        end = start - timedelta(seconds=30)
        with caplog.at_level(logging.WARNING, logger="src.sync.sync_engine"):
            self.engine.send_sleep_event(start, end)
        self.bf.send_events.assert_not_called()
        self.queue.enqueue.assert_not_called()
        assert any("NTP clock correction" in r.message for r in caplog.records), (
            "negative-duration drop must log a warning so it is observable"
        )

    def test_sleep_idle_break_get_distinct_id_prefixes_and_bucket_types(self):
        """Status spans with same start must not collide on id OR bucket_type.

        Asserting `bucket_type` for break/idle (not just sleep) protects
        against a regression where _send_status_span was hardcoded to one
        kind — the id-prefix-only check alone would silently miss it.
        """
        from src.sync.bf_client import SyncResult
        self.bf.send_events.return_value = SyncResult(success=True, events_synced=1)
        start = datetime(2026, 6, 9, 0, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(minutes=30)

        self.engine.send_idle_event(start, end)
        idle_ev = self.bf.send_events.call_args[0][0][0]
        self.bf.send_events.reset_mock()

        self.engine.send_sleep_event(start, end)
        sleep_ev = self.bf.send_events.call_args[0][0][0]
        self.bf.send_events.reset_mock()

        self.engine.send_break_event(start, end)
        break_ev = self.bf.send_events.call_args[0][0][0]

        # Ids
        assert idle_ev["id"].startswith("idle_")
        assert sleep_ev["id"].startswith("sleep_")
        assert break_ev["id"].startswith("break_")
        assert idle_ev["id"] != sleep_ev["id"] != break_ev["id"]
        # Bucket types — the field the server keys on for event_type inference
        assert idle_ev["bucket_type"] == "idle_time"
        assert sleep_ev["bucket_type"] == "sleep_time"
        assert break_ev["bucket_type"] == "break_time"
        # data.status (sent through to the timeline)
        assert idle_ev["data"]["status"] == "idle"
        assert sleep_ev["data"]["status"] == "sleep"
        assert break_ev["data"]["status"] == "break"


class TestSystemSleepWakeEmitsSleepSpan:
    """on_system_wake must emit a sleep_time event for the sleep span."""

    def _make_handler(self):
        from src.system_event_handler import SystemEventHandler
        sync_engine = Mock()
        sync_engine.is_private = False
        coordinator = Mock()
        coordinator.is_on_break = False
        coordinator.paused_by_network = False
        handler = SystemEventHandler(
            sync_engine=sync_engine,
            tray=Mock(),
            coordinator=coordinator,
            reminder_manager=Mock(),
            bf=Mock(),
            aw=Mock(),
            pause_state_lock=threading.RLock(),
            shutdown_fn=Mock(),
        )
        return handler, sync_engine

    def test_wake_emits_sleep_event_for_captured_span(self):
        handler, sync_engine = self._make_handler()
        handler.on_system_sleep()
        sleep_start = handler._sleep_start
        assert sleep_start is not None

        handler.on_system_wake()

        sync_engine.send_sleep_event.assert_called_once()
        passed_start = sync_engine.send_sleep_event.call_args[0][0]
        assert passed_start == sleep_start
        # Must be cleared so the next sleep doesn't reuse this start
        assert handler._sleep_start is None

    def test_wake_without_prior_sleep_does_not_emit(self):
        handler, sync_engine = self._make_handler()
        handler.on_system_wake()
        sync_engine.send_sleep_event.assert_not_called()

    def test_two_sleeps_without_wake_keep_earliest_timestamp(self):
        """macOS can fire Display Sleep then System Sleep without a wake between.

        Overwriting _sleep_start would silently truncate the front of the
        sleep span — emit the *earliest* timestamp instead.
        """
        handler, sync_engine = self._make_handler()
        handler.on_system_sleep()
        first_start = handler._sleep_start
        assert first_start is not None
        import time as _time
        _time.sleep(0.01)  # ensure a measurable timestamp difference
        handler.on_system_sleep()
        second_start = handler._sleep_start
        assert second_start == first_start, (
            "second sleep without an intervening wake must NOT overwrite _sleep_start"
        )

    def test_send_sleep_event_failure_does_not_break_wake_flow(self):
        handler, sync_engine = self._make_handler()
        sync_engine.send_sleep_event.side_effect = RuntimeError("queue full")
        handler.on_system_sleep()
        # Must not raise — wake flow continues even if event emission fails.
        handler.on_system_wake()
        sync_engine.resume.assert_called_once()

    def test_private_active_at_sleep_is_ended_and_not_restored_on_wake(self):
        """A forgotten Private Time toggle must not survive a sleep.

        Private has no auto-timeout, so a user who enables it and whose machine
        then sleeps would otherwise stay private across the sleep AND into the
        next awake session — silently marking real post-wake work as private and
        uncounted (Raluca, 2026-06-24: a ~20-min private toggle stayed on ~11h
        overnight and swallowed her evening). End it at the sleep boundary (the
        normal leave path records the true enable→sleep span) and resume NORMAL
        tracking on wake, never re-entering private.
        """
        handler, sync_engine = self._make_handler()
        sync_engine.is_private = True

        handler.on_system_sleep()
        sync_engine.set_private_mode.assert_called_once_with(False)

        sync_engine.set_private_mode.reset_mock()
        handler.on_system_wake()

        assert not any(
            call.args == (True,)
            for call in sync_engine.set_private_mode.call_args_list
        ), "wake must NOT restore private mode"
        sync_engine.resume.assert_called()

    def test_non_private_sleep_does_not_touch_private_mode(self):
        """When the user was NOT private at sleep, the flow never calls
        set_private_mode (no spurious enter/leave)."""
        handler, sync_engine = self._make_handler()
        sync_engine.is_private = False

        handler.on_system_sleep()
        handler.on_system_wake()

        sync_engine.set_private_mode.assert_not_called()


class TestSyncCoordinatorBreak:
    """Tests for SyncCoordinator break state management."""

    def setup_method(self):
        self.config = Config()
        self.aw = Mock()
        self.bf = Mock()
        self.queue = Mock()
        self.queue.get_checkpoint.return_value = None
        self.sync_engine = Mock(spec=SyncEngine)
        self.sync_engine.is_paused = False
        self.sync_engine.is_private = False
        self.tray = Mock()
        self.tray.model = Mock()
        self.tray.model.lock = threading.RLock()
        self.tray.model.on_break = False
        self.tray.model.break_minutes_left = 0
        self.tray.model.needs_permissions = False
        self.tray.model.state = TrayState.SYNCING
        self.aw_manager = Mock()
        self.reminder = Mock(spec=ReminderManager)
        self.coordinator = SyncCoordinator(
            config=self.config,
            aw=self.aw,
            bf=self.bf,
            queue=self.queue,
            sync_engine=self.sync_engine,
            tray=self.tray,
            aw_manager=self.aw_manager,
            reminder_manager=self.reminder,
        )
        self.coordinator.scheduler = Mock()
        self.coordinator.scheduler.running = True

    def test_private_auto_end_fires_callback_past_cap(self):
        self.config.reminders.private_auto_end_hours = 4.0
        self.sync_engine.is_private = True
        self.reminder.private_elapsed_seconds.return_value = 4 * 3600 + 1
        cb = Mock()
        self.coordinator.on_private_auto_end = cb

        self.coordinator._check_private_auto_end()

        cb.assert_called_once()
        assert cb.call_args[0][0] >= 4 * 3600

    def test_private_auto_end_below_cap_does_nothing(self):
        self.config.reminders.private_auto_end_hours = 4.0
        self.sync_engine.is_private = True
        self.reminder.private_elapsed_seconds.return_value = 3 * 3600
        cb = Mock()
        self.coordinator.on_private_auto_end = cb

        self.coordinator._check_private_auto_end()

        cb.assert_not_called()

    def test_private_auto_end_disabled_when_cap_zero(self):
        self.config.reminders.private_auto_end_hours = 0
        self.sync_engine.is_private = True
        self.reminder.private_elapsed_seconds.return_value = 99 * 3600
        cb = Mock()
        self.coordinator.on_private_auto_end = cb

        self.coordinator._check_private_auto_end()

        cb.assert_not_called()

    def test_private_auto_end_skipped_when_not_private(self):
        self.config.reminders.private_auto_end_hours = 4.0
        self.sync_engine.is_private = False
        cb = Mock()
        self.coordinator.on_private_auto_end = cb

        self.coordinator._check_private_auto_end()

        cb.assert_not_called()
        self.reminder.private_elapsed_seconds.assert_not_called()

    @patch("src.main.send_notification")
    def test_auto_end_private_tears_down_and_notifies(self, mock_notify):
        """The app-side teardown (wired as on_private_auto_end) ends private the
        same way the user toggle does, with a 'we turned it off' notice."""
        from src.main import BetterFlowApp
        app = Mock()
        BetterFlowApp._auto_end_private(app, 5 * 3600)
        app._set_user_paused.assert_called_once_with(False)
        app.sync_engine.set_private_mode.assert_called_once_with(False)
        app.sync_engine.resume.assert_called_once()
        app.reminder_manager.on_private_ended.assert_called_once()
        app.reminder_manager.on_tracking_started.assert_called_once()
        mock_notify.assert_called_once()
        assert "auto-ended" in mock_notify.call_args[0][0].lower()

    def test_start_break_sets_flag(self):
        """start_break sets _on_break."""
        self.coordinator.start_break()
        assert self.coordinator.break_mgr._on_break is True
        self.sync_engine.pause.assert_called_once()

    def test_double_start_break_is_idempotent(self):
        """Second start_break is a no-op."""
        self.coordinator.start_break()
        self.coordinator.start_break()
        self.sync_engine.pause.assert_called_once()

    def test_end_break_clears_flag(self):
        """end_break clears _on_break."""
        self.coordinator.break_mgr._on_break = True
        self.coordinator.break_mgr._break_start = datetime.now(timezone.utc) - timedelta(seconds=120)
        self.coordinator.break_mgr._pre_break_paused = False
        self.coordinator.break_mgr._pre_break_private = False
        self.coordinator.end_break()
        assert self.coordinator.break_mgr._on_break is False
        self.sync_engine.resume.assert_called_once()

    def test_end_break_restores_paused_state(self):
        """end_break should not resume if app was paused before break."""
        self.coordinator.break_mgr._on_break = True
        self.coordinator.break_mgr._break_start = datetime.now(timezone.utc) - timedelta(seconds=120)
        self.coordinator.break_mgr._pre_break_paused = True
        self.coordinator.break_mgr._pre_break_private = False

        self.coordinator.end_break()

        self.sync_engine.resume.assert_not_called()
        self.tray.set_state.assert_called_with(TrayState.PAUSED)

    def test_end_break_restores_private_state(self):
        """end_break should resume the engine then re-enable private mode.

        start_break() pauses the engine before enabling private mode, so
        end_break() must un-pause it (resume) before re-applying private mode.
        This ensures both flags are consistent: paused=False, private=True.
        """
        self.coordinator.break_mgr._on_break = True
        self.coordinator.break_mgr._break_start = datetime.now(timezone.utc) - timedelta(seconds=120)
        self.coordinator.break_mgr._pre_break_paused = False
        self.coordinator.break_mgr._pre_break_private = True

        self.coordinator.end_break()

        self.sync_engine.resume.assert_called_once()
        self.sync_engine.set_private_mode.assert_called_once_with(True)
        self.tray.set_state.assert_called_with(TrayState.PRIVATE)

    def test_end_break_silent_no_notification(self):
        """end_break(silent=True) should not send notification."""
        self.coordinator.break_mgr._on_break = True
        self.coordinator.break_mgr._break_start = datetime.now(timezone.utc) - timedelta(seconds=120)
        self.coordinator.break_mgr._pre_break_paused = False
        self.coordinator.break_mgr._pre_break_private = False

        with patch("src.break_manager.send_notification") as mock_notify:
            self.coordinator.end_break(silent=True)
            mock_notify.assert_not_called()

    def test_end_break_too_soon_rejected_without_force(self):
        """end_break() is a no-op when called < 60s after start (scheduler race guard)."""
        self.coordinator.break_mgr._on_break = True
        self.coordinator.break_mgr._break_start = datetime.now(timezone.utc) - timedelta(seconds=10)
        self.coordinator.break_mgr._pre_break_paused = False
        self.coordinator.break_mgr._pre_break_private = False

        self.coordinator.end_break(force=False)

        assert self.coordinator.break_mgr._on_break is True
        self.sync_engine.resume.assert_not_called()

    def test_end_break_force_bypasses_60s_guard(self):
        """end_break(force=True) succeeds even when < 60s have elapsed."""
        self.coordinator.break_mgr._on_break = True
        self.coordinator.break_mgr._break_start = datetime.now(timezone.utc) - timedelta(seconds=5)
        self.coordinator.break_mgr._pre_break_paused = False
        self.coordinator.break_mgr._pre_break_private = False

        self.coordinator.end_break(force=True)

        assert self.coordinator.break_mgr._on_break is False
        self.sync_engine.resume.assert_called_once()

    def test_end_break_no_timestamp_without_force_rejected(self):
        """end_break() is a no-op when _break_start is None and force=False."""
        self.coordinator.break_mgr._on_break = True
        self.coordinator.break_mgr._break_start = None
        self.coordinator.break_mgr._pre_break_paused = False
        self.coordinator.break_mgr._pre_break_private = False

        self.coordinator.end_break(force=False)

        assert self.coordinator.break_mgr._on_break is True
        self.sync_engine.resume.assert_not_called()


class TestConfigDefaultCategories:
    """Tests for default_categories save/load round-trip."""

    def test_save_load_roundtrip_restores_default_categories(self, tmp_path, monkeypatch):
        """default_categories must come from dataclass defaults, not config.json."""
        monkeypatch.setattr(Config, "get_config_dir", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(Config, "get_config_file", classmethod(lambda cls: tmp_path / "config.json"))
        config = Config()
        assert "Claude" in config.privacy.default_categories
        config.save()
        loaded = Config.load()
        assert "Claude" in loaded.privacy.default_categories


def _coord_for_bucket_watchdog():
    """A SyncCoordinator with just the attrs _handle_aw_bucket_failure touches."""
    c = SyncCoordinator.__new__(SyncCoordinator)
    c._aw_buckets_failed_streak = 0
    c._AW_UNREACHABLE_ERROR_THRESHOLD = 2
    c.aw_manager = Mock()
    c.aw_manager.is_managing = True
    c.tray = Mock()
    return c


def test_bucket_failure_force_restarts_only_at_threshold():
    """A single bucket-fetch failure is debounced; force_restart fires once the
    streak crosses the threshold (the hung-server recovery is_running() missed)."""
    c = _coord_for_bucket_watchdog()

    c._handle_aw_bucket_failure()  # 1/2 — below threshold
    c.aw_manager.force_restart.assert_not_called()

    c._handle_aw_bucket_failure()  # 2/2 — escalate
    c.aw_manager.force_restart.assert_called_once()
    assert c.tray.set_state.call_args[0][0] == TrayState.ERROR


def test_bucket_failure_streak_resets_clear_no_restart():
    """A reset streak (e.g. after a healthy sync) needs the full threshold again
    before another force_restart — no carry-over."""
    c = _coord_for_bucket_watchdog()
    c._aw_buckets_failed_streak = 0
    c._handle_aw_bucket_failure()  # 1/2
    c.aw_manager.force_restart.assert_not_called()


def test_error_context_omits_user_email_and_name():
    """Error reports go to the cross-tenant BetterQA ops ingest, so they must NOT
    carry the end user's email/name. device_id (maps back server-side) + role are
    enough for routing."""
    from src.main import BetterFlowApp
    app = BetterFlowApp.__new__(BetterFlowApp)
    app.tray = MagicMock()
    app.tray.model.user_email = "someone@betterqa.co"
    app.tray.model.user_name = "Some One"
    app.tray.model.user_role = "B2E"
    app.bf = MagicMock()
    app.bf.device_id = 42

    ctx = app._error_context()

    assert "user_email" not in ctx
    assert "user_name" not in ctx
    assert ctx["user_role"] == "B2E"
    assert ctx["device_id"] == 42
