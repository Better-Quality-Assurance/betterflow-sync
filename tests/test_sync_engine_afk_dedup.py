"""Collapsing duplicate/overlapping AFK heartbeat rows.

A misbehaving idle tracker (or a server heartbeat-merge failure) emits many AFK
rows sharing a start timestamp with growing durations — observed live as 29
'afk' rows all starting at the same instant (furdui.iancu, 2026-06-17). This
happens with a SINGLE tracker, so it is distinct from the orphan-tracker bug.
Synced raw, the overlapping rows are billed as redundant idle. These pin that
_collapse_afk_duplicates removes the corruption while preserving the timeline.
"""

from datetime import datetime, timedelta, timezone

from src.sync.aw_client import AWEvent
from src.sync.sync_engine import SyncEngine


def _afk(start: datetime, dur: float, status: str) -> AWEvent:
    return AWEvent(id=0, timestamp=start, duration=dur, data={"status": status})


def test_collapses_29_same_start_afk_rows_into_one():
    base = datetime(2026, 6, 17, 4, 16, 14, tzinfo=timezone.utc)
    # The exact corruption shape: same start, growing durations.
    durations = list(range(622, 2061, 50))
    rows = [_afk(base, dur, "afk") for dur in durations]
    assert len(rows) > 20

    out = SyncEngine._collapse_afk_duplicates(rows)

    assert len(out) == 1, "all same-start 'afk' duplicates collapse to one span"
    assert out[0].timestamp == base
    assert out[0].duration == max(durations), "kept the longest (latest-ending) span"


def test_preserves_distinct_nonoverlapping_spans():
    t0 = datetime(2026, 6, 17, 7, 0, 0, tzinfo=timezone.utc)
    not_afk = _afk(t0, 300, "not-afk")             # 07:00–07:05
    afk = _afk(t0 + timedelta(seconds=300), 600, "afk")  # 07:05–07:15
    not_afk2 = _afk(t0 + timedelta(seconds=900), 120, "not-afk")  # 07:15–07:17

    out = SyncEngine._collapse_afk_duplicates([not_afk, afk, not_afk2])

    assert [e.status for e in out] == ["not-afk", "afk", "not-afk"], "real timeline untouched"
    assert [round(e.duration) for e in out] == [300, 600, 120]


def test_merges_overlapping_same_status_spans():
    t0 = datetime(2026, 6, 17, 8, 0, 0, tzinfo=timezone.utc)
    a = _afk(t0, 600, "afk")                            # 08:00–08:10
    b = _afk(t0 + timedelta(seconds=120), 600, "afk")  # 08:02–08:12 (overlaps a)

    out = SyncEngine._collapse_afk_duplicates([a, b])

    assert len(out) == 1, "overlapping same-status spans merge into one"
    assert out[0].duration == 720, "merged span runs 08:00–08:12"


def test_never_merges_across_a_different_status():
    """An intervening not-afk must NOT be erased by merging the afk spans around
    it — that would fabricate idle over real activity."""
    t0 = datetime(2026, 6, 17, 8, 0, 0, tzinfo=timezone.utc)
    afk1 = _afk(t0, 120, "afk")                           # 08:00–08:02
    active = _afk(t0 + timedelta(seconds=120), 120, "not-afk")  # 08:02–08:04
    afk2 = _afk(t0 + timedelta(seconds=240), 120, "afk")  # 08:04–08:06

    out = SyncEngine._collapse_afk_duplicates([afk1, active, afk2])

    assert [e.status for e in out] == ["afk", "not-afk", "afk"], "activity preserved between idle spans"


def test_does_not_mutate_source_events():
    base = datetime(2026, 6, 17, 4, 16, 14, tzinfo=timezone.utc)
    a = _afk(base, 600, "afk")
    b = _afk(base, 1200, "afk")
    SyncEngine._collapse_afk_duplicates([a, b])
    assert a.duration == 600 and b.duration == 1200, "source events untouched"
