"""In-process window source: builds the per-app active-window stream from the
OS frontmost-window probe (+ psutil process name), replacing the external
bf-window-tracker bucket on machines where its Win32 capture goes blind.

Mirrors ``AfkSource``: a retention-pruned deque of samples under a lock, a
sticky ``available()`` latch, a single-source-of-truth ``bucket_id``, and a
``build_window_events(range_start, range_end)`` reconstructor. Ships dormant
(``SyncSettings.in_process_window`` defaults False) — opt-in per the AFK
convergence playbook."""

import logging
import threading
from collections import deque
from datetime import datetime, timedelta
from typing import Callable, Optional

try:
    from .aw_client import BUCKET_TYPE_WINDOW
    from .foreground_activity import _default_pid_getter
except ImportError:  # PyInstaller bundle (src/ is import root)
    from sync.aw_client import BUCKET_TYPE_WINDOW
    from sync.foreground_activity import _default_pid_getter

logger = logging.getLogger(__name__)

# Sentinel: distinguishes "caller didn't pass a getter" (use the platform
# default) from "caller passed None" (explicitly unsupported — e.g. a test
# simulating a platform with no frontmost probe).
_UNSET = object()


def _default_foreground_getter() -> Optional[Callable[[], Optional[tuple[str, str]]]]:
    """Return a getter yielding (app_name, title) for the frontmost window, or
    None when the platform has no frontmost-pid probe.

    Reuses the same platform probes the foreground-CPU detector uses. On
    macOS the probe's app label is already the localized application name; on
    Windows/Linux the probe returns the window TITLE, so we resolve the real
    process name via psutil (the app) and keep the probe string as the title.
    A focus we can't read at all yields None (a gap — never an invented app)."""
    pid_getter = _default_pid_getter()
    if pid_getter is None:
        return None

    def getter() -> Optional[tuple[str, str]]:
        pid, label = pid_getter()
        if pid is None:
            return None
        # macOS: the probe already hands back the app's localized name; there is
        # no separate window title from this probe, so reuse it for both.
        import sys

        if sys.platform == "darwin":
            app = str(label or "").strip()
            if not app:
                return None
            return app, app
        # Windows/Linux: label is the window title; the app is the process name.
        try:
            import psutil

            app = psutil.Process(pid).name()
        except Exception as e:  # process gone / psutil error / import failure
            logger.debug("WindowSource process-name lookup failed: %s", e)
            return None
        app = str(app or "").strip()
        if not app:
            return None
        return app, str(label or "")

    return getter


class WindowSource:
    """Records frontmost-window samples and reconstructs per-app focus spans."""

    def __init__(
        self,
        hostname: str,
        *,
        foreground_getter=_UNSET,
        retention_seconds: float = 7200.0,
        max_samples: int = 20000,
    ) -> None:
        self._hostname = hostname
        # Not passed -> platform default probe. Passed as None -> unsupported
        # platform (no frontmost probe): the source stays permanently
        # unavailable, exactly like AfkSource on Linux.
        self._getter: Optional[Callable[[], Optional[tuple[str, str]]]] = (
            _default_foreground_getter() if foreground_getter is _UNSET
            else foreground_getter
        )
        self._retention_seconds = float(retention_seconds)
        self._retention = timedelta(seconds=retention_seconds)
        self._lock = threading.Lock()
        # (sample_time, app, title). `maxlen` is an absolute memory backstop
        # mirroring AfkSource: ~20k samples ≈ 2 weeks at one per cycle; appends
        # auto-evict the oldest beyond that.
        self._samples: deque = deque(maxlen=max_samples)
        # Sticky platform-capability latch: a transient probe failure (a focus we
        # momentarily can't read) must NOT revoke in-process mode for a cycle —
        # that would hand per-app coverage back to the external tracker and flap.
        # Once a read has ever succeeded the platform HAS the capability, so we
        # stay latched on (mirrors AfkSource audit finding A).
        self._available_latched = False
        # Consecutive probe failures, for blind-probe visibility. Reset to 0 on
        # any successful read.
        self._consecutive_failures = 0

    @property
    def samples(self) -> list:
        with self._lock:
            return list(self._samples)

    @property
    def retention_seconds(self) -> float:
        return self._retention_seconds

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def bucket_id(self) -> str:
        """The synthetic window bucket id this source uploads under. Single
        source of truth so the upload (_event) and the checkpoint-commit gate
        agree."""
        return f"bf-window-inproc_{self._hostname}"

    def available(self) -> bool:
        """True when the frontmost-window probe is (or has ever been) usable on
        this platform. Sticky: once a read succeeds it stays True, so a transient
        failure cannot flap in-process window capture off for a cycle. False
        forever on a platform with no probe (getter is None)."""
        if self._available_latched:
            return True
        if self._getter is None:
            return False
        try:
            ok = self._getter() is not None
        except Exception:
            ok = False
        if ok:
            self._available_latched = True
        return ok

    def record_sample(self, now: datetime) -> None:
        """Observe the frontmost window at ``now`` and append (now, app, title).

        A focus we can't read (getter returns None or a blank app) is a GAP: we
        append nothing rather than invent an app, so an unreadable moment cannot
        fabricate window time. No-op when the probe is unsupported."""
        if self._getter is None:
            return
        try:
            result = self._getter()
        except Exception as e:
            logger.debug("WindowSource foreground getter failed: %s", e)
            self._consecutive_failures += 1
            return
        if result is None:
            self._consecutive_failures += 1
            return
        app, title = result
        app = str(app or "").strip()
        if not app:
            # Readable focus but no resolvable app — treat as a gap, never invent.
            self._consecutive_failures += 1
            return
        self._consecutive_failures = 0
        title = str(title or "")
        with self._lock:
            self._samples.append((now, app, title))
            cutoff = now - self._retention
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()

    def build_window_events(self, range_start: datetime, range_end: datetime) -> list:
        """Reconstruct per-app focus spans over [range_start, range_end].

        Each maximal run of CONTIGUOUS same-app samples becomes ONE window event
        spanning [first_sample_time, next_different_sample_time_or_range_end].
        The representative title is the one held longest within the run. A change
        of app closes the current span and opens a new one. Spans are clamped to
        [range_start, range_end]; zero/negative-duration events are dropped (we
        never invent time for a focus we couldn't observe)."""
        if range_end <= range_start:
            return []
        with self._lock:
            samples = [
                (t, app, title) for (t, app, title) in self._samples
                if range_start <= t < range_end
            ]
        if not samples:
            return []
        samples.sort(key=lambda s: s[0])

        events: list = []
        run_start = samples[0][0]
        run_app = samples[0][1]
        # title -> total held seconds within the current run, to pick the
        # longest-held representative title.
        title_held: dict[str, float] = {}
        prev_time = samples[0][0]
        prev_title = samples[0][2]

        def _close(end: datetime) -> None:
            # Credit the final observed title's tail up to the span end.
            title_held[prev_title] = (
                title_held.get(prev_title, 0.0) + (end - prev_time).total_seconds()
            )
            title = max(title_held, key=lambda k: title_held[k]) if title_held else ""
            self._append_event(events, run_start, end, run_app, title, range_start, range_end)

        for t, app, title in samples[1:]:
            if app != run_app:
                # App changed: close the run at this instant, open a new one.
                _close(t)
                run_start = t
                run_app = app
                title_held = {}
                prev_time = t
                prev_title = title
                continue
            # Same app: accrue the just-elapsed interval to the previously-held
            # title, then advance the cursor.
            title_held[prev_title] = (
                title_held.get(prev_title, 0.0) + (t - prev_time).total_seconds()
            )
            prev_time = t
            prev_title = title
        # Close the trailing run at range_end.
        _close(range_end)
        return events

    def _append_event(
        self, events: list, start: datetime, end: datetime, app: str, title: str,
        range_start: datetime, range_end: datetime,
    ) -> None:
        s = max(start, range_start)
        e = min(end, range_end)
        if e <= s:
            return
        events.append(self._event(s, (e - s).total_seconds(), app, title))

    def _event(self, start: datetime, duration: float, app: str, title: str) -> dict:
        return {
            # Millisecond precision: two spans can legitimately start within the
            # same whole second (rapid app switches at a fine cadence); a
            # second-truncated id would collide and the server upsert would drop
            # one, under-counting (mirrors AfkSource audit finding F).
            "id": f"win-inproc_{self._hostname}_{int(start.timestamp() * 1000)}",
            "timestamp": start.isoformat(),
            "duration": round(duration, 2),
            "bucket_id": self.bucket_id,
            "bucket_type": BUCKET_TYPE_WINDOW,
            "data": {"app": app, "title": title},
        }
