from datetime import datetime, timedelta, timezone

from src.sync.afk_source import AfkSource

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


def _src(idle_value, **kw):
    return AfkSource(afk_timeout_seconds=600, hostname="host",
                     idle_clock=lambda: idle_value, **kw)


def test_available_true_when_idle_clock_returns_value():
    assert _src(5.0).available() is True


def test_available_false_when_idle_clock_returns_none():
    assert _src(None).available() is False


def test_record_sample_appends_last_input_from_idle_clock():
    src = _src(30.0)
    src.record_sample(T0)
    assert src.samples == [(T0, T0 - timedelta(seconds=30))]


def test_record_sample_noop_when_clock_unavailable():
    src = _src(None)
    src.record_sample(T0)
    assert src.samples == []


def test_record_sample_prefers_fresher_input_watcher():
    class W:
        def get_last_input_at(self):
            return T0 - timedelta(seconds=2)  # fresher than idle clock's 30s
    src = _src(30.0, input_watcher=W())
    src.record_sample(T0)
    assert src.samples == [(T0, T0 - timedelta(seconds=2))]


def test_retention_prunes_old_samples():
    src = _src(0.0, retention_seconds=100)
    src.record_sample(T0)
    src.record_sample(T0 + timedelta(seconds=200))  # T0 now older than retention
    assert [s[0] for s in src.samples] == [T0 + timedelta(seconds=200)]
