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


def _seed(src, *pairs):
    """pairs: (sample_time, last_input_at)."""
    for st, li in pairs:
        with src._lock:
            src._samples.append((st, li))


def _spans(events):
    return [(e["data"]["status"], e["timestamp"], round(e["duration"])) for e in events]


def test_continuous_activity_is_one_notafk_span():
    src = _src(0.0)
    _seed(src, (T0, T0), (T0 + timedelta(seconds=30), T0 + timedelta(seconds=30)))
    ev = src.build_afk_events(T0, T0 + timedelta(seconds=30))
    assert _spans(ev) == [("not-afk", T0.isoformat(), 30)]


def test_idle_past_timeout_flips_at_last_input_plus_timeout():
    src = _src(700.0)
    _seed(src, (T0, T0), (T0 + timedelta(seconds=700), T0))
    ev = src.build_afk_events(T0, T0 + timedelta(seconds=700))
    assert _spans(ev) == [
        ("not-afk", T0.isoformat(), 600),
        ("afk", (T0 + timedelta(seconds=600)).isoformat(), 100),
    ]


def test_pause_shorter_than_timeout_stays_active():
    src = _src(599.0)
    _seed(src, (T0, T0), (T0 + timedelta(seconds=599), T0))
    ev = src.build_afk_events(T0, T0 + timedelta(seconds=599))
    assert _spans(ev) == [("not-afk", T0.isoformat(), 599)]


def test_no_samples_in_range_is_afk():
    src = _src(0.0)
    ev = src.build_afk_events(T0, T0 + timedelta(seconds=300))
    assert _spans(ev) == [("afk", T0.isoformat(), 300)]


def test_gap_with_no_samples_billed_afk_not_active():
    src = _src(0.0)
    _seed(src, (T0, T0), (T0 + timedelta(seconds=3600), T0 + timedelta(seconds=3600)))
    ev = src.build_afk_events(T0, T0 + timedelta(seconds=3600))
    statuses = [e["data"]["status"] for e in ev]
    assert "not-afk" in statuses and "afk" in statuses
    notafk = sum(e["duration"] for e in ev if e["data"]["status"] == "not-afk")
    assert notafk <= 1200


def test_empty_range_returns_nothing():
    src = _src(0.0)
    assert src.build_afk_events(T0, T0) == []


def test_project_id_attached_when_given():
    src = _src(0.0)
    _seed(src, (T0, T0))
    ev = src.build_afk_events(T0, T0 + timedelta(seconds=10), project_id=42)
    assert all(e["project_id"] == 42 for e in ev)


def test_consecutive_cycles_are_contiguous_and_non_overlapping():
    src = _src(0.0)
    _seed(src, (T0, T0), (T0 + timedelta(seconds=30), T0 + timedelta(seconds=30)),
          (T0 + timedelta(seconds=60), T0 + timedelta(seconds=60)))
    a = src.build_afk_events(T0, T0 + timedelta(seconds=30))
    b = src.build_afk_events(T0 + timedelta(seconds=30), T0 + timedelta(seconds=60))
    a_end = datetime.fromisoformat(a[-1]["timestamp"]) + timedelta(seconds=a[-1]["duration"])
    b_start = datetime.fromisoformat(b[0]["timestamp"])
    assert a_end == b_start
    assert a[-1]["id"] != b[0]["id"]
