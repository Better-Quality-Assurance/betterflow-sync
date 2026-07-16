"""In-process AFK source: builds the authoritative active/idle stream from the
OS idle clock (+ macOS input watcher), replacing the external bf-idle-tracker
bucket. See docs/superpowers/specs/2026-06-19-in-process-afk-source-design.md."""

import logging
import threading
from collections import deque
from datetime import datetime, timedelta
from typing import Callable, Optional

try:
    from .aw_client import BUCKET_TYPE_AFK
    from .os_idle import get_system_idle_seconds
except ImportError:  # PyInstaller bundle (src/ is import root)
    from sync.aw_client import BUCKET_TYPE_AFK
    from sync.os_idle import get_system_idle_seconds

logger = logging.getLogger(__name__)


class AfkSource:
    """Records activity samples and reconstructs AFK spans from them."""

    def __init__(
        self,
        afk_timeout_seconds: float,
        hostname: str,
        *,
        input_watcher=None,
        activity_sources: Optional[list] = None,
        idle_clock: Callable[[], Optional[float]] = get_system_idle_seconds,
        retention_seconds: float = 7200.0,
        max_samples: int = 20000,
    ) -> None:
        self._afk_timeout = float(afk_timeout_seconds)
        self._hostname = hostname
        self._input_watcher = input_watcher
        # Supplementary non-input activity sources (e.g. foreground-CPU). Each
        # exposes ``get_last_active_at(base_last_input, now) -> Optional[datetime]``
        # and can only ever move last_input_at FORWARD (toward now), never back —
        # so a buggy source can over-credit at most up to ``now``, never strand
        # real idle as active beyond it.
        self._activity_sources = list(activity_sources or [])
        self._idle_clock = idle_clock
        self._retention_seconds = float(retention_seconds)
        self._retention = timedelta(seconds=retention_seconds)
        self._lock = threading.Lock()
        # (sample_time, last_input_at). `maxlen` is an absolute memory backstop:
        # `protect_since` can hold samples past the retention prune (so a long
        # pause's pre-pause tail survives, and a held checkpoint can rebuild),
        # but a pathological frozen checkpoint during a multi-day outage must
        # not grow this without bound. ~20k samples ≈ 2 weeks at one per cycle;
        # appends auto-evict the oldest beyond that (salvage past that horizon is
        # moot anyway).
        self._samples: deque = deque(maxlen=max_samples)
        # Sticky platform-capability latch: a transient idle-clock read failure
        # (e.g. a one-off `ioreg` timeout) must NOT revoke in-process mode for a
        # cycle — that would hand billing back to the external tracker and flap
        # the health telemetry. Once a read has ever succeeded the platform HAS
        # the capability, so we stay latched on (audit finding A).
        self._available_latched = False
        # Consecutive idle-clock read failures, for blind-clock detection (audit
        # finding E). Reset to 0 on any successful read.
        self._consecutive_clock_failures = 0

    @property
    def samples(self) -> list:
        with self._lock:
            return list(self._samples)

    @property
    def retention_seconds(self) -> float:
        return self._retention_seconds

    @property
    def consecutive_clock_failures(self) -> int:
        return self._consecutive_clock_failures

    @property
    def bucket_id(self) -> str:
        """The synthetic AFK bucket id this source uploads under. Single source
        of truth so the upload (_event) and the checkpoint-commit gate agree."""
        return f"bf-afk-inproc_{self._hostname}"

    def available(self) -> bool:
        """True when the OS idle clock is (or has ever been) readable on this
        platform. Sticky: once a read succeeds it stays True, so a transient
        failure cannot flap in-process AFK off for a cycle (audit finding A)."""
        if self._available_latched:
            return True
        try:
            ok = self._idle_clock() is not None
        except Exception:
            ok = False
        if ok:
            self._available_latched = True
        return ok

    def record_sample(self, now: datetime, protect_since: Optional[datetime] = None) -> None:
        """Observe activity at ``now`` and append (now, last_input_at). No-op when
        the OS idle clock is unavailable (Linux).

        ``protect_since`` keeps samples at/after that instant from being pruned
        even when they age past the retention window. The engine passes its
        in-process AFK checkpoint so the active samples taken just before a
        long pause survive the wake-cycle prune and can still be billed —
        otherwise a sleep longer than retention drops the genuine pre-pause
        work as an idle gap (Ecaterina/Matei Cocora, 2026-06-25)."""
        try:
            idle = self._idle_clock()
        except Exception as e:
            logger.debug("AfkSource idle clock failed: %s", e)
            self._consecutive_clock_failures += 1
            return
        if idle is None:
            self._consecutive_clock_failures += 1
            return
        self._consecutive_clock_failures = 0
        last_input_at = self._apply_input_watcher(now - timedelta(seconds=idle))
        # Fold in supplementary activity sources (call, mic, foreground-CPU).
        # Every source receives the same REAL input anchor — the pre-fold
        # keyboard/mouse instant, never another source's credit — so one
        # detector's no-input credit can't become the next one's cap anchor
        # (credit chaining). Each answer may only advance last_input_at toward
        # `now`, clamped here as a hard backstop so no source can ever push
        # activity into the future.
        real_input_anchor = last_input_at
        for src in self._activity_sources:
            try:
                a = src.get_last_active_at(real_input_anchor, now)
            except Exception as e:
                logger.debug("AfkSource activity source failed: %s", e)
                a = None
            if a is not None and last_input_at < a <= now:
                last_input_at = a
        with self._lock:
            self._samples.append((now, last_input_at))
            cutoff = now - self._retention
            while self._samples and self._samples[0][0] < cutoff:
                # Don't prune below a protected floor (the engine's checkpoint):
                # those samples still cover an un-billed span before a pause.
                if protect_since is not None and self._samples[0][0] >= protect_since:
                    break
                self._samples.popleft()

    def _apply_input_watcher(self, last_input_at: datetime) -> datetime:
        """Prefer the in-process input watcher when it reports a *more recent*
        input than the OS idle clock — it holds the main app's Input Monitoring
        grant, so it sees keystrokes the separate-TCC bf-idle-tracker can miss."""
        watcher = self._input_watcher
        if watcher is None:
            return last_input_at
        try:
            wli = watcher.get_last_input_at()
        except Exception as e:
            logger.debug("AfkSource input watcher failed: %s", e)
            return last_input_at
        if wli is not None and wli > last_input_at:
            return wli
        return last_input_at

    def base_last_input_at(self, now: datetime) -> Optional[datetime]:
        """The real keyboard/mouse input anchor (OS idle clock + in-process input
        watcher), WITHOUT supplementary activity sources. None when the OS idle
        clock is unreadable (Linux). Activity sources use this as their
        human-presence anchor; reading it here doesn't touch the failure counter
        (a no-op read must not flap in-process AFK off)."""
        try:
            idle = self._idle_clock()
        except Exception as e:
            logger.debug("AfkSource base_last_input_at idle clock failed: %s", e)
            return None
        if idle is None:
            return None
        return self._apply_input_watcher(now - timedelta(seconds=idle))

    def finalize_point(self, now: datetime) -> datetime:
        """The latest instant whose afk classification is FINAL.

        While the user is within the timeout of their last input, the trailing
        region [last_input, now] could still resolve either way — they may return
        (making it not-afk) or stay idle past the timeout (making the whole region
        afk, backdated). So we finalize only up to the last confirmed input. Once
        idle >= timeout, the trailing region is definitively afk and we finalize
        all the way to `now`. With no samples there is nothing to defer."""
        with self._lock:
            last_input = max((li for (_, li) in self._samples), default=None)
        if last_input is None:
            return now
        if (now - last_input).total_seconds() >= self._afk_timeout:
            return now
        return last_input

    def build_afk_events(
        self, range_start: datetime, range_end: datetime,
        project_id: Optional[int] = None,
        cover_unsampled: bool = True,
    ) -> list:
        """Reconstruct AFK spans over [range_start, range_end] from samples.

        Activity is known only at each sample's last_input_at. Between two
        activity instants the user is not-afk until last_input + afk_timeout,
        then afk (aw-watcher-afk parity). Any sub-range with no covering sample
        is afk (never invent activity).

        With ``cover_unsampled=False`` the unsampled boundary regions (a leading
        gap before the first observed instant, and a trailing gap past the last
        one that already exceeds the timeout) are LEFT OUT instead of filled
        with afk. Used by the long-pause re-seed path to salvage only the spans
        the retained samples genuinely cover, leaving the unobserved sleep
        window uncovered rather than reconstructing it."""
        if range_end <= range_start:
            return []
        timeout = timedelta(seconds=self._afk_timeout)
        with self._lock:
            instants = sorted({li for (_, li) in self._samples})

        anchor = None
        for li in instants:
            if li <= range_start:
                anchor = li  # newest instant at/before range_start
        in_range = [li for li in instants if range_start < li < range_end]
        activity = ([anchor] if anchor is not None else []) + in_range

        # NO GRACE (verified against live aw-watcher-afk 2026-06-19): afk is
        # backdated to the last input. A gap between two inputs is not-afk only if
        # it never reached the timeout; otherwise the ENTIRE gap is afk. The
        # afk_timeout governs only how long real-time detection waits before
        # declaring afk, not how the billed span is classified.
        spans: list = []
        if not activity:
            if cover_unsampled:
                spans.append((range_start, range_end, "afk"))
        else:
            first = activity[0]
            # `first > range_start` also covers anchor == range_start (checkpoint
            # set exactly to a last_input): no leading gap, so skip the span.
            if first > range_start and cover_unsampled:
                spans.append((range_start, first, "afk"))  # leading unknown
            for a, b in zip(activity, activity[1:], strict=False):
                spans.append((a, b, "not-afk" if (b - a) <= timeout else "afk"))
            last = activity[-1]
            trailing = "not-afk" if (range_end - last) <= timeout else "afk"
            if trailing == "not-afk" or cover_unsampled:
                spans.append((last, range_end, trailing))

        events: list = []
        for start, end, status in sorted(spans, key=lambda s: s[0]):
            s = max(start, range_start)
            e = min(end, range_end)
            if e <= s:
                continue
            events.append(self._event(s, (e - s).total_seconds(), status, project_id))
        return events

    def _event(self, start: datetime, duration: float, status: str,
               project_id: Optional[int]) -> dict:
        ev = {
            # Millisecond precision: two spans can legitimately start within the
            # same whole second (an activity instant and a last_input+timeout
            # boundary <1s apart); a second-truncated id would collide and the
            # server upsert would drop one, under-billing (audit finding F).
            "id": f"afk-inproc_{self._hostname}_{int(start.timestamp() * 1000)}",
            "timestamp": start.isoformat(),
            "duration": round(duration, 2),
            "bucket_id": self.bucket_id,
            "bucket_type": BUCKET_TYPE_AFK,
            "data": {"status": status, "synthetic": True},
        }
        if project_id is not None:
            ev["project_id"] = project_id
        return ev
