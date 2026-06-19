# In-process AFK source — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the agent generate the authoritative active/idle (AFK) stream in-process from the OS idle clock (+ macOS input watcher) and upload it as the sole AFK source, removing correctness-dependence on the external `bf-idle-tracker`.

**Architecture:** A new pure-logic `AfkSource` turns a log of activity samples into AFK spans. `SyncEngine` records a sample each cycle and, behind a default-on kill-switch flag, uploads the in-process spans instead of the external AFK bucket (Linux / flag-off keeps today's external-bucket + #67 path). `aw_manager` stops alerting on the now-ignored tracker.

**Tech Stack:** Python 3.11, pytest, dataclasses, stdlib `datetime`/`collections.deque`/`threading`.

## Global Constraints

- afk transition occurs at `last_input_at + afk_timeout` (aw-watcher-afk parity — first `afk_timeout` seconds of any pause stay billed active). Copied from spec; the single most important invariant.
- Never invent activity: any sub-range with no covering sample is emitted as `afk`.
- Default flag value `Config.sync.in_process_afk = True`; it is a kill-switch, not a staging gate.
- In-process path active only where `os_idle.get_system_idle_seconds()` returns a value (macOS/Windows); Linux always uses the external path.
- 4-space indent, snake_case, follow existing `sync/` module patterns. Imports use the `try: from .x import ... except ImportError: from x import ...` dual form already in `sync_engine.py`.
- Event dict shape: `{"id","timestamp","duration","bucket_id","bucket_type","data":{"status","synthetic":True}[,"project_id"]}`; `bucket_type = BUCKET_TYPE_AFK`.
- Per fixture discipline: every behavior test must fail on pre-change code and pass after; commit the proof.

---

### Task 1: Config kill-switch flag

**Files:**
- Modify: `src/config.py:294-302` (`SyncSettings`)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config().sync.in_process_afk: bool` (default `True`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py  (add)
def test_in_process_afk_defaults_on():
    from src.config import Config
    assert Config().sync.in_process_afk is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py::test_in_process_afk_defaults_on -v`
Expected: FAIL — `AttributeError: 'SyncSettings' object has no attribute 'in_process_afk'`

- [ ] **Step 3: Add the field**

```python
# src/config.py — inside SyncSettings, after min_window_event_seconds
    # Generate the AFK/active stream in-process from the OS idle clock + input
    # watcher instead of the external bf-idle-tracker bucket. Kill-switch: set
    # False to fall back to the external bucket + stale-synthesis path.
    in_process_afk: bool = True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py::test_in_process_afk_defaults_on -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/config.py tests/test_config.py
git commit -m "feat(config): add in_process_afk kill-switch (default on)"
```

---

### Task 2: `AfkSource` — construction, `available()`, `record_sample()`

**Files:**
- Create: `src/sync/afk_source.py`
- Test: `tests/test_afk_source.py`

**Interfaces:**
- Consumes: `os_idle.get_system_idle_seconds` (existing, returns `Optional[float]`).
- Produces:
  - `AfkSource(afk_timeout_seconds: float, hostname: str, *, input_watcher=None, idle_clock=get_system_idle_seconds, retention_seconds: float = 7200.0)`
  - `.available() -> bool`
  - `.record_sample(now: datetime) -> None`
  - `.samples` (read-only list copy, for tests)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_afk_source.py
from datetime import datetime, timedelta, timezone

from src.sync.afk_source import AfkSource

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


def _src(idle_value, **kw):
    return AfkSource(afk_timeout_seconds=600, hostname="host",
                     idle_clock=lambda: idle_value, **kw)


def test_available_true_when_idle_clock_returns_value():
    assert _src(5.0).available() is True


def test_available_false_when_idle_clock_returns_none():
    assert _src(None).available() is False


def test_record_sample_appends_last_input_from_idle_clock():
    src = _src(30.0)
    src.record_sample(T0)
    assert src.samples == [(T0, T0 - timedelta(seconds=30))]


def test_record_sample_noop_when_clock_unavailable():
    src = _src(None)
    src.record_sample(T0)
    assert src.samples == []


def test_record_sample_prefers_fresher_input_watcher():
    class W:
        def get_last_input_at(self):
            return T0 - timedelta(seconds=2)  # fresher than idle clock's 30s
    src = _src(30.0, input_watcher=W())
    src.record_sample(T0)
    assert src.samples == [(T0, T0 - timedelta(seconds=2))]


def test_retention_prunes_old_samples():
    src = _src(0.0, retention_seconds=100)
    src.record_sample(T0)
    src.record_sample(T0 + timedelta(seconds=200))  # T0 now older than retention
    assert [s[0] for s in src.samples] == [T0 + timedelta(seconds=200)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_afk_source.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.sync.afk_source'`

- [ ] **Step 3: Implement construction + sampling**

```python
# src/sync/afk_source.py
"""In-process AFK source: builds the authoritative active/idle stream from the
OS idle clock (+ macOS input watcher), replacing the external bf-idle-tracker
bucket. See docs/superpowers/specs/2026-06-19-in-process-afk-source-design.md."""

import logging
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_afk_source.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add src/sync/afk_source.py tests/test_afk_source.py
git commit -m "feat(afk): AfkSource sampler (record_sample + available)"
```

---

### Task 3: `AfkSource.build_afk_events` — timeline reconstruction

**Files:**
- Modify: `src/sync/afk_source.py` (add method)
- Test: `tests/test_afk_source.py` (add cases)

**Interfaces:**
- Produces: `.build_afk_events(range_start: datetime, range_end: datetime, project_id: Optional[int] = None) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_afk_source.py  (add)
def _seed(src, *pairs):
    """pairs: (sample_time, last_input_at)."""
    for st, li in pairs:
        with src._lock:
            src._samples.append((st, li))


def _spans(events):
    return [(e["data"]["status"], e["timestamp"],
             round(e["duration"])) for e in events]


def test_continuous_activity_is_one_notafk_span():
    src = _src(0.0)
    _seed(src, (T0, T0), (T0 + timedelta(seconds=30), T0 + timedelta(seconds=30)))
    ev = src.build_afk_events(T0, T0 + timedelta(seconds=30))
    assert _spans(ev) == [("not-afk", T0.isoformat(), 30)]


def test_idle_past_timeout_flips_at_last_input_plus_timeout():
    # last input at T0; range runs to T0+700. afk must start at T0+600, NOT T0.
    src = _src(700.0)
    _seed(src, (T0, T0), (T0 + timedelta(seconds=700), T0))
    ev = src.build_afk_events(T0, T0 + timedelta(seconds=700))
    assert _spans(ev) == [
        ("not-afk", T0.isoformat(), 600),
        ("afk", (T0 + timedelta(seconds=600)).isoformat(), 100),
    ]


def test_pause_shorter_than_timeout_stays_active():
    src = _src(599.0)
    _seed(src, (T0, T0), (T0 + timedelta(seconds=599), T0))
    ev = src.build_afk_events(T0, T0 + timedelta(seconds=599))
    assert _spans(ev) == [("not-afk", T0.isoformat(), 599)]


def test_no_samples_in_range_is_afk():
    src = _src(0.0)  # empty log
    ev = src.build_afk_events(T0, T0 + timedelta(seconds=300))
    assert _spans(ev) == [("afk", T0.isoformat(), 300)]


def test_gap_with_no_samples_billed_afk_not_active():
    # active at T0, then a 1h hole (machine asleep), active again at T0+3600.
    src = _src(0.0)
    _seed(src, (T0, T0), (T0 + timedelta(seconds=3600), T0 + timedelta(seconds=3600)))
    ev = src.build_afk_events(T0, T0 + timedelta(seconds=3600))
    statuses = [e["data"]["status"] for e in ev]
    assert "not-afk" in statuses and "afk" in statuses
    # the long middle is afk; total not-afk never exceeds 2*timeout (grace at each end)
    notafk = sum(e["duration"] for e in ev if e["data"]["status"] == "not-afk")
    assert notafk <= 1200


def test_empty_range_returns_nothing():
    src = _src(0.0)
    assert src.build_afk_events(T0, T0) == []


def test_project_id_attached_when_given():
    src = _src(0.0)
    _seed(src, (T0, T0))
    ev = src.build_afk_events(T0, T0 + timedelta(seconds=10), project_id=42)
    assert all(e["project_id"] == 42 for e in ev)


def test_consecutive_cycles_are_contiguous_and_non_overlapping():
    src = _src(0.0)
    _seed(src, (T0, T0), (T0 + timedelta(seconds=30), T0 + timedelta(seconds=30)),
          (T0 + timedelta(seconds=60), T0 + timedelta(seconds=60)))
    a = src.build_afk_events(T0, T0 + timedelta(seconds=30))
    b = src.build_afk_events(T0 + timedelta(seconds=30), T0 + timedelta(seconds=60))
    a_end = datetime.fromisoformat(a[-1]["timestamp"]) + timedelta(seconds=a[-1]["duration"])
    b_start = datetime.fromisoformat(b[0]["timestamp"])
    assert a_end == b_start  # contiguous, no gap, no overlap
    assert a[-1]["id"] != b[0]["id"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_afk_source.py -k build_afk -v`
Expected: FAIL — `AttributeError: 'AfkSource' object has no attribute 'build_afk_events'`

- [ ] **Step 3: Implement the timeline**

```python
# src/sync/afk_source.py — add to AfkSource

    def build_afk_events(
        self, range_start: datetime, range_end: datetime,
        project_id: Optional[int] = None,
    ) -> list[dict]:
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

        spans: list[tuple] = []
        if not activity:
            spans.append((range_start, range_end, "afk"))
        else:
            first = activity[0]
            if first > range_start:
                spans.append((range_start, first, "afk"))  # leading unknown
            for a, b in zip(activity, activity[1:]):
                spans.append((a, min(b, a + timeout), "not-afk"))
                if b - a > timeout:
                    spans.append((a + timeout, b, "afk"))
            last = activity[-1]
            spans.append((last, min(range_end, last + timeout), "not-afk"))
            if range_end - last > timeout:
                spans.append((last + timeout, range_end, "afk"))

        events: list[dict] = []
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_afk_source.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/sync/afk_source.py tests/test_afk_source.py
git commit -m "feat(afk): build_afk_events timeline (aw-watcher-afk parity, never over-bill)"
```

---

### Task 4: SyncEngine integration

**Files:**
- Modify: `src/sync/sync_engine.py` (constructor: `self._afk_source=None`, `self._afk_inproc_checkpoint=None`; `sync()` AFK branch ~line 521-539)
- Test: `tests/test_sync_engine_inproc_afk.py`

**Interfaces:**
- Consumes: `AfkSource` (Task 2/3), `Config().sync.in_process_afk` (Task 1).
- Produces: `SyncEngine.afk_source` attribute (settable post-construction, like `health_provider`); when set + flag on + `available()`, `sync()` uploads in-process AFK and skips the external bucket + `_synthesize_for_stale_afk`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sync_engine_inproc_afk.py
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from src.config import Config
from src.sync.afk_source import AfkSource
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.sync_engine import SyncEngine


def _engine(in_process_afk: bool):
    cfg = Config()
    cfg.sync.in_process_afk = in_process_afk
    return SyncEngine(aw=Mock(), bf=Mock(), queue=Mock(), config=cfg,
                      activity_analyzer=Mock(spec=ActivityAnalyzer),
                      time_tracker=Mock(spec=DailyTimeTracker))


def test_inproc_afk_events_built_for_uploaded_range():
    eng = _engine(True)
    eng.afk_source = AfkSource(600, "host", idle_clock=lambda: 5.0)
    now = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)
    eng.afk_source.record_sample(now - timedelta(seconds=30))
    eng.afk_source.record_sample(now)
    events = eng._build_inproc_afk(now)  # helper under test
    assert events and all(e["bucket_id"] == "bf-afk-inproc_host" for e in events)


def test_external_afk_bucket_skipped_when_inproc_active():
    eng = _engine(True)
    eng.afk_source = AfkSource(600, "host", idle_clock=lambda: 5.0)
    assert eng._should_skip_external_afk() is True


def test_external_afk_bucket_used_when_flag_off():
    eng = _engine(False)
    eng.afk_source = AfkSource(600, "host", idle_clock=lambda: 5.0)
    assert eng._should_skip_external_afk() is False


def test_inproc_inactive_when_clock_unavailable():
    eng = _engine(True)
    eng.afk_source = AfkSource(600, "host", idle_clock=lambda: None)  # Linux
    assert eng._should_skip_external_afk() is False


def test_inproc_inactive_when_no_source_wired():
    eng = _engine(True)
    assert eng._should_skip_external_afk() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_sync_engine_inproc_afk.py -v`
Expected: FAIL — `AttributeError: 'SyncEngine' object has no attribute '_should_skip_external_afk'`

- [ ] **Step 3: Implement constructor fields + helpers + sync() branch**

```python
# src/sync/sync_engine.py — in __init__, near self.health_provider = None
        self.afk_source = None  # AfkSource; set post-construction by the app
        self._afk_inproc_checkpoint: Optional[datetime] = None
```

```python
# src/sync/sync_engine.py — add methods near _synthesize_for_stale_afk
    def _inproc_afk_active(self) -> bool:
        """True when the in-process AFK source should be the sole source."""
        return (
            bool(self.config.sync.in_process_afk)
            and self.afk_source is not None
            and self.afk_source.available()
        )

    def _should_skip_external_afk(self) -> bool:
        return self._inproc_afk_active()

    def _build_inproc_afk(self, now: datetime) -> list[dict]:
        """Build the in-process AFK events for [checkpoint, now] and advance the
        checkpoint. Returns [] when not active."""
        if not self._inproc_afk_active():
            return []
        if self._afk_inproc_checkpoint is None:
            self._afk_inproc_checkpoint = now  # account only while running
            return []
        with self._state_lock:
            project = self._current_project
        events = self.afk_source.build_afk_events(
            self._afk_inproc_checkpoint, now,
            project_id=project["id"] if project else None,
        )
        self._afk_inproc_checkpoint = now
        return events
```

```python
# src/sync/sync_engine.py — at the very top of sync()'s real work (after the
# paused/private guard returns, before fetching buckets ~line 472), sample:
        if self.afk_source is not None:
            try:
                self.afk_source.record_sample(datetime.now(timezone.utc))
            except Exception as e:
                logger.debug("afk_source.record_sample failed: %s", e)
```

```python
# src/sync/sync_engine.py — replace the non-window bucket loop + synth block
# (currently ~line 521-539). Skip external AFK buckets when in-process is active,
# and emit in-process events instead of _synthesize_for_stale_afk.
        skip_afk = self._should_skip_external_afk()
        for bucket in web_buckets + afk_buckets + input_buckets:
            if skip_afk and _is_afk_like(bucket.type):
                continue
            try:
                events, checkpoint = self._sync_bucket(bucket.id, bucket.type, stats, cycle)
                all_events.extend(events)
                if checkpoint:
                    pending_checkpoints.append(checkpoint)
                stats.buckets_synced += 1
            except AWClientError as e:
                stats.errors.append(f"Failed to sync bucket {bucket.id}: {e}")

        if skip_afk:
            all_events.extend(self._build_inproc_afk(datetime.now(timezone.utc)))
        else:
            synth_afk = self._synthesize_for_stale_afk(afk_buckets)
            if synth_afk:
                all_events.append(synth_afk)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_sync_engine_inproc_afk.py tests/test_sync_engine_synth_afk.py -v`
Expected: PASS (synth tests still pass — that path is unchanged when flag off / no source)

- [ ] **Step 5: Run the full sync-engine suite for regressions**

Run: `python -m pytest tests/test_sync_engine.py tests/test_backlog_reconcile_enqueues_day.py -q`
Expected: PASS (backlog reconcile untouched — it never sees `_afk_inproc_checkpoint`)

- [ ] **Step 6: Commit**

```bash
git add src/sync/sync_engine.py tests/test_sync_engine_inproc_afk.py
git commit -m "feat(sync): upload in-process AFK as sole source when enabled"
```

---

### Task 5: aw_manager — suppress idle stale/blind alerting when in-process active

**Files:**
- Modify: `src/aw_manager.py` (`__init__` flag + setter; gate the idle stale path in `_restart_if_needed_locked` ~line 688-695)
- Test: `tests/test_aw_manager_idle_restart.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `AWManager.set_inproc_afk_active(active: bool)`; when active, the `bf-idle-tracker` stale path is skipped (no restart, no `_idle_stale_restart_count`/blind escalation). Window tracker path unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_aw_manager_idle_restart.py  (add)
def test_inproc_afk_suppresses_idle_tracker_restart():
    mgr, idle = _make_manager(afk_age=1800, window_age=5)  # would normally restart
    mgr.set_inproc_afk_active(True)

    mgr.restart_if_needed()

    assert not idle.terminate.called, "ignored tracker must not be restarted"
    assert mgr._idle_stale_restart_count == 0
    assert mgr.idle_tracker_blind is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_aw_manager_idle_restart.py::test_inproc_afk_suppresses_idle_tracker_restart -v`
Expected: FAIL — `AttributeError: 'AWManager' object has no attribute 'set_inproc_afk_active'`

- [ ] **Step 3: Implement the flag + gate**

```python
# src/aw_manager.py — in __init__, near self._idle_tracker_blind = False
        # When the agent uploads its own in-process AFK stream, the external
        # bf-idle-tracker bucket is ignored — don't restart it or raise blind
        # alerts about a tracker we no longer consume.
        self._inproc_afk_active: bool = False
```

```python
# src/aw_manager.py — add method near idle_tracker_blind property
    def set_inproc_afk_active(self, active: bool) -> None:
        with self._lifecycle_lock:
            self._inproc_afk_active = bool(active)
```

```python
# src/aw_manager.py — in _restart_if_needed_locked, gate the idle-tracker block.
# Change the existing guard (currently ~line 682):
#     idle_watcher = "bf-idle-tracker"
#     if (idle_watcher not in self._disabled_components and ...):
# to also require not self._inproc_afk_active:
        idle_watcher = "bf-idle-tracker"
        if (
            not self._inproc_afk_active
            and idle_watcher not in self._disabled_components
            and idle_watcher in self._processes
            and self._processes[idle_watcher].poll() is None
        ):
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_aw_manager_idle_restart.py tests/test_aw_manager_health_snapshot.py -v`
Expected: PASS (existing restart tests unaffected — default `_inproc_afk_active` is False)

- [ ] **Step 5: Commit**

```bash
git add src/aw_manager.py tests/test_aw_manager_idle_restart.py
git commit -m "feat(aw): suppress idle-tracker restart/blind alerts when in-process AFK active"
```

---

### Task 6: Wire it together in `main.py`

**Files:**
- Modify: `src/main.py` (construct `AfkSource`, assign `sync_engine.afk_source`, call `aw_manager.set_inproc_afk_active` from config)
- Test: `tests/test_linux_support.py` (Linux forces external path) + manual smoke

**Interfaces:**
- Consumes: `AfkSource`, `SyncEngine.afk_source`, `AWManager.set_inproc_afk_active`, the existing `self._input_watcher` and hostname.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_linux_support.py  (add to the Linux class)
def test_afk_source_unavailable_on_linux(monkeypatch):
    import src.sync.afk_source as afk
    src = afk.AfkSource(600, "host", idle_clock=lambda: None)
    assert src.available() is False
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `python -m pytest tests/test_linux_support.py::*::test_afk_source_unavailable_on_linux -v`
Expected: PASS immediately (AfkSource already returns False for None clock) — this is a guard test pinning the Linux contract; keep it.

- [ ] **Step 3: Wire construction in main.py**

```python
# src/main.py — where SyncEngine + AWManager are constructed and the input
# watcher is wired (search for `self._input_watcher` assignment and
# `sync_engine.health_provider`). After the input watcher exists:
        try:
            from .sync.afk_source import AfkSource
        except ImportError:
            from sync.afk_source import AfkSource
        afk_source = AfkSource(
            afk_timeout_seconds=self.config.aw.afk_timeout_minutes * 60,
            hostname=self.sync_engine._hostname,
            input_watcher=self._input_watcher,
        )
        self.sync_engine.afk_source = afk_source
        self.aw_manager.set_inproc_afk_active(
            self.config.sync.in_process_afk and afk_source.available()
        )
```

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (all)

- [ ] **Step 5: Manual smoke (macOS) — billing parity pre-flip check (from spec Risks)**

Run the app locally with `in_process_afk=True` for a short session, then compare the `bf-afk-inproc_*` active-seconds against the live `aw-watcher-afk` bucket for the same window; they must match within sampling noise. If they diverge (server bills from `last_input`, no grace), revisit the grace in `build_afk_events`.

- [ ] **Step 6: Bump version + commit**

```bash
# bump src/__init__.py to 1.5.64
git add src/main.py src/__init__.py tests/test_linux_support.py
git commit -m "feat(afk): wire in-process AFK source (default on, macOS+Windows)"
```

---

## Self-Review

**Spec coverage:**
- Sole source / skip external bucket → Task 4 (`_should_skip_external_afk`, loop skip). ✓
- macOS+Windows via OS idle clock, Linux excluded → Task 2 `available()`, Task 4 gate, Task 6 test. ✓
- Default-ON kill-switch flag → Task 1. ✓
- Timeline + aw-watcher-afk parity + never-over-bill → Task 3 (+ boundary tests). ✓
- Checkpoint init=now, no backlog rewind → Task 4 (`_build_inproc_afk`; backlog path untouched, asserted in Step 5). ✓
- aw_manager alert suppression → Task 5. ✓
- Pre-flip parity verification → Task 6 Step 5. ✓
- Out-of-scope (stop external process, server flag, remove #67) → not in any task. ✓

**Placeholder scan:** none — all steps carry real code/commands.

**Type consistency:** `AfkSource(afk_timeout_seconds, hostname, *, input_watcher, idle_clock, retention_seconds)`, `available()`, `record_sample(now)`, `build_afk_events(range_start, range_end, project_id=None)` consistent across Tasks 2/3/4/6. `set_inproc_afk_active(active)` consistent Task 5/6. `_should_skip_external_afk` / `_build_inproc_afk` / `_inproc_afk_active` consistent within Task 4.
