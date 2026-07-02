from datetime import datetime, timedelta, timezone

from src.sync.aw_client import BUCKET_TYPE_WINDOW
from src.sync.window_source import WindowSource

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


class _Getter:
    """Fake foreground getter driven by a scripted list of (app, title) or None."""

    def __init__(self, results):
        self._results = list(results)
        self._i = 0

    def __call__(self):
        if self._i >= len(self._results):
            return self._results[-1] if self._results else None
        r = self._results[self._i]
        self._i += 1
        return r


def _src(getter, **kw):
    return WindowSource(hostname="host", foreground_getter=getter, **kw)


def _seed(src, *triples):
    """triples: (sample_time, app, title)."""
    for st, app, title in triples:
        with src._lock:
            src._samples.append((st, app, title))


def _spans(events):
    return [(e["data"]["app"], e["data"]["title"], e["timestamp"], round(e["duration"]))
            for e in events]


# -- available() / sticky latch ----------------------------------------------


def test_available_true_when_getter_reads():
    assert _src(_Getter([("Code", "main.py")])).available() is True


def test_available_false_when_getter_returns_none():
    assert _src(_Getter([None])).available() is False


def test_available_false_when_unsupported_platform():
    assert _src(None).available() is False


def test_available_latch_survives_transient_failure():
    src = _src(_Getter([("Code", "a"), None, None]))
    assert src.available() is True  # first read succeeds -> latched
    # A later blind read must NOT flap availability off.
    src.record_sample(T0 + timedelta(seconds=30))  # returns None -> gap
    assert src.available() is True


# -- record_sample -----------------------------------------------------------


def test_record_sample_appends_app_and_title():
    src = _src(_Getter([("Code", "main.py")]))
    src.record_sample(T0)
    assert src.samples == [(T0, "Code", "main.py")]


def test_record_sample_noop_when_unsupported():
    src = _src(None)
    src.record_sample(T0)
    assert src.samples == []


def test_blind_getter_invents_no_events():
    # A focus we can't read (None) must append nothing — never an invented app.
    src = _src(_Getter([None, None]))
    src.record_sample(T0)
    src.record_sample(T0 + timedelta(seconds=30))
    assert src.samples == []
    ev = src.build_window_events(T0, T0 + timedelta(seconds=60))
    assert ev == []


def test_blank_app_is_a_gap():
    src = _src(_Getter([("", "some title")]))
    src.record_sample(T0)
    assert src.samples == []
    assert src.consecutive_failures == 1


def test_consecutive_failures_reset_on_success():
    src = _src(_Getter([None, ("Code", "a")]))
    src.record_sample(T0)
    assert src.consecutive_failures == 1
    src.record_sample(T0 + timedelta(seconds=30))
    assert src.consecutive_failures == 0


def test_retention_prunes_old_samples():
    src = _src(_Getter([("Code", "a"), ("Code", "b")]), retention_seconds=100)
    src.record_sample(T0)
    src.record_sample(T0 + timedelta(seconds=200))  # T0 now older than retention
    assert [s[0] for s in src.samples] == [T0 + timedelta(seconds=200)]


# -- build_window_events -----------------------------------------------------


def test_same_app_run_coalesces_into_one_span():
    src = _src(_Getter([]))
    _seed(src,
          (T0, "Code", "a.py"),
          (T0 + timedelta(seconds=30), "Code", "a.py"),
          (T0 + timedelta(seconds=60), "Code", "a.py"))
    ev = src.build_window_events(T0, T0 + timedelta(seconds=90))
    assert _spans(ev) == [("Code", "a.py", T0.isoformat(), 90)]


def test_unobserved_gap_is_not_credited_as_focus():
    """A gap longer than MAX_UNOBSERVED_GAP_SECONDS (sleep/lid-close) must NOT be
    billed as focus on the last-seen app. Two samples of the same app 1h apart
    should credit at most ~cap seconds around each, never the whole hour."""
    src = _src(_Getter([]))
    _seed(src,
          (T0, "Code", "a.py"),
          (T0 + timedelta(seconds=3600), "Code", "a.py"))
    ev = src.build_window_events(T0, T0 + timedelta(seconds=3610))
    total = sum(e["duration"] for e in ev)
    cap = src.MAX_UNOBSERVED_GAP_SECONDS
    # Bounded: the boundary run (~cap) + the trailing tail (~10s), never ~3610.
    assert total <= cap + 60, f"unobserved 1h gap fabricated {total}s of focus"
    assert total < 3600, "the sleep gap must not be credited as focus"


def test_small_gaps_within_cap_still_coalesce():
    """Gaps within the cap are normal sampling and still merge into one run (no
    regression to the intended coalescing)."""
    src = _src(_Getter([]), max_unobserved_gap_seconds=120)
    _seed(src,
          (T0, "Code", "a.py"),
          (T0 + timedelta(seconds=60), "Code", "a.py"),
          (T0 + timedelta(seconds=110), "Code", "a.py"))
    ev = src.build_window_events(T0, T0 + timedelta(seconds=120))
    assert _spans(ev) == [("Code", "a.py", T0.isoformat(), 120)]


def test_app_change_opens_new_span():
    src = _src(_Getter([]))
    _seed(src,
          (T0, "Code", "a.py"),
          (T0 + timedelta(seconds=30), "Chrome", "docs"),
          (T0 + timedelta(seconds=60), "Chrome", "docs"))
    ev = src.build_window_events(T0, T0 + timedelta(seconds=90))
    assert _spans(ev) == [
        ("Code", "a.py", T0.isoformat(), 30),
        ("Chrome", "docs", (T0 + timedelta(seconds=30)).isoformat(), 60),
    ]


def test_representative_title_is_longest_held():
    # Same app the whole run, but the title changes. "b" is held from +10 to +60
    # (50s) vs "a" from 0 to +10 (10s) -> "b" wins.
    src = _src(_Getter([]))
    _seed(src,
          (T0, "Code", "a"),
          (T0 + timedelta(seconds=10), "Code", "b"),
          (T0 + timedelta(seconds=60), "Code", "b"))
    ev = src.build_window_events(T0, T0 + timedelta(seconds=60))
    assert len(ev) == 1
    assert ev[0]["data"] == {"app": "Code", "title": "b"}
    assert round(ev[0]["duration"]) == 60


def test_no_samples_in_range_is_empty():
    src = _src(_Getter([]))
    assert src.build_window_events(T0, T0 + timedelta(seconds=300)) == []


def test_single_sample_spans_to_range_end():
    src = _src(_Getter([]))
    _seed(src, (T0, "Code", "a"))
    ev = src.build_window_events(T0, T0 + timedelta(seconds=60))
    assert _spans(ev) == [("Code", "a", T0.isoformat(), 60)]


def test_empty_range_returns_empty():
    src = _src(_Getter([]))
    _seed(src, (T0, "Code", "a"))
    assert src.build_window_events(T0 + timedelta(seconds=60), T0) == []


def test_events_clamped_to_range():
    src = _src(_Getter([]))
    _seed(src,
          (T0, "Code", "a"),
          (T0 + timedelta(seconds=120), "Code", "a"))
    # Range ends before the last sample; span is clamped to range_end.
    ev = src.build_window_events(T0, T0 + timedelta(seconds=60))
    assert _spans(ev) == [("Code", "a", T0.isoformat(), 60)]


def test_no_zero_duration_events():
    src = _src(_Getter([]))
    _seed(src, (T0, "Code", "a"), (T0, "Chrome", "b"))
    ev = src.build_window_events(T0, T0 + timedelta(seconds=30))
    # The Code run at exactly T0 has zero duration (closes at the Chrome sample,
    # also T0) -> dropped. Only the Chrome span survives.
    for e in ev:
        assert e["duration"] > 0
    assert [e["data"]["app"] for e in ev] == ["Chrome"]


# -- event dict shape --------------------------------------------------------


def test_event_shape_and_ids():
    src = _src(_Getter([]))
    _seed(src, (T0, "Code", "a"), (T0 + timedelta(seconds=30), "Code", "a"))
    ev = src.build_window_events(T0, T0 + timedelta(seconds=30))[0]
    assert ev["bucket_id"] == "bf-window-inproc_host"
    assert ev["bucket_type"] == BUCKET_TYPE_WINDOW
    assert ev["id"] == f"win-inproc_host_{int(T0.timestamp() * 1000)}"
    assert ev["timestamp"] == T0.isoformat()
    assert ev["data"] == {"app": "Code", "title": "a"}


def test_bucket_id_single_source_of_truth():
    assert _src(_Getter([])).bucket_id == "bf-window-inproc_host"


# -- immutability ------------------------------------------------------------


def test_source_not_mutated_by_build():
    src = _src(_Getter([]))
    _seed(src, (T0, "Code", "a"), (T0 + timedelta(seconds=30), "Chrome", "b"))
    before = src.samples
    src.build_window_events(T0, T0 + timedelta(seconds=60))
    assert src.samples == before
