"""Tests for call/meeting detection."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from src.config import CallDetectionSettings, Config
from src.sync.call_detector import CallDetector, CallEvent, _match_browser, _match_native


def _ts(minutes_offset: float = 0) -> datetime:
    """Helper: return a UTC timestamp offset by minutes from a fixed base."""
    base = datetime(2026, 3, 16, 10, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(minutes=minutes_offset)


class TestPatternMatching:
    """Test individual pattern matching functions."""

    def test_zoom_native_meeting(self):
        assert _match_native("zoom.us", "Zoom Meeting") == "Zoom"

    def test_zoom_native_webinar(self):
        assert _match_native("zoom.us", "Zoom Webinar") == "Zoom"

    def test_zoom_native_bare_meeting_id_does_not_match(self):
        # A bare 9–11 digit number in a Zoom window (ticket / phone / timestamp)
        # must NOT be read as a meeting ID — the old \d{9,11} fallback
        # over-credited (dropped, mirrors PHP CallEventDetector E3). Real Zoom
        # calls match the "Zoom Meeting"/"Zoom Webinar" title or the zoom.us/j/
        # browser URL instead.
        assert _match_native("zoom.us", "123456789") is None
        assert _match_native("Zoom", "Ticket 2024123456") is None

    def test_zoom_native_no_match(self):
        assert _match_native("zoom.us", "Zoom - Home") is None

    def test_teams_native_meeting(self):
        assert _match_native("Microsoft Teams", "Meeting with John") == "Microsoft Teams"

    def test_teams_native_call(self):
        assert _match_native("Microsoft Teams", "Call with Sarah") == "Microsoft Teams"

    def test_teams_native_in_call(self):
        assert _match_native("Microsoft Teams", "In a call") == "Microsoft Teams"

    def test_teams_native_chat_no_match(self):
        assert _match_native("Microsoft Teams", "Chat - General") is None

    def test_teams_bare_app_name_matches_on_word_boundary(self):
        # The classic-Teams process can report as a standalone "Teams".
        assert _match_native("Teams", "Meeting with John") == "Microsoft Teams"

    def test_teams_alias_does_not_bleed_into_unrelated_apps(self):
        # "teams" must match as a whole word, not a substring — TeamSpeak /
        # TeamViewer contain "teams"/"team" but are not Microsoft Teams. (Even
        # title-gated, a tighter app match is correct.)
        assert _match_native("TeamSpeak", "Meeting with John") is None
        assert _match_native("TeamViewer", "Call with Sarah") is None

    def test_zoom_alias_does_not_bleed_into_unrelated_apps(self):
        # "zoom" matches "Zoom" but not "ZoomText" (a screen magnifier).
        assert _match_native("ZoomText", "Zoom Meeting") is None

    def test_webex_still_matches_compound_process_name(self):
        # "webex" stays a SUBSTRING match (not word-boundary) so the compound
        # Windows process "CiscoWebexStart" is still caught — the word-boundary
        # tightening is scoped to teams/zoom only.
        assert _match_native("CiscoWebexStart", "Meeting - Sprint Review") == "Webex"

    def test_slack_huddle(self):
        assert _match_native("Slack", "Huddle in #engineering") == "Slack"

    def test_slack_chat_no_match(self):
        assert _match_native("Slack", "Slack - #general") is None

    def test_discord_voice(self):
        assert _match_native("Discord", "Voice Connected - gaming") == "Discord"

    def test_discord_text_no_match(self):
        assert _match_native("Discord", "#general - Discord") is None

    def test_facetime_any_window(self):
        assert _match_native("FaceTime", "anything here") == "FaceTime"

    def test_facetime_alias_matches_on_word_boundary(self):
        # FaceTime has NO title gate (any window counts), so its alias must match
        # on a word boundary — a helper process like "FaceTimeHelper" must not be
        # read as an active call.
        assert _match_native("FaceTimeHelper", "anything here") is None

    def test_webex_meeting(self):
        assert _match_native("Webex", "Meeting - Sprint Review") == "Webex"

    def test_unrelated_app(self):
        assert _match_native("Visual Studio Code", "main.py") is None

    def test_google_meet_browser(self):
        assert _match_browser("https://meet.google.com/abc-defg-hij") == "Google Meet"

    def test_google_meet_landing_no_match(self):
        assert _match_browser("https://meet.google.com/") is None

    def test_teams_browser_meeting(self):
        assert _match_browser("https://teams.microsoft.com/v2/#/meeting/123") == "Microsoft Teams"

    def test_zoom_browser_join(self):
        assert _match_browser("https://zoom.us/j/123456789") == "Zoom"

    def test_zoom_browser_web_client(self):
        assert _match_browser("https://zoom.us/wc/123456789") == "Zoom"

    def test_slack_browser_huddle(self):
        assert _match_browser("https://app.slack.com/huddle/T12345/C67890") == "Slack"

    def test_unrelated_url(self):
        assert _match_browser("https://github.com/pulls") is None

    def test_empty_url(self):
        assert _match_browser("") is None

    def test_none_url(self):
        assert _match_browser(None) is None


class TestCallDetector:
    """Test the CallDetector state machine."""

    def test_zoom_call_detected_and_ended(self):
        """Full call lifecycle: start, continue, end."""
        detector = CallDetector(min_duration=10)

        # Start a Zoom call
        result = detector.process_event("zoom.us", "Zoom Meeting", None, _ts(0), 5.0)
        assert result is None  # Call just started, no event yet

        # Continue in call (2 more events)
        result = detector.process_event("zoom.us", "Zoom Meeting", None, _ts(1), 60.0)
        assert result is None

        result = detector.process_event("zoom.us", "Zoom Meeting", None, _ts(2), 60.0)
        assert result is None

        # Switch away for longer than grace period (>30s)
        result = detector.process_event("Visual Studio Code", "main.py", None, _ts(3), 5.0)
        assert result is None  # Grace period starts

        result = detector.process_event("Visual Studio Code", "main.py", None, _ts(4), 60.0)
        assert result is not None  # Grace expired -> call ended

        assert result.app == "Zoom"
        assert result.call_type == "native"
        assert result.start == _ts(0)
        assert result.end == _ts(3)  # End = when user left call app
        assert result.duration > 0

    def test_google_meet_browser_detected(self):
        """Browser-based Google Meet detection via URL."""
        detector = CallDetector(min_duration=10)

        result = detector.process_event(
            "Google Chrome", "Meeting - Google Meet",
            "https://meet.google.com/abc-defg-hij", _ts(0), 5.0
        )
        assert result is None

        result = detector.process_event(
            "Google Chrome", "Meeting - Google Meet",
            "https://meet.google.com/abc-defg-hij", _ts(5), 300.0
        )
        assert result is None

        # End: navigate to different page
        result = detector.process_event(
            "Google Chrome", "GitHub", "https://github.com", _ts(10), 5.0
        )
        assert result is None  # Grace period

        result = detector.process_event(
            "Google Chrome", "GitHub", "https://github.com", _ts(11), 60.0
        )
        assert result is not None

        assert result.app == "Google Meet"
        assert result.call_type == "browser"

    def test_short_open_filtered(self):
        """Opens Zoom briefly (<30s min_duration) should be discarded."""
        detector = CallDetector(min_duration=30)

        result = detector.process_event("zoom.us", "Zoom Meeting", None, _ts(0), 5.0)
        assert result is None

        # Switch away after only 15 seconds total
        result = detector.process_event("Safari", "Google", None, _ts(0.25), 5.0)
        assert result is None  # Grace starts

        result = detector.process_event("Safari", "Google", None, _ts(1.25), 60.0)
        # Grace expired, call ended but too short
        assert result is None  # Filtered by min_duration

    def test_grace_period_prevents_splitting(self):
        """Brief app switch during a call should not split it."""
        detector = CallDetector(min_duration=10, grace_period=30.0)

        # Start Zoom call
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(0), 5.0)
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(1), 60.0)

        # Brief switch to Slack (< 30s grace period, only 15 seconds away)
        detector.process_event("Slack", "Slack - #general", None, _ts(2), 5.0)

        # Return to Zoom within grace period (15 seconds later)
        result = detector.process_event("zoom.us", "Zoom Meeting", None, _ts(2.25), 60.0)
        assert result is None  # Still in the same call

        # End the call for real
        detector.process_event("Safari", "Google", None, _ts(10), 5.0)
        result = detector.process_event("Safari", "Google", None, _ts(11), 60.0)
        assert result is not None

        # Should be one continuous call, not two
        assert result.app == "Zoom"
        assert result.start == _ts(0)

    def test_flush_emits_ongoing_call(self):
        """flush() should emit the current call at sync boundary."""
        detector = CallDetector(min_duration=10)

        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(0), 5.0)
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(5), 300.0)

        result = detector.flush()
        assert result is not None
        assert result.app == "Zoom"
        assert result.start == _ts(0)

        # After flush, state is reset
        result2 = detector.flush()
        assert result2 is None

    def test_flush_no_active_call(self):
        """flush() with no active call returns None."""
        detector = CallDetector()
        assert detector.flush() is None

    def test_different_call_app_ends_previous(self):
        """Switching from Zoom to Teams should end the Zoom call."""
        detector = CallDetector(min_duration=10)

        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(0), 5.0)
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(5), 300.0)

        # Switch to Teams call
        result = detector.process_event(
            "Microsoft Teams", "Meeting with Bob", None, _ts(10), 5.0
        )
        assert result is not None
        assert result.app == "Zoom"

        # Teams call is now active; add more events so it exceeds min_duration
        detector.process_event(
            "Microsoft Teams", "Meeting with Bob", None, _ts(15), 300.0
        )
        result = detector.flush()
        assert result is not None
        assert result.app == "Microsoft Teams"

    def test_case_insensitive_app_match(self):
        """App matching should be case-insensitive."""
        detector = CallDetector(min_duration=0)

        detector.process_event("Zoom.US", "Zoom Meeting", None, _ts(0), 5.0)
        result = detector.flush()
        assert result is not None
        assert result.app == "Zoom"

    def test_no_events_processed(self):
        """Detector handles empty event stream gracefully."""
        detector = CallDetector()
        assert detector.flush() is None


class TestCallDetectorDisabled:
    """Test that disabled config produces no call detector."""

    def test_disabled_config(self):
        """When call_detection.enabled is False, SyncEngine should not
        create a call detector (tested at integration level in test_sync_engine)."""
        config = CallDetectionSettings(enabled=False)
        assert config.enabled is False

    def test_min_duration_zero_allows_short_calls(self):
        """min_duration=0 should allow even 1-second calls."""
        detector = CallDetector(min_duration=0)
        detector.process_event("FaceTime", "FaceTime call", None, _ts(0), 1.0)

        # End the call
        detector.process_event("Safari", "Google", None, _ts(0.5), 5.0)
        result = detector.process_event("Safari", "Google", None, _ts(1.5), 60.0)
        assert result is not None
        assert result.app == "FaceTime"
        assert result.duration > 0


class TestSyncEngineCallIntegration:
    """Test CallDetector integration in SyncEngine."""

    def setup_method(self):
        from src.sync.activity_analyzer import ActivityAnalyzer
        from src.sync.daily_time_tracker import DailyTimeTracker
        from src.sync.sync_engine import SyncEngine

        self.aw = Mock()
        self.bf = Mock()
        self.queue = Mock()
        self.queue.get_checkpoint.return_value = None
        self.queue.get_all_categories.return_value = {}

        self.config = Config()
        self.config.call_detection.enabled = True
        self.config.call_detection.min_call_duration = 10

        self.activity_analyzer = Mock(spec=ActivityAnalyzer)
        self.activity_analyzer.get_activity_state.return_value = "active"
        self.activity_analyzer.get_raw_metrics.return_value = Mock(
            to_dict=lambda: {"presses": 0, "clicks": 0, "scrolls": 0, "window_changes": 0}
        )
        self.activity_analyzer.get_fraud_assessment.return_value = Mock(
            score=0, signals=[], extra_metrics={"unique_apps": 0, "keystroke_variance": None},
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

    def test_call_detector_initialized_when_enabled(self):
        assert self.engine._call_detector is not None

    def test_call_detector_credit_cap_plumbed_from_config(self):
        from src.sync.sync_engine import SyncEngine
        config = Config()
        config.call_detection.enabled = True
        config.call_detection.max_credit_minutes = 60

        engine = SyncEngine(
            aw=self.aw, bf=self.bf, queue=self.queue,
            config=config,
            activity_analyzer=self.activity_analyzer,
            time_tracker=self.time_tracker,
        )
        assert engine._call_detector._max_credit_seconds == 3600.0

    def test_call_detector_none_when_disabled(self):
        from src.sync.sync_engine import SyncEngine
        config = Config()
        config.call_detection.enabled = False

        engine = SyncEngine(
            aw=self.aw, bf=self.bf, queue=self.queue,
            config=config,
            activity_analyzer=self.activity_analyzer,
            time_tracker=self.time_tracker,
        )
        assert engine._call_detector is None

    def test_make_call_bf_event(self):
        ce = CallEvent(
            app="Zoom",
            start=_ts(0),
            end=_ts(30),
            duration=1800.0,
            call_type="native",
        )
        result = self.engine._make_call_bf_event(ce)
        assert result["bucket_type"] == "call"
        assert result["duration"] == 1800.0
        assert result["data"]["app"] == "Zoom"
        assert result["data"]["call_type"] == "native"
        assert result["data"]["status"] == "completed"
        assert "bf-call-detector_" in result["bucket_id"]

    def test_make_call_bf_event_with_project(self):
        self.engine.set_current_project({"id": 42, "name": "Test"})
        ce = CallEvent(
            app="Google Meet", start=_ts(0), end=_ts(30),
            duration=1800.0, call_type="browser",
        )
        result = self.engine._make_call_bf_event(ce)
        assert result["project_id"] == 42


def test_is_in_call_reflects_active_call_state():
    """is_in_call() is True from call start until the call ends."""
    from datetime import datetime, timezone
    det = CallDetector(min_duration=1)
    t0 = datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
    assert det.is_in_call() is False
    # Enter a Teams meeting.
    det.process_event("Microsoft Teams", "Meeting with Sachi", None, t0, 60.0)
    assert det.is_in_call() is True
    # Still in the call on a later same-call event.
    det.process_event("Microsoft Teams", "Meeting with Sachi", None,
                      t0.replace(minute=30), 60.0)
    assert det.is_in_call() is True
    # Flushing ends the call.
    det.flush()
    assert det.is_in_call() is False


class TestGetLastActiveAt:
    """Activity-source contract: call engagement folded into the uploaded AFK
    stream (AfkSource) so a hands-off meeting bills as active, not idle."""

    def _start_zoom_call(self, detector, at_minutes=0.0):
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(at_minutes), 60)

    def test_none_when_no_call_active(self):
        detector = CallDetector()
        assert detector.get_last_active_at(None, _ts(0)) is None

    def test_tracks_now_while_evidence_fresh(self):
        # The starting event covers [_ts(0), _ts(1)); at now=_ts(2) evidence is
        # well within the staleness allowance → credit tracks now.
        detector = CallDetector()
        self._start_zoom_call(detector)
        now = _ts(2)
        assert detector.get_last_active_at(_ts(-60), now) == now

    def test_credit_freezes_when_window_evidence_goes_stale(self):
        # Hung window watcher: nothing confirms the call after its last event
        # (end _ts(1)) → credit must freeze a staleness-allowance later, NOT
        # keep tracking now for the full credit cap on zero evidence.
        detector = CallDetector()
        self._start_zoom_call(detector)
        got = detector.get_last_active_at(None, _ts(30))
        assert got == _ts(1) + timedelta(seconds=180)

    def test_cap_anchored_on_real_input_not_call_start(self):
        # Ending one call and starting another must not re-arm the cap: the
        # anchor is the REAL input instant AfkSource passes in, so chained
        # sessions share one bounded window.
        detector = CallDetector(max_credit_seconds=3600.0)
        self._start_zoom_call(detector)
        # Switch to a different call app at _ts(50): ends Zoom, starts Slack.
        detector.process_event("Slack", "Huddle", None, _ts(50), 60)
        detector.process_event("Slack", "Huddle", None, _ts(54), 60)
        # Real input was at _ts(-10) → cap end _ts(50), regardless of the
        # fresh Slack call (whose own start would re-arm to _ts(110)).
        got = detector.get_last_active_at(_ts(-10), _ts(55))
        assert got == _ts(50)

    def test_credit_freezes_at_end_after_call_ends(self):
        # After a call ends the user WAS engaged until its end — credit must
        # freeze there (not vanish, not keep tracking now). This also bridges
        # the AW-outage/shutdown flush: the AFK sample taken next cycle still
        # sees the engagement up to the flushed call's end. The last event
        # covers [_ts(20), _ts(21)) so the call end is _ts(21).
        detector = CallDetector()
        self._start_zoom_call(detector)
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(20), 60)
        detector.flush()
        assert detector.get_last_active_at(None, _ts(30)) == _ts(21)

    def test_ended_call_credit_respects_cap(self):
        detector = CallDetector(max_credit_seconds=600.0)  # 10min cap
        self._start_zoom_call(detector)
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(20), 60)
        detector.flush()
        # No real-input anchor → falls back to call start _ts(0) + 10min.
        assert detector.get_last_active_at(None, _ts(30)) == _ts(10)

    def test_short_discarded_call_still_credits_engagement(self):
        # min_duration discards the EVENT, but the engagement was real — the
        # credit memory must still cover it (up to the last event's end).
        detector = CallDetector(min_duration=300)
        self._start_zoom_call(detector)
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(1), 60)
        assert detector.flush() is None  # too short: no event emitted
        assert detector.get_last_active_at(None, _ts(5)) == _ts(2)

    def test_grace_window_credits_only_up_to_leave_instant(self):
        # User switched to a non-call window at t=10 — they produced real input
        # to do that, so call credit must stop there, not keep tracking `now`.
        detector = CallDetector(grace_period=30.0)
        self._start_zoom_call(detector)
        detector.process_event("Finder", "Documents", None, _ts(10), 5)
        assert detector.get_last_active_at(None, _ts(10.2)) == _ts(10)

    def test_credit_capped_past_max_credit_seconds(self):
        # A stuck call title must not keep the AFK stream not-afk forever:
        # with no real-input anchor, credit freezes at call_start + max_credit
        # even while the call keeps being confirmed by fresh events.
        detector = CallDetector(max_credit_seconds=3600.0)  # 1h cap
        self._start_zoom_call(detector)
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(60), 60)
        assert detector.get_last_active_at(None, _ts(61)) == _ts(60)

    def test_is_in_call_bounded_by_credit_cap(self):
        # The LOCAL idle guard must not outlive the billing cap: a wedged
        # call-matching title would otherwise suppress the idle pause forever
        # while the server bills idle.
        detector = CallDetector(max_credit_seconds=3600.0)
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(0), 60)
        assert detector.is_in_call()
        # Heartbeat grows the confirmed span past the cap.
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(0), 4000)
        assert detector.is_in_call() is False

    def test_never_returns_future_instant(self):
        detector = CallDetector()
        self._start_zoom_call(detector)
        now = _ts(5)
        result = detector.get_last_active_at(None, now)
        assert result is not None and result <= now

    def test_afk_source_folds_call_credit_into_samples(self):
        # End-to-end with AfkSource: a call with zero keyboard/mouse input must
        # keep the reconstructed (uploaded) AFK stream not-afk for its duration.
        from src.sync.afk_source import AfkSource

        detector = CallDetector()
        self._start_zoom_call(detector)

        # OS idle clock: last real input was at t=0 and only ages from there.
        base = _ts(0)
        clock_now = {"now": base}
        afk = AfkSource(
            afk_timeout_seconds=600.0,
            hostname="testhost",
            activity_sources=[detector],
            idle_clock=lambda: (clock_now["now"] - base).total_seconds(),
        )
        # Sample every minute across a 49-minute hands-off meeting. The window
        # watcher heartbeats one growing event per unchanged window (same
        # timestamp, growing duration) — the detector must keep treating that
        # as fresh evidence.
        for m in range(0, 50):
            clock_now["now"] = _ts(m)
            if m > 0:
                detector.process_event(
                    "zoom.us", "Zoom Meeting", None, _ts(0), m * 60.0
                )
            afk.record_sample(_ts(m))

        events = afk.build_afk_events(_ts(0), _ts(49))
        statuses = {e["data"]["status"] for e in events}
        assert statuses == {"not-afk"}, events
        assert sum(e["duration"] for e in events) == 49 * 60


class TestSnapshot:
    """snapshot(): live emission of an ongoing call without ending it."""

    def test_none_when_no_call(self):
        assert CallDetector().snapshot() is None

    def test_none_while_below_min_duration(self):
        detector = CallDetector(min_duration=30)
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(0), 10)
        assert detector.snapshot() is None

    def test_emits_growing_span_with_stable_identity(self):
        detector = CallDetector(min_duration=30)
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(0), 60)
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(2), 60)
        first = detector.snapshot()
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(5), 60)
        second = detector.snapshot()

        # Same (app, start) → _make_call_bf_event derives the same id, so the
        # server upserts one growing row instead of per-cycle fragments.
        assert (first.app, first.start) == (second.app, second.start)
        assert second.duration > first.duration
        # Snapshot must NOT end the call.
        assert detector.is_in_call()

    def test_snapshot_during_grace_ends_at_leave_instant(self):
        detector = CallDetector(min_duration=30, grace_period=60.0)
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(0), 60)
        detector.process_event("Finder", "Documents", None, _ts(5), 5)
        snap = detector.snapshot()
        assert snap.end == _ts(5)


class TestWindowsNativeAppNames:
    """Native call apps report different process names on Windows than macOS.

    macOS: "zoom.us" / "Microsoft Teams". Windows: "Zoom" / "ms-teams". The
    title gate is unchanged, so widening the app aliases cannot create false
    positives — a non-call window still won't match.
    """

    def test_zoom_windows_process_name(self):
        assert _match_native("Zoom", "Zoom Meeting") == "Zoom"

    def test_teams_windows_new_process_name(self):
        assert _match_native("ms-teams", "Meeting with John") == "Microsoft Teams"
        assert _match_native("MSTeams", "In a call") == "Microsoft Teams"

    def test_teams_bare_name_in_call(self):
        assert _match_native("Teams", "Call with Sarah") == "Microsoft Teams"

    def test_teams_windows_chat_still_no_match(self):
        # Title gate must still reject a non-call Teams window.
        assert _match_native("ms-teams", "Chat | Microsoft Teams") is None

    def test_zoom_windows_home_still_no_match(self):
        assert _match_native("Zoom", "Zoom - Home") is None


class TestCallPersistsAcrossSyncCycles:
    """A meeting must survive the sync boundary: snapshot-not-flush keeps
    is_in_call() True between cycles (so the idle suppression actually works)
    and uploads one growing call row instead of per-cycle fragments."""

    def _build_engine(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from src.sync.queue import OfflineQueue
        from src.sync.sync_engine import SyncEngine

        tmp = Path(tempfile.mkdtemp())
        cfg = Config()
        cfg.working_hours.known = True  # capture is fail-closed on unknown
        engine = SyncEngine(
            aw=Mock(),
            bf=Mock(),
            queue=OfflineQueue(db_path=tmp / "q.db", max_size=1000),
            config=cfg,
            time_tracker=Mock(),
        )
        engine._config_fetched = True
        engine._backlog_reconciled = True
        engine.aw.is_running.return_value = True
        for getter in ("get_window_buckets", "get_web_buckets",
                       "get_afk_buckets", "get_input_buckets"):
            getattr(engine.aw, getter).return_value = []
        engine.bf.is_reachable.return_value = True
        engine.bf.send_events.return_value = SimpleNamespace(
            success=True, events_synced=1, accepted_ids=[], error=None
        )
        return engine

    def _sent_call_events(self, engine):
        return [
            ev
            for call in engine.bf.send_events.call_args_list
            for ev in call.args[0]
            if ev.get("bucket_type") == "call"
        ]

    def test_call_stays_open_and_snapshots_upsert_one_row(self):
        engine = self._build_engine()
        t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
        engine._call_detector.process_event("Slack", "Huddle", None, t0, 60)
        # Heartbeated growing event: end ≈ now-2min (fresh evidence).
        engine._call_detector.process_event("Slack", "Huddle", None, t0, 8 * 60)

        engine.sync()
        assert engine.is_in_call(), (
            "sync boundary must NOT end an ongoing call anymore"
        )
        engine._call_detector.process_event("Slack", "Huddle", None, t0, 9 * 60)
        engine.sync()
        assert engine.is_in_call()

        snaps = self._sent_call_events(engine)
        assert len(snaps) == 2, "one live snapshot per cycle"
        assert snaps[0]["id"] == snaps[1]["id"], (
            "stable id → the server upserts one growing row per meeting"
        )
        assert snaps[1]["duration"] > snaps[0]["duration"]

    def test_shutdown_closes_open_call_and_sends_final_event(self):
        # Shutdown runs on logout: the final span is SENT under the owner's
        # token (queue only as fallback — the queue is account-agnostic and a
        # leftover row would deliver under the next login on a shared machine).
        engine = self._build_engine()
        t0 = datetime.now(timezone.utc) - timedelta(minutes=2)
        engine._call_detector.process_event("Slack", "Huddle", None, t0, 60)
        engine.shutdown()
        assert engine.is_in_call() is False
        finals = [
            ev for ev in self._sent_call_events(engine)
            if ev["data"]["status"] == "completed"
        ]
        assert len(finals) == 1, "final call span sent at shutdown"
        assert engine.queue.is_empty()


class TestCaptureBoundariesCloseSessions:
    """Open call/mic sessions must not bridge not-recorded periods (private
    time, manual pause, working-hours suppression) into one uploaded span."""

    def _engine_in_call(self):
        harness = TestCallPersistsAcrossSyncCycles()
        engine = harness._build_engine()
        t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
        engine._call_detector.process_event("zoom.us", "Zoom Meeting", None, t0, 60)
        # Heartbeat: end ≈ now-1min → fresh evidence for is_in_call().
        engine._call_detector.process_event("zoom.us", "Zoom Meeting", None, t0, 9 * 60)
        assert engine.is_in_call()
        return engine, t0

    def _queued_completed_calls(self, engine):
        rows = engine.queue.dequeue(100)
        return [
            q.event_data
            for q in rows
            if q.event_data.get("bucket_type") == "call"
            and q.event_data["data"]["status"] == "completed"
        ]

    def test_private_time_closes_call_at_boundary(self):
        engine, t0 = self._engine_in_call()
        engine.set_private_mode(True)
        assert engine.is_in_call() is False, (
            "a call left open across Private Time would later upload one span "
            "covering the whole private period"
        )
        finals = self._queued_completed_calls(engine)
        assert len(finals) == 1
        # The queued span ends at the last pre-private evidence, ~14:11.
        assert finals[0]["duration"] <= 11 * 60 + 1

    def test_pause_closes_call_at_boundary(self):
        engine, _ = self._engine_in_call()
        engine.pause()
        assert engine.is_in_call() is False
        assert len(self._queued_completed_calls(engine)) == 1

    def test_capture_suppression_closes_call_at_boundary(self):
        from types import SimpleNamespace

        engine, _ = self._engine_in_call()
        engine.config.working_hours = SimpleNamespace(
            known=True, allows=lambda ts: False
        )
        stats = engine.sync()
        assert stats.capture_suppressed is True
        assert engine.is_in_call() is False, (
            "a call title still matching when capture stops must not stay "
            "open all night and manufacture a span over the suppressed hours"
        )
        # The suppressed branch drains the queue right after the flush, so the
        # final span is already SENT (queued only when offline).
        sent_finals = [
            ev
            for call in engine.bf.send_events.call_args_list
            for ev in call.args[0]
            if ev.get("bucket_type") == "call"
            and ev["data"]["status"] == "completed"
        ]
        assert len(sent_finals) == 1


class TestStaleOngoingSnapshotsNeverReplay:
    def test_process_queue_drops_ongoing_call_rows(self):
        from inspect import signature

        from src.sync.queue import OfflineQueue, is_event_storable

        harness = TestCallPersistsAcrossSyncCycles()
        engine = harness._build_engine()
        # RELATIVE anchor, deliberately. _process_queue() evicts anything past
        # the server's retention window BEFORE batching, so an absolute date
        # here is a time bomb: it silently ages out of the window and the
        # 'completed' event is evicted instead of sent, failing this test on a
        # commit that never changed (it detonated on 2026-07-22, exactly 7d
        # after the 2026-07-15 literal it used to pin).
        t0 = datetime.now(timezone.utc) - timedelta(days=1)
        stale_snapshot = {
            "id": "call_Zoom_1784000000",
            "timestamp": t0.isoformat(),
            "duration": 120.0,
            "bucket_id": "bf-call-detector_h",
            "bucket_type": "call",
            "data": {"app": "Zoom", "call_type": "native", "status": "ongoing"},
        }
        final_event = {
            "id": "call_Zoom_1784000000",
            "timestamp": t0.isoformat(),
            "duration": 600.0,
            "bucket_id": "bf-call-detector_h",
            "bucket_type": "call",
            "data": {"app": "Zoom", "call_type": "native", "status": "completed"},
        }
        # Pin the geometry this test depends on: BOTH events must be inside the
        # retention window, or evict_unstorable() drops them and the assertions
        # below pass/fail for reasons that have nothing to do with replay.
        # Read the window from production's own default so it tracks any change.
        retention_days = (
            signature(OfflineQueue.evict_unstorable)
            .parameters["stale_after_days"]
            .default
        )
        stale_cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        for ev in (stale_snapshot, final_event):
            assert is_event_storable(ev, stale_cutoff=stale_cutoff), (
                "test fixture aged out of the queue's retention window "
                f"({retention_days}d) — anchor the timestamp relative to now, "
                "never to an absolute date"
            )

        engine.queue.enqueue([stale_snapshot, final_event])

        from src.sync.sync_engine import SyncStats
        engine._process_queue(SyncStats())

        sent = [
            ev
            for call in engine.bf.send_events.call_args_list
            for ev in call.args[0]
        ]
        statuses = [ev["data"]["status"] for ev in sent]
        assert "ongoing" not in statuses, (
            "a stale queued 'ongoing' snapshot replayed after the final "
            "'completed' event would upsert the row back to a shorter, "
            "forever-ongoing span"
        )
        assert "completed" in statuses
        assert engine.queue.is_empty(), "stale snapshot removed, not retried"


class TestBoundaryFlushRaceAndForeground:
    def test_paused_cycle_reflushes_call_reopened_by_inflight_sync(self):
        # pause()/set_private_mode() run on the tray thread; a sync cycle
        # already in flight keeps feeding pre-boundary window events AFTER the
        # boundary flush and re-opens the call. Every paused/private cycle
        # must re-flush (idempotent), or the reopened call bridges the whole
        # not-recorded period.
        harness = TestCallPersistsAcrossSyncCycles()
        engine = harness._build_engine()
        t0 = datetime.now(timezone.utc) - timedelta(minutes=1)
        engine.set_private_mode(True)
        # In-flight cycle delivers a pre-boundary call event after the flush:
        engine._call_detector.process_event("zoom.us", "Zoom Meeting", None, t0, 60)
        assert engine.is_in_call(), "precondition: the race re-opened the call"

        engine.sync()  # paused/private early-return branch
        assert engine.is_in_call() is False, (
            "the paused/private branch must re-flush like the suppressed one"
        )

    def test_boundary_flush_closes_foreground_session_too(self):
        from src.sync.foreground_activity import (
            ForegroundActivityDetector,
            ForegroundSample,
        )

        class HotProbe:
            def sample(self):
                return ForegroundSample(pid=1, app="Terminal", cpu_percent=80.0)

        harness = TestCallPersistsAcrossSyncCycles()
        engine = harness._build_engine()
        det = ForegroundActivityDetector(
            hostname="h", probe=HotProbe(), min_session_seconds=30.0
        )
        now = datetime.now(timezone.utc)
        det.observe(now - timedelta(minutes=10), now - timedelta(minutes=10))
        det.observe(now, now)
        engine._foreground_detector = det
        assert det.is_active()

        engine.set_private_mode(True)
        assert det.is_active() is False, (
            "an open dev-session would bridge the private period the same way "
            "a call would — and on Linux it IS the billing path"
        )
        assert not engine.queue.is_empty(), "the pre-boundary span is queued"


class TestEvidenceFreshness:
    """has_fresh_evidence / engine is_in_call: a hung window watcher must not
    suppress the idle pause forever on state nothing confirms."""

    def test_fresh_heartbeat_is_fresh(self):
        detector = CallDetector()
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(0), 10 * 60)
        # Evidence ends _ts(10); asking at _ts(11) is within staleness+grace.
        assert detector.has_fresh_evidence(_ts(11)) is True

    def test_stale_evidence_is_not_fresh(self):
        detector = CallDetector()
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(0), 60)
        # Last evidence ends _ts(1); at _ts(10) the watcher has been silent
        # for 9 minutes — nothing confirms this call anymore.
        assert detector.has_fresh_evidence(_ts(10)) is False

    def test_no_call_is_not_fresh(self):
        assert CallDetector().has_fresh_evidence(_ts(0)) is False

    def test_engine_is_in_call_false_on_stale_evidence(self):
        harness = TestCallPersistsAcrossSyncCycles()
        engine = harness._build_engine()
        stale_t0 = datetime.now(timezone.utc) - timedelta(hours=2)
        engine._call_detector.process_event(
            "zoom.us", "Zoom Meeting", None, stale_t0, 60
        )
        assert engine._call_detector.is_in_call(), (
            "state machine itself still says in-call"
        )
        assert engine.is_in_call() is False, (
            "the engine must not suppress idle on 2h-stale evidence"
        )
