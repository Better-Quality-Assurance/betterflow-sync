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

    def test_macos_gate_error_fails_open(self):
        def boom():
            raise RuntimeError("psutil hiccup")

        det = _detector(FakeProbe(MicSample(True)), conferencing_gate=boom)
        det.observe(_ts(0))
        assert det.is_active(), "losing a real meeting to a gate error is the worse failure"

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

    def test_now_while_session_open(self):
        det = _detector(FakeProbe(MicSample(True)))
        det.observe(_ts(0))
        assert det.get_last_active_at(_ts(-60), _ts(30)) == _ts(30)

    def test_credit_capped_past_max_credit(self):
        det = _detector(FakeProbe(MicSample(True)), max_credit_seconds=3600.0)
        det.observe(_ts(0))
        det.observe(_ts(120))
        assert det.get_last_active_at(None, _ts(120)) == _ts(60)

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

        monkeypatch.setattr(mod.sys, "platform", "darwin")
        monkeypatch.setattr(mod, "MacosMicProbe", lambda: FakeProbe())
        cfg = Config()
        cfg.call_detection.max_credit_minutes = 60
        cfg.call_detection.min_call_duration = 45
        det = create_mic_detector(cfg, "h")
        assert det is not None
        assert det._max_credit_seconds == 3600.0
        assert det._min_session_seconds == 45.0

    def test_unsupported_platform_returns_none(self, monkeypatch):
        import src.sync.mic_activity as mod

        monkeypatch.setattr(mod.sys, "platform", "linux")
        assert create_mic_detector(Config(), "h") is None
