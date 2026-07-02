"""Coalesce sub-threshold same-window fragments so an AW heartbeat-merge
failure doesn't cost the whole span's per-app attribution to the flicker filter.

Symptom (Sachi device 16, recurring 2026-06-26 .. 07-02): every session logs
``the watcher IS producing window events but the filter is dropping them all
(99 window event(s) under the 5s minimum (flicker filter))``. The window
watcher emits a focus as many sub-5s fragments instead of one merged event;
each fragment is individually dropped by ``min_window_event_seconds``, so the
per-app category breakdown for that span disappears. Billing is unaffected
(time comes from AFK/input), but the timeline loses attribution.

Fix: merge runs of consecutive, time-contiguous, identical-window (app+title+
url) fragments into one event before the filter runs — analogous to
``_collapse_afk_duplicates``. The merged event clears the 5s filter and the
attribution is recovered.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from src.config import Config
from src.sync.aw_client import BUCKET_TYPE_WINDOW, AWEvent
from src.sync.sync_engine import SyncEngine, SyncStats, _SyncCycleContext

BASE = datetime(2026, 7, 2, 9, 0, 0, tzinfo=timezone.utc)


def _win(idx, offset_s, duration_s, app="Code.exe", title="main.py", url=None):
    """A window AWEvent starting BASE+offset for duration_s seconds."""
    data = {"app": app, "title": title}
    if url is not None:
        data["url"] = url
    return AWEvent(
        id=idx,
        timestamp=BASE + timedelta(seconds=offset_s),
        duration=duration_s,
        data=data,
    )


class TestCoalesceWindowFlickers:
    def test_merges_contiguous_identical_fragments(self):
        # Three 2s fragments of the SAME window, back-to-back → one 6s event.
        events = [_win(1, 0, 2), _win(2, 2, 2), _win(3, 4, 2)]
        merged = SyncEngine._coalesce_window_flickers(events)
        assert len(merged) == 1
        assert merged[0].id == 1  # keeps the first fragment's id (stable dedup)
        assert merged[0].duration == 6.0
        assert merged[0].timestamp == BASE

    def test_does_not_merge_different_apps(self):
        events = [_win(1, 0, 2, app="Code.exe"), _win(2, 2, 2, app="Chrome.exe")]
        merged = SyncEngine._coalesce_window_flickers(events)
        assert len(merged) == 2

    def test_does_not_merge_different_titles(self):
        events = [_win(1, 0, 2, title="a.py"), _win(2, 2, 2, title="b.py")]
        merged = SyncEngine._coalesce_window_flickers(events)
        assert len(merged) == 2

    def test_does_not_merge_across_a_real_gap(self):
        # Same window but 30s apart — a different window was focused between
        # them; these are two genuine visits, not one fragmented focus.
        events = [_win(1, 0, 2), _win(2, 32, 2)]
        merged = SyncEngine._coalesce_window_flickers(events)
        assert len(merged) == 2

    def test_fully_contained_duplicate_is_dropped_not_double_counted(self):
        # A 6s event and a 2s event inside it (heartbeat-merge overlap) → one
        # 6s event, never 8s.
        events = [_win(1, 0, 6), _win(2, 2, 2)]
        merged = SyncEngine._coalesce_window_flickers(events)
        assert len(merged) == 1
        assert merged[0].duration == 6.0

    def test_empty_and_single_are_passthrough(self):
        assert SyncEngine._coalesce_window_flickers([]) == []
        one = [_win(1, 0, 2)]
        assert SyncEngine._coalesce_window_flickers(one) == one


class TestFlickerSurvivesFilterEndToEnd:
    """Drive the real transform+filter path, asserting on emitted events."""

    def _engine(self):
        aw, bf, queue = Mock(), Mock(), Mock()
        queue.get_checkpoint.return_value = None
        config = Config()
        assert config.sync.min_window_event_seconds == 5.0
        analyzer = Mock()
        analyzer.get_activity_state.return_value = "active"
        analyzer.get_raw_metrics.return_value = Mock(
            to_dict=lambda: {"presses": 0, "clicks": 0, "scrolls": 0, "window_changes": 0}
        )
        analyzer.get_fraud_assessment.return_value = Mock(
            score=0, signals=[], extra_metrics={}
        )
        time_tracker = Mock()
        return SyncEngine(
            aw=aw, bf=bf, queue=queue, config=config,
            activity_analyzer=analyzer, time_tracker=time_tracker,
        )

    def test_sub5s_fragments_are_recovered_as_one_attributed_event(self):
        engine = self._engine()
        # Three 2s fragments of the same window: each is BELOW the 5s filter,
        # so pre-fix all three are dropped and attribution is lost.
        events = [_win(1, 0, 2), _win(2, 2, 2), _win(3, 4, 2)]
        stats = SyncStats()
        transformed, checkpoint = engine._transform_and_checkpoint(
            events, "aw-watcher-window_host", BUCKET_TYPE_WINDOW, stats, _SyncCycleContext()
        )
        # Post-fix: one window event survives, carrying the app attribution.
        assert len(transformed) == 1
        assert transformed[0]["data"]["app"] == "Code.exe"
        assert transformed[0]["duration"] >= 5.0
        # Checkpoint must still point at the NEWEST ORIGINAL event (id 3), not
        # the coalesced event (id 1) — coalescing must not rewind progress.
        assert checkpoint is not None
        assert checkpoint[2] == 3

    def test_genuine_short_distinct_windows_still_filtered(self):
        # Two different sub-5s windows that are NOT the same focus must still be
        # dropped — coalescing only rescues fragmented identical windows.
        engine = self._engine()
        events = [_win(1, 0, 2, app="A.exe", title="a"),
                  _win(2, 2, 2, app="B.exe", title="b")]
        stats = SyncStats()
        transformed, _ = engine._transform_and_checkpoint(
            events, "aw-watcher-window_host", BUCKET_TYPE_WINDOW, stats, _SyncCycleContext()
        )
        assert transformed == []
