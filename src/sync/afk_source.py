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
