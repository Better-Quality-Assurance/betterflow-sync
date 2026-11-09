"""Held input counts and the time span they cover: the ONE implementation of the
rule both input senders stamp their events with.

Two producers upload keystroke/click/scroll COUNTS as spans, and the server
bills an input span as active wall-clock time:

  * ``MacOSInputWatcher`` (the macOS emitter, posting to ActivityWatch every
    ``emit_interval`` seconds and holding its counts across failed posts);
  * ``InputSource`` (the in-process sender, the Windows default and the macOS
    opt-in), drained by ``SyncEngine`` once per sync cycle for [checkpoint, now].

Both used to stamp the span from their own bookkeeping instants (the emitter's
first held count / the engine's checkpoint) to "now". That instant can be days
old: device 53, 2026-11-09, one press held across a 68 h sleep was uploaded as a
68 h input event and billed 13.6 h of a day the laptop spent asleep. Fixing it
in one sender left the other stamping [checkpoint, now] (#302 review).

The rule, in one place:

  * Held counts are kept as SEGMENTS, each with the instant of its first count
    and of its last.
  * A silence between two counts longer than ``max_silence_s`` (the configured
    AFK timeout: past it the agent itself calls the person away) closes the
    segment; the next count opens a new one. Counts are never moved from one
    segment into another, so a segment's counts always sit inside its span and
    nothing is lost or counted twice.
  * A closed segment is stamped [first count, last count + pad]. The open
    (latest) segment ends at ``now``.
  * The first segment starts at ``floor`` (the instant up to which the sender
    has already accounted: the engine's checkpoint, the emitter's last empty or
    posted tick) when its first count is within ``max_silence_s`` of it, which
    is exactly the old steady-state stamp. Otherwise a silence precedes it and
    it starts at its own first count.
  * A span is never shorter than ``pad`` (a sub-interval span only inflates a
    rate computed as count / duration), and never starts before the previous
    span's end.
  * Segments that clamp to the same edge (all held from before ``floor``) are
    stamped as one span, so no two spans share a start, key or event id.

Not thread-safe on its own: every caller holds its own lock around every call,
the same lock that guards its other counter state.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional, Union

logger = logging.getLogger(__name__)

#: At most one fallback warning per this many seconds: max_silence_s() runs on
#: every keystroke, so an unreadable setting must not flood the log.
_FALLBACK_WARN_INTERVAL_S = 600.0

#: The agent's default AFK timeout (config.AWSettings.afk_timeout_minutes = 10).
#: Used only when no configured value is supplied, or the supplied one is not a
#: usable number.
DEFAULT_MAX_SILENCE_S = 600.0

#: One emit interval of the macOS watcher: the shortest span either sender
#: stamps, and how far past its last count a closed segment runs.
DEFAULT_PAD_S = 10.0

Counts = tuple[int, int, int]


@dataclass
class _Segment:
    start: datetime
    last: datetime
    presses: int = 0
    clicks: int = 0
    scrolls: int = 0

    def counts(self) -> Counts:
        return (self.presses, self.clicks, self.scrolls)


@dataclass(frozen=True)
class StampedSpan:
    """One segment of held counts with the span it is uploaded under (or
    several, when they all clamp to the same edge of [floor, now]; see
    ``HeldInputSpans.stamp``).

    ``key`` is the instant that identifies the span independently of ``now``
    (the floor for a span that starts there, else the segment's first count):
    the in-process stream derives its event id from it, so the top-of-cycle
    peek and the later drain of the same segment share one id even when the
    min-duration rule moved the visible start. Keys are distinct within one
    ``stamp`` result, so no two spans of it can share an event id."""

    start: datetime
    end: datetime
    key: datetime
    counts: Counts
    # (segment, the counts this span took from it), for consume().
    _parts: tuple[tuple[_Segment, Counts], ...]

    @property
    def duration_s(self) -> float:
        return (self.end - self.start).total_seconds()


class HeldInputSpans:
    """Segments of held input counts, split at long silences. See module doc."""

    def __init__(
        self,
        max_silence_s: Union[float, Callable[[], float]] = DEFAULT_MAX_SILENCE_S,
        pad_s: float = DEFAULT_PAD_S,
    ) -> None:
        # A callable so a server-pushed AFK timeout applies without a restart.
        self._max_silence = max_silence_s
        self._pad = timedelta(seconds=float(pad_s))
        self._segments: list[_Segment] = []
        # Monotonic instant of the last fallback warning (None: never warned).
        # Guarded by the caller's lock, like the segments.
        self._fallback_warned_at: Optional[float] = None

    def max_silence_s(self) -> float:
        """The configured threshold, or the default when it is unreadable or not
        a positive number (fail toward the documented default, never toward 0,
        which would split every keystroke into its own segment). A fallback is
        logged, rate-limited: it changes how much time is billed as active."""
        value = self._max_silence
        try:
            if callable(value):
                value = value()
            value = float(value)
        except Exception as e:
            self._warn_fallback(f"unreadable ({type(e).__name__}: {e})")
            return DEFAULT_MAX_SILENCE_S
        if not value > 0 or value == float("inf"):
            self._warn_fallback(f"not a positive finite number ({value!r})")
            return DEFAULT_MAX_SILENCE_S
        return value

    def _warn_fallback(self, reason: str) -> None:
        now = time.monotonic()
        if (
            self._fallback_warned_at is not None
            and now - self._fallback_warned_at < _FALLBACK_WARN_INTERVAL_S
        ):
            return
        self._fallback_warned_at = now
        logger.warning(
            "Input span max silence %s; using default %.0f s "
            "(input silences are split at that instead of the AFK timeout)",
            reason, DEFAULT_MAX_SILENCE_S,
        )

    # -- recording ------------------------------------------------------------

    def note(self, now: datetime, presses: int = 0, clicks: int = 0, scrolls: int = 0) -> None:
        """Record counts observed at ``now``. A zero-count note (a key
        auto-repeat: real input, not a press) only keeps the open segment alive;
        it never opens one, since a segment with no counts uploads nothing."""
        silence = timedelta(seconds=self.max_silence_s())
        seg = self._segments[-1] if self._segments else None
        if not (presses or clicks or scrolls):
            if seg is not None and now - seg.last <= silence and now > seg.last:
                seg.last = now
            return
        if seg is None or now - seg.last > silence:
            seg = _Segment(start=now, last=now)
            self._segments.append(seg)
        elif now > seg.last:
            # Forward only: a wall clock stepped back must not end a segment
            # before counts it already holds.
            seg.last = now
        seg.presses += presses
        seg.clicks += clicks
        seg.scrolls += scrolls

    # -- reading --------------------------------------------------------------

    def totals(self) -> Counts:
        p = c = s = 0
        for seg in self._segments:
            p += seg.presses
            c += seg.clicks
            s += seg.scrolls
        return (p, c, s)

    def clear(self) -> None:
        self._segments = []

    def stamp(self, now: datetime, floor: Optional[datetime] = None) -> list[StampedSpan]:
        """The spans the held counts upload under, oldest first. Changes nothing.

        ``floor`` is the instant up to which the caller has already accounted
        (None when it has not accounted for anything yet). Segment instants are
        clamped into [floor, now], so a count recorded outside that range (a
        wall clock that stepped, counts held from before the first checkpoint)
        is stamped at its edge, as the old [floor, now] stamp did.

        Segments whose first count clamps to the same instant (several held
        from before ``floor``, or several past a ``now`` the clock stepped back
        behind) are stamped as ONE span carrying their summed counts: stamped
        separately they would share a start, hence a key and an event id, and
        the server's upsert on id would keep only one of them."""
        silence = timedelta(seconds=self.max_silence_s())
        if floor is not None and floor > now:
            # The wall clock stepped back past the accounted instant. Clamping
            # to that floor would lift every instant to it and end each span
            # where it starts (zero duration, all at one frozen instant, #302
            # round-2 review). The floor says nothing about this timeline, so
            # stamp as if nothing were accounted: [now - pad, now] at least,
            # the shape the emitter always produced before this rule.
            floor = None
        last_seg = self._segments[-1] if self._segments else None
        # Clamping is monotonic and segment starts strictly increase, so
        # segments sharing a clamped first count are always adjacent.
        groups: list[tuple[datetime, list[_Segment]]] = []
        for seg in self._segments:
            if not any(seg.counts()):
                continue
            first = _clamp(seg.start, floor, now)
            if groups and groups[-1][0] == first:
                groups[-1][1].append(seg)
            else:
                groups.append((first, [seg]))
        out: list[StampedSpan] = []
        prev_end = floor
        for i, (first, segs) in enumerate(groups):
            last = max(_clamp(seg.last, floor, now) for seg in segs)
            # The first span continues the caller's accounted range unless a
            # silence separates them.
            bridged = i == 0 and floor is not None and first - floor <= silence
            start = floor if bridged else first
            key = start
            is_open = segs[-1] is last_seg and now - last <= silence
            end = now if is_open else min(now, last + self._pad)
            if end - start < self._pad:
                start = end - self._pad
                if prev_end is not None and start < prev_end:
                    start = prev_end
            if prev_end is not None and start < prev_end:
                start = prev_end
            if end < start:
                end = start
            parts = tuple((seg, seg.counts()) for seg in segs)
            counts = tuple(sum(c[k] for _, c in parts) for k in range(3))
            out.append(StampedSpan(start=start, end=end, key=key,
                                   counts=counts, _parts=parts))
            prev_end = end
        return out

    def consume(self, spans: list[StampedSpan], taken_at: datetime) -> None:
        """Remove the counts of ``spans`` (an earlier ``stamp``) once they have
        been delivered. Counts recorded after that stamp stay held, and the
        segment they landed in restarts no earlier than ``taken_at``, since the
        delivered span already covered everything before it. A span whose
        segment is already gone (a second, concurrent consumer) is skipped."""
        for span in spans:
            for seg, (p, c, s) in span._parts:
                if not any(x is seg for x in self._segments):
                    continue
                seg.presses = max(0, seg.presses - p)
                seg.clicks = max(0, seg.clicks - c)
                seg.scrolls = max(0, seg.scrolls - s)
                if any(seg.counts()):
                    if seg.start < taken_at:
                        seg.start = min(taken_at, seg.last)
                else:
                    self._segments = [x for x in self._segments if x is not seg]


def _clamp(t: datetime, lo: Optional[datetime], hi: datetime) -> datetime:
    if lo is not None and t < lo:
        t = lo
    if t > hi:
        t = hi
    return t
