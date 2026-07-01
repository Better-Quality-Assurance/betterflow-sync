"""Window title-flicker coalescing.

A window title that updates faster than min_window_event_seconds (a browser tab
with a live counter, a terminal, a media player) makes ActivityWatch emit a new
window event on every title change — each below the flicker filter's minimum, so
_transform_event drops them ALL and the app loses its per-app timeline entirely
(Sachi Navodi, 2026-07-01: chrome.exe, 99 sub-5s events dropped every cycle →
"No activity tracked" while hours still tracked).

_coalesce_window_flicker merges a run of consecutive SAME-APP events into one
span so continuous focus survives the minimum, but ONLY when the run would
otherwise lose data — so normal (already-passing) events are untouched.
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from src.config import Config
from src.sync.aw_client import AWEvent
from src.sync.queue import OfflineQueue
from src.sync.sync_engine import SyncEngine

BASE = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)


def _engine() -> SyncEngine:
    tmp = Path(tempfile.mkdtemp())
    return SyncEngine(
        aw=Mock(),
        bf=Mock(),
        queue=OfflineQueue(db_path=tmp / "q.db", max_size=10000),
        config=Config(),  # min_window_event_seconds defaults to 5.0
        time_tracker=Mock(),
    )


def _ev(offset_s: float, dur: float, app: str, title: str = "", eid: int = 0) -> AWEvent:
    return AWEvent(
        id=eid or int(offset_s * 1000),
        timestamp=BASE + timedelta(seconds=offset_s),
        duration=dur,
        data={"app": app, "title": title},
    )


class TestCoalesceWindowFlicker:
    def test_same_app_flicker_run_merges_into_one_passing_span(self):
        eng = _engine()
        # 6 back-to-back 1s chrome events (title flickering) — each < 5s min.
        events = [_ev(i, 1.0, "chrome.exe", f"tab {i}", eid=100 + i) for i in range(6)]

        merged = eng._coalesce_window_flicker(events)

        assert len(merged) == 1, "the flicker run should collapse to a single span"
        m = merged[0]
        assert m.app == "chrome.exe"
        assert abs(m.duration - 6.0) < 0.01, "span = first.start -> last.end"
        assert m.timestamp == events[0].timestamp
        assert m.id == events[-1].id, "anchored on the run's newest id"
        # And it now clears the flicker filter (would have been dropped before).
        assert m.duration >= eng.config.sync.min_window_event_seconds

    def test_different_apps_are_not_merged(self):
        eng = _engine()
        # Alt-tab noise across apps: each 1s, different app — must NOT merge, so
        # the per-event minimum still suppresses cross-app flicker.
        events = [
            _ev(0, 1.0, "chrome.exe", eid=1),
            _ev(1, 1.0, "slack.exe", eid=2),
            _ev(2, 1.0, "code.exe", eid=3),
        ]
        merged = eng._coalesce_window_flicker(events)
        assert [m.app for m in merged] == ["chrome.exe", "slack.exe", "code.exe"]
        assert all(m.duration == 1.0 for m in merged)

    def test_all_long_events_are_left_byte_identical(self):
        eng = _engine()
        # Every event already clears the minimum → no data at risk → no merge.
        events = [_ev(0, 30.0, "chrome.exe", eid=1), _ev(30, 30.0, "chrome.exe", eid=2)]
        merged = eng._coalesce_window_flicker(events)
        assert merged == events, "already-passing events must be untouched"

    def test_flicks_are_absorbed_into_adjacent_same_app_event(self):
        eng = _engine()
        # A long chrome event followed by two 1s chrome title flicks → one span.
        events = [
            _ev(0, 10.0, "chrome.exe", "main", eid=1),
            _ev(10, 1.0, "chrome.exe", "notif 1", eid=2),
            _ev(11, 1.0, "chrome.exe", "notif 2", eid=3),
        ]
        merged = eng._coalesce_window_flicker(events)
        assert len(merged) == 1
        assert abs(merged[0].duration - 12.0) < 0.01
        # Representative title comes from the longest-lived member.
        assert merged[0].title == "main"

    def test_gap_larger_than_tolerance_breaks_the_run(self):
        eng = _engine()
        # Same app but a 30s gap between the flicks → two separate runs, neither
        # mergeable (each a lone 1s event), so both stay short (and would drop).
        events = [_ev(0, 1.0, "chrome.exe", eid=1), _ev(31, 1.0, "chrome.exe", eid=2)]
        merged = eng._coalesce_window_flicker(events)
        assert len(merged) == 2, "a large gap must not be bridged"

    def test_short_same_app_run_still_under_minimum_is_not_rescued(self):
        eng = _engine()
        # Two 1s flicks total 2s (< 5s) — merged, but still below the minimum, so
        # this genuinely-brief same-app focus is still (correctly) suppressed.
        events = [_ev(0, 1.0, "chrome.exe", eid=1), _ev(1, 1.0, "chrome.exe", eid=2)]
        merged = eng._coalesce_window_flicker(events)
        assert len(merged) == 1
        assert merged[0].duration < eng.config.sync.min_window_event_seconds

    def test_source_events_not_mutated(self):
        eng = _engine()
        events = [_ev(i, 1.0, "chrome.exe", f"t{i}", eid=i + 1) for i in range(4)]
        before = [(e.id, e.timestamp, e.duration, dict(e.data)) for e in events]
        eng._coalesce_window_flicker(events)
        after = [(e.id, e.timestamp, e.duration, dict(e.data)) for e in events]
        assert before == after, "frozen source events must never be mutated"

    def test_fewer_than_two_events_returned_as_is(self):
        eng = _engine()
        assert eng._coalesce_window_flicker([]) == []
        one = [_ev(0, 1.0, "chrome.exe", eid=1)]
        assert eng._coalesce_window_flicker(one) == one
