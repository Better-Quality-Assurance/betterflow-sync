"""Tests for microphone-in-use meeting detection (mic_activity.py)."""

from datetime import datetime, timedelta, timezone

from src.config import Config
from src.sync.mic_activity import (
    MicActivityDetector,
    MicSample,
    _matches_conferencing,
    create_mic_detector,
)


def _ts(minutes_offset: float = 0) -> datetime:
    base = datetime(2026, 7, 15, 17, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(minutes=minutes_offset)


class FakeProbe:
    """Scriptable probe: set .current before each observe()."""

    def __init__(self, sample: MicSample | None = None):
        self.current = sample if sample is not None else MicSample(False)

    def sample(self) -> MicSample:
        return self.current


def _detector(probe=None, **kwargs) -> MicActivityDetector:
    defaults = {
        "hostname": "testhost",
        "probe": probe or FakeProbe(),
        "min_session_seconds": 30.0,
        "max_credit_seconds": 14400.0,
        "off_grace_seconds": 90.0,
    }
    defaults.update(kwargs)
    return MicActivityDetector(**defaults)


class TestSessionLifecycle:
    def test_session_opens_when_mic_hot(self):
        probe = FakeProbe(MicSample(True))
        det = _detector(probe)
        assert det.observe(_ts(0)) is None
        assert det.is_active()

    def test_no_session_while_mic_cold(self):
        det = _detector(FakeProbe(MicSample(False)))
        assert det.observe(_ts(0)) is None
        assert not det.is_active()

    def test_unreadable_probe_never_opens_a_session(self):
        det = _detector(FakeProbe(MicSample(None)))
        det.observe(_ts(0))
        assert not det.is_active()

    def test_brief_mic_drop_survives_off_grace(self):
        probe = FakeProbe(MicSample(True))
        det = _detector(probe, off_grace_seconds=90.0)
        det.observe(_ts(0))
        probe.current = MicSample(False)  # one cold sample, 60s later
        assert det.observe(_ts(1)) is None
        assert det.is_active(), "a <90s drop must not split the meeting"
        probe.current = MicSample(True)
        det.observe(_ts(2))
        assert det.is_active()

    def test_session_closes_after_off_grace_at_last_hot_instant(self):
        probe = FakeProbe(MicSample(True))
        det = _detector(probe)
        det.observe(_ts(0))
        det.observe(_ts(49))  # 49-minute meeting
        probe.current = MicSample(False)
        ended = det.observe(_ts(52))  # 3min cold > 90s grace
        assert ended is not None
        assert ended["timestamp"] == _ts(0).isoformat()
        assert ended["duration"] == 49 * 60.0
        assert not det.is_active()

    def test_short_session_discarded_but_not_active_after(self):
        probe = FakeProbe(MicSample(True))
        det = _detector(probe, min_session_seconds=300.0)
        det.observe(_ts(0))
        det.observe(_ts(1))
        probe.current = MicSample(False)
        assert det.observe(_ts(5)) is None  # 60s < 300s: event discarded
        assert not det.is_active()

    def test_probe_exception_does_not_close_session_within_grace(self):
        class BoomProbe:
            def sample(self):
                raise RuntimeError("boom")

        det = _detector(FakeProbe(MicSample(True)))
        det.observe(_ts(0))
        det._probe = BoomProbe()
        assert det.observe(_ts(1)) is None
        assert det.is_active(), "a transient blind read must not split the meeting"

    def test_flush_closes_open_session(self):
        probe = FakeProbe(MicSample(True))
        det = _detector(probe)
        det.observe(_ts(0))
        det.observe(_ts(10))
        ended = det.flush()
        assert ended is not None and ended["duration"] == 600.0
        assert not det.is_active()
        assert det.flush() is None


class TestConferencingGate:
    def test_windows_attribution_conferencing_app_passes(self):
        det = _detector(FakeProbe(MicSample(True, ("Zoom.exe",))))
        det.observe(_ts(0))
        assert det.is_active()

    def test_windows_attribution_non_conferencing_app_blocked(self):
        det = _detector(FakeProbe(MicSample(True, ("Audacity.exe",))))
        det.observe(_ts(0))
        assert not det.is_active(), "a hot mic held by a recorder is not a meeting"

    def test_macos_gate_false_blocks(self):
        det = _detector(FakeProbe(MicSample(True)), conferencing_gate=lambda: False)
        det.observe(_ts(0))
        assert not det.is_active()

    def test_macos_gate_error_fails_closed(self):
        # This signal feeds billing: a gate error must never become an
        # unconditional pass (a reproducible gate crash would be a free
        # bypass). Cost is one delayed cycle — the mic is still hot next time.
        def boom():
            raise RuntimeError("psutil hiccup")

        det = _detector(FakeProbe(MicSample(True)), conferencing_gate=boom)
        det.observe(_ts(0))
        assert not det.is_active()

    def test_attributed_app_used_as_session_label(self):
        probe = FakeProbe(MicSample(True, ("ms-teams.exe",)))
        det = _detector(probe)
        det.observe(_ts(0))
        det.observe(_ts(10))
        assert det.flush()["data"]["app"] == "ms-teams.exe"

    def test_token_matching(self):
        assert _matches_conferencing("zoom.us")
        assert _matches_conferencing("Google Chrome")
        assert not _matches_conferencing("Audacity")


class TestAfkCredit:
    def test_none_when_never_observed(self):
        assert _detector().get_last_active_at(None, _ts(0)) is None

    def test_tracks_now_within_off_grace_of_last_hot(self):
        det = _detector(FakeProbe(MicSample(True)))
        det.observe(_ts(0))
        assert det.get_last_active_at(_ts(-60), _ts(1)) == _ts(1)

    def test_credit_stops_growing_past_off_grace_of_last_hot(self):
        # After the last HOT observation, credit may extend at most the
        # off-grace allowance — an open session must not keep answering `now`
        # indefinitely between observations.
        det = _detector(FakeProbe(MicSample(True)), off_grace_seconds=90.0)
        det.observe(_ts(0))
        assert det.get_last_active_at(None, _ts(30)) == _ts(0) + timedelta(seconds=90)

    def test_force_closes_at_credit_cap_and_latches(self):
        # Past the cap the session earns nothing and its snapshot would grow
        # unboundedly — it must force-close even while the mic stays hot, and
        # LATCH: reopening every cycle would suppress the idle pause for a
        # fresh cap period forever (a wedged mic looping 4h sessions).
        probe = FakeProbe(MicSample(True))
        det = _detector(probe, max_credit_seconds=3600.0)
        det.observe(_ts(0))
        det.observe(_ts(30))
        ended = det.observe(_ts(60))  # 1h since start → cap reached
        assert ended is not None and ended["duration"] == 30 * 60.0
        assert not det.is_active()
        # Still hot → latched, no reopen…
        det.observe(_ts(61))
        assert not det.is_active()
        # …until the mic genuinely goes cold once, then hot again (a real
        # new meeting) reopens.
        probe.current = MicSample(False)
        det.observe(_ts(62))
        probe.current = MicSample(True)
        det.observe(_ts(63))
        assert det.is_active()

    def test_cap_anchored_on_real_input_not_session_start(self):
        # …but the reopened session grants no fresh credit on its own: the cap
        # anchors on the real-input instant AfkSource passes in, so cycling
        # the mic can't re-arm it.
        det = _detector(FakeProbe(MicSample(True)), max_credit_seconds=3600.0)
        det.observe(_ts(0))
        det.observe(_ts(30))
        # Real input was at _ts(-31) → cap end _ts(29), despite the session
        # being open and hot at _ts(30).
        assert det.get_last_active_at(_ts(-31), _ts(30)) == _ts(29)

    def test_credit_freezes_at_session_end_after_close(self):
        probe = FakeProbe(MicSample(True))
        det = _detector(probe)
        det.observe(_ts(0))
        det.observe(_ts(49))
        probe.current = MicSample(False)
        det.observe(_ts(52))  # closes
        assert det.get_last_active_at(None, _ts(120)) == _ts(49)

    def test_discarded_short_session_still_credits(self):
        probe = FakeProbe(MicSample(True))
        det = _detector(probe, min_session_seconds=300.0)
        det.observe(_ts(0))
        det.observe(_ts(1))
        probe.current = MicSample(False)
        det.observe(_ts(5))
        assert det.get_last_active_at(None, _ts(6)) == _ts(1)

    def test_afk_source_folds_mic_credit(self):
        # End-to-end: a background huddle (mic hot, zero input) keeps the
        # reconstructed uploaded AFK stream not-afk.
        from src.sync.afk_source import AfkSource

        probe = FakeProbe(MicSample(True))
        det = _detector(probe)

        base = _ts(0)
        clock_now = {"now": base}
        afk = AfkSource(
            afk_timeout_seconds=600.0,
            hostname="testhost",
            activity_sources=[det],
            idle_clock=lambda: (clock_now["now"] - base).total_seconds(),
        )
        for m in range(0, 50):
            clock_now["now"] = _ts(m)
            det.observe(_ts(m))
            afk.record_sample(_ts(m))

        events = afk.build_afk_events(_ts(0), _ts(49))
        assert {e["data"]["status"] for e in events} == {"not-afk"}


class TestSnapshotAndEventShape:
    def test_snapshot_grows_with_stable_id_and_does_not_close(self):
        probe = FakeProbe(MicSample(True))
        det = _detector(probe)
        det.observe(_ts(0))
        det.observe(_ts(2))
        first = det.snapshot()
        det.observe(_ts(5))
        second = det.snapshot()
        assert first["id"] == second["id"]
        assert second["duration"] > first["duration"]
        assert det.is_active()

    def test_snapshot_none_below_min_duration(self):
        det = _detector(FakeProbe(MicSample(True)))
        det.observe(_ts(0))
        assert det.snapshot() is None

    def test_event_rides_existing_call_shape_and_is_storable(self):
        from src.sync.queue import is_event_storable

        probe = FakeProbe(MicSample(True))
        det = _detector(probe)
        det.observe(_ts(0))
        det.observe(_ts(10))
        ev = det.flush()
        assert ev["bucket_type"] == "call"
        assert ev["bucket_id"] == "bf-call-detector_testhost"
        assert ev["data"]["call_type"] == "mic"
        assert is_event_storable(ev, stale_cutoff=_ts(-60))


class TestFactory:
    def test_disabled_by_mic_signal_flag(self):
        cfg = Config()
        cfg.call_detection.mic_signal = False
        assert create_mic_detector(cfg, "h") is None

    def test_disabled_when_call_detection_disabled(self):
        cfg = Config()
        cfg.call_detection.enabled = False
        assert create_mic_detector(cfg, "h") is None

    def test_cap_and_min_duration_plumbed(self, monkeypatch):
        import src.sync.mic_activity as mod

        # Windows is the supported platform for mic credit (see
        # test_macos_returns_none_until_per_process_taps for why macOS is off).
        monkeypatch.setattr(mod.sys, "platform", "win32")
        monkeypatch.setattr(mod, "WindowsMicProbe", lambda: FakeProbe())
        cfg = Config()
        cfg.call_detection.max_credit_minutes = 60
        cfg.call_detection.min_call_duration = 45
        det = create_mic_detector(cfg, "h")
        assert det is not None
        assert det._max_credit_seconds == 3600.0
        assert det._min_session_seconds == 45.0

    def test_macos_returns_none_until_per_process_taps(self, monkeypatch):
        """macOS mic credit is disabled: the CoreAudio device-level
        'running somewhere' signal can't tell mic capture from playback on a
        combined-device headset, and the conferencing gate is satisfied by any
        running browser process — together an over-billing path (idle media
        playback billed as active). Windows attributes the mic per app and is
        unaffected. Re-enable when per-process CoreAudio taps (macOS 14+) land."""
        import src.sync.mic_activity as mod

        monkeypatch.setattr(mod.sys, "platform", "darwin")
        # Even with a constructible probe, macOS must not build a detector.
        monkeypatch.setattr(mod, "MacosMicProbe", lambda: FakeProbe())
        cfg = Config()  # enabled + mic_signal default True
        assert create_mic_detector(cfg, "h") is None

    def test_unsupported_platform_returns_none(self, monkeypatch):
        import src.sync.mic_activity as mod

        monkeypatch.setattr(mod.sys, "platform", "linux")
        assert create_mic_detector(Config(), "h") is None


class TestSyncEngineMicIntegration:
    """Engine wiring: per-cycle observe/snapshot, outage behavior, shutdown."""

    def _build_engine(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock

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

    def _inject_detector(self, engine, probe, **kwargs):
        defaults = {
            "hostname": "testhost",
            "probe": probe,
            "min_session_seconds": 30.0,
            "max_credit_seconds": 14400.0,
        }
        defaults.update(kwargs)
        engine._mic_detector = MicActivityDetector(**defaults)
        return engine._mic_detector

    def _sent_mic_events(self, engine):
        return [
            ev
            for call in engine.bf.send_events.call_args_list
            for ev in call.args[0]
            if ev.get("data", {}).get("call_type") == "mic"
        ]

    def test_snapshot_uploaded_and_session_persists_across_cycles(self):
        from datetime import datetime, timezone

        engine = self._build_engine()
        probe = FakeProbe(MicSample(True, ("zoom.exe",)))
        det = self._inject_detector(engine, probe)
        # Meeting has been running for 10 minutes when the first cycle fires.
        det.observe(datetime.now(timezone.utc) - timedelta(minutes=10))

        engine.sync()
        assert engine.is_mic_meeting_active()
        engine.sync()
        assert engine.is_mic_meeting_active()

        snaps = self._sent_mic_events(engine)
        assert len(snaps) == 2, "one live mic snapshot per cycle"
        assert snaps[0]["id"] == snaps[1]["id"], "server upserts one growing row"
        assert snaps[1]["duration"] >= snaps[0]["duration"]

    def test_session_closes_during_aw_outage(self):
        from datetime import datetime, timezone

        engine = self._build_engine()
        probe = FakeProbe(MicSample(True, ("zoom.exe",)))
        det = self._inject_detector(engine, probe, off_grace_seconds=0.0)
        det.observe(datetime.now(timezone.utc) - timedelta(minutes=10))
        det.observe(datetime.now(timezone.utc) - timedelta(minutes=1))
        assert engine.is_mic_meeting_active()

        engine.aw.is_running.return_value = False  # AW outage
        probe.current = MicSample(False)  # meeting ended mid-outage
        stats = engine.sync()

        assert engine.is_mic_meeting_active() is False, (
            "the mic probe doesn't need AW — the session must close mid-outage "
            "so idle isn't suppressed for the rest of the outage"
        )
        assert stats.calls_detected == 1, "the ended mic span is still recorded"
        assert len(self._sent_mic_events(engine)) == 1

    def test_shutdown_closes_open_mic_session(self):
        from datetime import datetime, timezone

        engine = self._build_engine()
        probe = FakeProbe(MicSample(True, ("zoom.exe",)))
        det = self._inject_detector(engine, probe)
        det.observe(datetime.now(timezone.utc) - timedelta(minutes=10))
        assert engine.is_mic_meeting_active()
        engine.shutdown()
        assert engine.is_mic_meeting_active() is False


class TestIdleManagerMicSuppression:
    """A mic meeting suppresses the idle pause like a window-detected call."""

    def _manager(self, sync_engine):
        from unittest.mock import Mock

        from src.idle_manager import IdleManager

        return IdleManager(sync_engine, tray=Mock(), aw=Mock(), config=Config())

    def test_mic_meeting_counts_as_engaged(self):
        from unittest.mock import Mock

        eng = Mock()
        eng.is_in_call.return_value = False
        eng.is_mic_meeting_active.return_value = True
        assert self._manager(eng)._is_engaged_without_input() is True

    def test_mic_check_error_falls_through(self):
        from unittest.mock import Mock

        eng = Mock()
        eng.is_in_call.return_value = False
        eng.is_mic_meeting_active.side_effect = RuntimeError("boom")
        eng.is_active_dev_session.return_value = False
        assert self._manager(eng)._is_engaged_without_input() is False

    def test_not_engaged_when_all_signals_false(self):
        from unittest.mock import Mock

        eng = Mock()
        eng.is_in_call.return_value = False
        eng.is_mic_meeting_active.return_value = False
        eng.is_active_dev_session.return_value = False
        assert self._manager(eng)._is_engaged_without_input() is False


class TestOutageAndShutdownDurability:
    """Ended sessions must survive full-offline outages and shutdown."""

    def test_offline_outage_queues_ended_session(self):
        harness = TestSyncEngineMicIntegration()
        engine = harness._build_engine()
        probe = FakeProbe(MicSample(True, ("zoom.exe",)))
        det = harness._inject_detector(engine, probe, off_grace_seconds=0.0)
        now = datetime.now(timezone.utc)
        det.observe(now - timedelta(minutes=10))
        det.observe(now - timedelta(minutes=1))

        engine.aw.is_running.return_value = False   # AW outage…
        engine.bf.is_reachable.return_value = False  # …and fully offline
        probe.current = MicSample(False)
        engine.sync()

        assert engine.is_mic_meeting_active() is False
        assert not engine.queue.is_empty(), (
            "an ended mic span while fully offline must be QUEUED — the "
            "session state is already reset, it can never be re-emitted"
        )

    def test_afk_samples_keep_flowing_during_aw_outage(self):
        # AfkSource needs only the OS idle clock, not AW. Without outage-branch
        # sampling, an AW hiccup spanning a hands-off meeting leaves a >timeout
        # hole in the sample log that reconstructs as afk — the exact
        # billed-idle-during-meeting failure this feature fixes.
        from src.sync.afk_source import AfkSource

        harness = TestSyncEngineMicIntegration()
        engine = harness._build_engine()
        engine.afk_source = AfkSource(
            afk_timeout_seconds=600.0, hostname="h", idle_clock=lambda: 5.0
        )
        engine.aw.is_running.return_value = False
        engine.sync()
        assert len(engine.afk_source.samples) == 1

    def test_shutdown_sends_final_mic_event_immediately(self):
        # Shutdown runs on logout: the final span must be SENT under the
        # owner's token, not left in the account-agnostic queue for whoever
        # logs in next on a shared machine.
        harness = TestSyncEngineMicIntegration()
        engine = harness._build_engine()
        probe = FakeProbe(MicSample(True, ("zoom.exe",)))
        det = harness._inject_detector(engine, probe)
        now = datetime.now(timezone.utc)
        det.observe(now - timedelta(minutes=10))
        det.observe(now - timedelta(minutes=1))

        engine.shutdown()
        finals = [
            ev
            for ev in harness._sent_mic_events(engine)
            if ev["data"]["status"] == "completed"
        ]
        assert len(finals) == 1, "final mic span sent at shutdown"
        assert engine.queue.is_empty(), "sent, so nothing left for the next user"

    def test_shutdown_queues_final_mic_event_when_send_fails(self):
        from types import SimpleNamespace

        harness = TestSyncEngineMicIntegration()
        engine = harness._build_engine()
        engine.bf.send_events.return_value = SimpleNamespace(
            success=False, events_synced=0, accepted_ids=[], error="boom",
            transient=True,
        )
        probe = FakeProbe(MicSample(True, ("zoom.exe",)))
        det = harness._inject_detector(engine, probe)
        now = datetime.now(timezone.utc)
        det.observe(now - timedelta(minutes=10))
        det.observe(now - timedelta(minutes=1))

        engine.shutdown()
        assert not engine.queue.is_empty(), (
            "send failed → the final span falls back to the queue"
        )


class TestMicKillSwitch:
    def test_server_mic_signal_off_closes_session_without_restart(self):
        # The detector is built at startup, but a server-pushed
        # mic_signal=false must stop the privacy-sensitive probe NOW.
        harness = TestSyncEngineMicIntegration()
        engine = harness._build_engine()
        probe = FakeProbe(MicSample(True, ("zoom.exe",)))
        det = harness._inject_detector(engine, probe)
        now = datetime.now(timezone.utc)
        det.observe(now - timedelta(minutes=10))
        det.observe(now - timedelta(minutes=1))
        assert engine.is_mic_meeting_active()

        engine.config.call_detection.mic_signal = False
        engine.sync()

        assert engine.is_mic_meeting_active() is False
        finals = [
            ev
            for ev in harness._sent_mic_events(engine)
            if ev["data"]["status"] == "completed"
        ]
        assert len(finals) == 1, "the truthful pre-kill span still ships"


class TestCapLatchRobustness:
    def test_probe_error_does_not_clear_cap_latch(self):
        # A wedged-hot mic hits the cap and latches; a transient probe error
        # (in_use=None) must NOT unlatch — only a definite cold read proves
        # the mic released. Otherwise every flap re-opens a fresh cap period.
        probe = FakeProbe(MicSample(True))
        det = _detector(probe, max_credit_seconds=3600.0)
        det.observe(_ts(0))
        det.observe(_ts(30))
        det.observe(_ts(60))  # cap force-close + latch
        assert not det.is_active()

        probe.current = MicSample(None)  # probe error
        det.observe(_ts(61))
        probe.current = MicSample(True)  # wedged mic still "hot"
        det.observe(_ts(62))
        assert not det.is_active(), "latch must survive a probe error"

        probe.current = MicSample(False)  # definite cold read
        det.observe(_ts(63))
        probe.current = MicSample(True)
        det.observe(_ts(64))
        assert det.is_active(), "a real cold-then-hot transition reopens"


class TestShutdownAuthPolicy:
    def test_auth_error_at_shutdown_drops_instead_of_queueing(self):
        # An expired token is the likeliest failure at logout. The offline
        # queue is account-agnostic: a queued row would be delivered under
        # whoever logs in NEXT on this machine. Drop, matching
        # _send_status_span's policy.
        from src.sync.bf_client import BetterFlowAuthError

        harness = TestSyncEngineMicIntegration()
        engine = harness._build_engine()
        engine.bf.send_events.side_effect = BetterFlowAuthError("token expired")
        probe = FakeProbe(MicSample(True, ("zoom.exe",)))
        det = harness._inject_detector(engine, probe)
        now = datetime.now(timezone.utc)
        det.observe(now - timedelta(minutes=10))
        det.observe(now - timedelta(minutes=1))

        engine.shutdown()
        assert engine.queue.is_empty(), (
            "auth failure at shutdown must DROP the final span, not queue it "
            "for the next login"
        )


class TestKillSwitchDeferralExemption:
    def test_mic_signal_false_applies_despite_deferral_gate(self):
        # mic_signal=false is the privacy kill switch — it must not wait for
        # the staged rollout of the call_detection block.
        cfg = Config()
        assert cfg.call_detection.mic_signal is True
        cfg.update_from_server({"call_detection": {"mic_signal": False}})
        assert cfg.call_detection.mic_signal is False

    def test_mic_signal_true_stays_deferred(self):
        # Only "off" passes through the gate; enabling is a rollout decision.
        cfg = Config()
        cfg.call_detection.mic_signal = False
        cfg.update_from_server({"call_detection": {"mic_signal": True}})
        assert cfg.call_detection.mic_signal is False

    def test_kill_switch_applies_during_aw_outage_too(self):
        harness = TestSyncEngineMicIntegration()
        engine = harness._build_engine()

        class CountingProbe:
            def __init__(self):
                self.samples = 0

            def sample(self):
                self.samples += 1
                return MicSample(True, ("zoom.exe",))

        probe = CountingProbe()
        det = harness._inject_detector(engine, probe)
        now = datetime.now(timezone.utc)
        det.observe(now - timedelta(minutes=10))
        det.observe(now - timedelta(minutes=1))
        sampled_before = probe.samples

        engine.config.call_detection.mic_signal = False
        engine.aw.is_running.return_value = False  # AW outage
        engine.sync()

        assert probe.samples == sampled_before, (
            "a server-disabled mic probe must not keep sampling during an "
            "AW outage"
        )
        assert engine.is_mic_meeting_active() is False, "open session flushed"
