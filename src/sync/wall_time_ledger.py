"""Wall-clock ledger so two window sources never credit the same time twice.

The in-process window stream and the external bf-window-tracker bucket describe
the SAME wall time under different bucket and event ids. DailyTimeTracker dedups
per (bucket_id, event_id), so when the in-process probe goes blind and the
external bucket is replayed over a span the in-process stream already counted,
both credited it (#300: 55 s became 110 s). This ledger remembers which wall
intervals each source group has claimed, so a source is only credited for the
part of a claim that the OTHER group has not already covered.

Callers claim only the NEW part of an event (``[start+prev, start+now]`` for a
growing event), never the whole span again. Within one group those claims are
disjoint, so the total credited equals the measure of the union of every span
both groups have claimed: nothing is counted twice and nothing is lost when an
event grows after the other group overlapped it.

Events inside one group are not clipped against each other: a growing event
re-reports its own earlier interval, and the per-event cache already handles it.

The ledger is in-memory; SyncEngine persists each event's span next to its
counted-time row and re-seeds the ledger with ``record`` on load, so a restart
cannot forget what the other group covered.
"""
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

__all__ = ["WallTimeLedger"]

Interval = Tuple[datetime, datetime]

# Intervals older than this are dropped; a replay never reaches further back
# than the reconcile window.
_RETENTION = timedelta(hours=48)


def _merge(intervals: List[Interval]) -> List[Interval]:
    merged: List[Interval] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return merged


class WallTimeLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_group: Dict[str, List[Interval]] = {}

    def clear(self) -> None:
        with self._lock:
            self._by_group.clear()

    def record(self, group: str, start: datetime, end: datetime) -> None:
        """Remember ``[start, end]`` as covered by ``group`` without crediting
        anything (used to re-seed the ledger from persisted spans)."""
        self.claim(group, start, end)

    def claim(self, group: str, start: datetime, end: datetime) -> float:
        """Record ``[start, end]`` for ``group`` and return how many of its
        seconds are NOT already covered by any other group."""
        if end <= start:
            return 0.0
        with self._lock:
            overlap = 0.0
            for other, intervals in self._by_group.items():
                if other == group:
                    continue
                for s, e in intervals:
                    lo, hi = max(s, start), min(e, end)
                    if hi > lo:
                        overlap += (hi - lo).total_seconds()
            own = self._by_group.setdefault(group, [])
            own.append((start, end))
            cutoff = end - _RETENTION
            self._by_group[group] = _merge([iv for iv in own if iv[1] >= cutoff])
        return max(0.0, (end - start).total_seconds() - overlap)
