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
        idle_clock: Callable[[], Optional[float]] = get_system_idle_seconds,
        retention_seconds: float = 7200.0,
    ) -> None:
        self._afk_timeout = float(afk_timeout_seconds)
        self._hostname = hostname
        self._input_watcher = input_watcher
        self._idle_clock = idle_clock
        self._retention = timedelta(seconds=retention_seconds)
        self._lock = threading.Lock()
        # (sample_time, last_input_at)
        self._samples: deque = deque()

    @property
    def samples(self) -> list:
        with self._lock:
            return list(self._samples)

    def available(self) -> bool:
        """True when the OS idle clock is readable on this platform."""
        try:
            return self._idle_clock() is not None
        except Exception:
            return False

    def record_sample(self, now: datetime) -> None:
        """Observe activity at ``now`` and append (now, last_input_at). No-op when
        the OS idle clock is unavailable (Linux)."""
        try:
            idle = self._idle_clock()
        except Exception as e:
            logger.debug("AfkSource idle clock failed: %s", e)
            return
        if idle is None:
            return
        last_input_at = now - timedelta(seconds=idle)
        # macOS in-process watcher holds the main app's grant — prefer it when
        # it reports a *more recent* input than the OS idle clock.
        watcher = self._input_watcher
        if watcher is not None:
            try:
                wli = watcher.get_last_input_at()
            except Exception as e:
                logger.debug("AfkSource input watcher failed: %s", e)
                wli = None
            if wli is not None and wli > last_input_at:
                last_input_at = wli
        with self._lock:
            self._samples.append((now, last_input_at))
            cutoff = now - self._retention
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()

    def build_afk_events(
        self, range_start: datetime, range_end: datetime,
        project_id: Optional[int] = None,
    ) -> list:
        """Reconstruct AFK spans over [range_start, range_end] from samples.

        Activity is known only at each sample's last_input_at. Between two
        activity instants the user is not-afk until last_input + afk_timeout,
        then afk (aw-watcher-afk parity). Any sub-range with no covering sample
        is afk (never invent activity)."""
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

        spans: list = []
        if not activity:
            spans.append((range_start, range_end, "afk"))
        else:
            first = activity[0]
            if first > range_start:
                spans.append((range_start, first, "afk"))  # leading unknown
            for a, b in zip(activity, activity[1:], strict=False):
                spans.append((a, min(b, a + timeout), "not-afk"))
                if b - a > timeout:
                    spans.append((a + timeout, b, "afk"))
            last = activity[-1]
            spans.append((last, min(range_end, last + timeout), "not-afk"))
            if range_end - last > timeout:
                spans.append((last + timeout, range_end, "afk"))

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
            "id": f"afk-inproc_{self._hostname}_{int(start.timestamp())}",
            "timestamp": start.isoformat(),
            "duration": round(duration, 2),
            "bucket_id": f"bf-afk-inproc_{self._hostname}",
            "bucket_type": BUCKET_TYPE_AFK,
            "data": {"status": status, "synthetic": True},
        }
        if project_id is not None:
            ev["project_id"] = project_id
        return ev
