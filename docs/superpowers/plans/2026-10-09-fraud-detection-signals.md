# Fraud-detection signal fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the client-side fraud score mean something: stop the false
positive every steady worker gets, add the impossible-typing-rate signal that
is missing, and make a dead Windows input sensor visible (and self-healing)
instead of indistinguishable from a person who never typed.

**Architecture:** Three agent changes in `betterflow-sync` (one branch, one
PR), then one server change in `internal-tool2` (separate PR, carries a
migration) so the new device signal is stored and readable. Mouse movement
as a *fraud* signal is deliberately NOT in this plan (see "Out of scope").

**Tech Stack:** Python 3.11 agent (pytest, ctypes on Windows); Laravel/PHP
server + Node MCP server in internal-tool2.

**Spec:** this session's investigation (2026-10-09), summarised under
"Evidence" below. No separate spec doc.

## Evidence (what this plan argues from)

- **Verified:** 13 users on Oct 7-8 topped out at a client fraud score of
  exactly 24, all with thousands of keystrokes/clicks. Replaying the real
  `FraudSignalDetector` with one input event per ~60 s (the agent's sync
  cadence, ±0.2 s jitter) yields `score=24 signals=['input_regularity']`;
  with three 10-minute breaks it yields 0. The in-process input source
  emits ONE aggregate event per drain (`InputSource.drain_input_event`), so
  the gaps `_check_input_regularity` measures are the scheduler's, not the
  person's.
- **Verified:** one user logged ~261,000 keystrokes in 7.9 h (~550/min
  all-day average) and scored 5. No signal checks rate.
- **Verified:** every Windows device in use (3 of 3: device ids 60, 16, 24)
  reports zero keystrokes and clicks every day while `idle_tracker_blind`
  is false and health is "not degraded". The server never sends
  `in_process_input`, so the Windows platform default (on) applies.
- **Inferred, not yet verified:** the hook is installed and later dies
  silently. Windows removes a low-level hook whose callback exceeds
  `LowLevelHooksTimeout` with no notification, and a Python callback that
  waits on the GIL is a plausible way to exceed it. `available()` then still
  returns True (thread alive, `_install_failed` False). A log upload from
  device 60 is requested and pending; read it before Task 3 is merged. If it
  shows `SetWindowsHookEx failed`, the cause is install refusal (already
  logged) and Task 3's self-heal is still correct but not the root fix.

## Global Constraints

- Run tests with `PYTHONPATH=. python3 -m pytest` from the worktree root.
- Every module keeps the dual import pattern (`from .x import` / `from x import`).
- PR CI is ubuntu; tag builds are macOS + Windows + Linux. Nothing may
  `skipif(darwin)`; Windows-only code is tested through injected fakes.
  Any `open()`/`read_text()` passes `encoding="utf-8"`.
- A heartbeat health key must be added to BOTH
  `src/sync/bf_client.py:HEARTBEAT_HEALTH_KEYS` and
  `src/disclosure_baseline.py` (with a written reason), and to the
  "Device identifiers sent on the heartbeat" section of `CLAUDE.md`.
- Nothing new may leave the device beyond what the plan names. Mouse-move
  events are counted ONLY as a local liveness timestamp, never uploaded.
- `DEFER_UNAPPLIED_SERVER_SETTINGS` stays True; new config fields ship with
  safe local defaults.
- Commit after each task. Explicit `git add <paths>`, never `-A`.

## Review Focus

1. A Windows user who only moves the mouse (reading) for 10+ minutes must
   NOT be reported `silent` — mouse moves count as hook liveness.
2. Right after start or reinstall, no `silent` verdict for 300 s (grace).
3. `os_idle_seconds` unreadable (None) ⇒ never `silent` (no evidence ⇒ no verdict).
4. macOS/Linux backends never report `silent`: their counters do not see
   mouse moves, so a reader would false-positive.
5. A server `/config` still carrying `input_regularity_cv_threshold` /
   `min_input_events_for_regularity` must parse without error.

Each line has a test in the owning task below.

---

### Task 1: Retire the `input_regularity` signal

The signal cannot be measured from aggregate count events: every sampling
cadence is regular by construction. Remove it from scoring; keep its config
fields so an old server payload still parses.

**Files:**
- Modify: `src/sync/activity_analyzer.py` (`FraudSignalDetector.assess`, delete `_check_input_regularity`, update class docstring)
- Modify: `src/config.py:366-381` (comment the two retired fields; keep them)
- Test: `tests/test_activity_analyzer.py` (replace `test_regular_intervals_flagged`, `test_random_intervals_pass`, `test_input_regularity_needs_min_events`)

**Interfaces:**
- Produces: `FraudAssessment.signals` never contains `"input_regularity"`.
  `record_input_timestamp` stays (still called by `ActivityAnalyzer.add_input_events`) but its data no longer feeds a score.

- [ ] **Step 1: Check the server does not read `input_regularity_cv`**

Run: `git -C /Users/brad/Code2/internal-tool2 fetch -q origin && git -C /Users/brad/Code2/internal-tool2 grep -n "input_regularity" origin/main -- src mcp-server`
If any hit READS `activity_metrics.input_regularity_cv`, keep that key in
`extra_metrics` (set to `None`) instead of deleting it in Step 3. Note the result in the commit body.

- [ ] **Step 2: Write the failing test** (replaces the three regularity tests)

```python
    def test_sync_cadence_input_is_not_flagged(self):
        """One aggregate input event per ~60s sync drain is the AGENT's clock,
        not the person's. Replayed through the old detector this scored 24
        (input_regularity) for every steady worker on 2026-10-08."""
        import random
        rng = random.Random(1)
        t = datetime(2026, 10, 8, 9, tzinfo=timezone.utc)
        for _ in range(300):
            t += timedelta(seconds=60 + rng.uniform(-0.2, 0.2))
            self.detector.record_input_timestamp(t)
        result = self.detector.assess()
        assert "input_regularity" not in result.signals
        assert result.score == 0

    def test_perfectly_regular_input_is_not_flagged_either(self):
        """The signal is retired, not retuned: exact 60s spacing is what a
        healthy agent produces too."""
        t = datetime(2026, 10, 8, 9, tzinfo=timezone.utc)
        for _ in range(300):
            t += timedelta(seconds=60)
            self.detector.record_input_timestamp(t)
        assert "input_regularity" not in self.detector.assess().signals
```

(Use the file's existing `self.detector` fixture and imports; add
`timezone`/`timedelta` to the import line if absent.)

- [ ] **Step 3: Run to verify it fails**

Run: `PYTHONPATH=. python3 -m pytest tests/test_activity_analyzer.py -k "sync_cadence or perfectly_regular" -v`
Expected: FAIL — `assert 'input_regularity' not in ['input_regularity']`.

- [ ] **Step 4: Implement** — in `assess()` delete the block:

```python
        ir_score, ir_cv = self._check_input_regularity()
        if ir_score > 0:
            signals.append("input_regularity")
            total_score += ir_score
```

delete `_check_input_regularity`, drop `input_regularity` from the class
docstring's signal list with one line saying why (aggregate events carry
the sampler's cadence, not the human's), and remove (or null, per Step 1)
the `input_regularity_cv` entry in `extra_metrics`. In `config.py` mark
`input_regularity_cv_threshold` and `min_input_events_for_regularity` as
"retired 2026-10-09, kept so an older server /config still parses".

- [ ] **Step 5: Config-compat test** (Review Focus 5) in `tests/test_activity_analyzer.py`:

```python
    def test_retired_regularity_config_still_accepted(self):
        from src.config import FraudDetectionConfig
        cfg = FraudDetectionConfig(input_regularity_cv_threshold=0.2,
                                   min_input_events_for_regularity=5)
        det = FraudSignalDetector(config=cfg)
        assert det.assess().score == 0
```

- [ ] **Step 6: Run the file and the full suite**

Run: `PYTHONPATH=. python3 -m pytest tests/test_activity_analyzer.py -v && PYTHONPATH=. python3 -m pytest -q`
Expected: all pass. `test_fraud_score_capped_at_100` and `test_extra_metrics_populated` may reference regularity — update them to the remaining signals, do not delete their assertions.

- [ ] **Step 7: Commit**

```bash
git add src/sync/activity_analyzer.py src/config.py tests/test_activity_analyzer.py
git commit -m "fix(fraud): retire input_regularity — it measured the sync clock, not the person"
```

---

### Task 2: Add the `implausible_input_rate` signal

**Files:**
- Modify: `src/config.py` (`FraudDetectionConfig`, `update_from_server` fraud block)
- Modify: `src/sync/activity_analyzer.py` (`FraudSignalDetector.__init__`, `record_window_metrics`, `assess`, `clear`; `ActivityAnalyzer.get_fraud_assessment` call site)
- Test: `tests/test_activity_analyzer.py`

**Interfaces:**
- Consumes: `ActivityMetrics.presses` (count over the analyzer window), `EngagementThresholds.window_minutes`.
- Produces: `FraudDetectionConfig.max_presses_per_minute: int = 400`;
  `FraudSignalDetector.record_window_metrics(metrics, app=None, window_minutes=5)`;
  signal name `"implausible_input_rate"`, score 30.

Threshold rationale (put in the config comment): a fast typist bursts at
~400-500 characters/min but a 5-minute window of real work averages far
below that; the 2026-10-08 outlier averaged ~550/min over 7.9 h. A held key
(auto-repeat) also trips it — that is intended, the name says "implausible
input", not "fraud".

- [ ] **Step 1: Write the failing tests**

```python
    def _window(self, presses, clicks=0):
        return ActivityMetrics(presses=presses, clicks=clicks, scrolls=0, window_changes=1)

    def test_implausible_typing_rate_flagged(self):
        # 2,750 presses in a 5-minute window = 550/min, the 2026-10-08 outlier's all-day average
        self.detector.record_window_metrics(self._window(2750), app="X", window_minutes=5)
        result = self.detector.assess()
        assert "implausible_input_rate" in result.signals
        assert result.score >= 30

    def test_fast_but_human_typing_passes(self):
        # 300/min sustained for 5 minutes: fast, possible
        self.detector.record_window_metrics(self._window(1500), app="X", window_minutes=5)
        assert "implausible_input_rate" not in self.detector.assess().signals

    def test_rate_threshold_is_configurable(self):
        from src.config import FraudDetectionConfig
        det = FraudSignalDetector(config=FraudDetectionConfig(max_presses_per_minute=100))
        det.record_window_metrics(self._window(600), app="X", window_minutes=5)  # 120/min
        assert "implausible_input_rate" in det.assess().signals

    def test_rate_signal_cleared_by_clear(self):
        self.detector.record_window_metrics(self._window(2750), app="X", window_minutes=5)
        self.detector.clear()
        assert "implausible_input_rate" not in self.detector.assess().signals
```

(Check `ActivityMetrics`'s real constructor fields first and match them.)

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=. python3 -m pytest tests/test_activity_analyzer.py -k "rate" -v`
Expected: FAIL — `TypeError: unexpected keyword argument 'window_minutes'`.

- [ ] **Step 3: Implement**

`config.py`, in `FraudDetectionConfig`:
```python
    # Presses per minute, averaged over one analyzer window, above which input
    # is not plausibly a person typing (fast typists burst at ~400-500/min but a
    # 5-minute working window averages far below). A held key trips it too.
    max_presses_per_minute: int = 400
```
and in the `fraud_detection` block of `update_from_server`:
```python
                if "max_presses_per_minute" in fd:
                    self.fraud_detection.max_presses_per_minute = max(60, int(fd["max_presses_per_minute"]))
```

`activity_analyzer.py`, `FraudSignalDetector`:
```python
        # Highest presses/min seen in any single window this session.
        self._max_presses_per_minute: float = 0.0
```
in `record_window_metrics(self, metrics, app=None, window_minutes=5)`:
```python
        if window_minutes > 0:
            rate = metrics.presses / window_minutes
            if rate > self._max_presses_per_minute:
                self._max_presses_per_minute = rate
```
in `assess()`:
```python
        if self._max_presses_per_minute > self._config.max_presses_per_minute:
            signals.append("implausible_input_rate")
            total_score += 30
```
add `"max_presses_per_minute": round(self._max_presses_per_minute, 1)` to
`extra_metrics`, reset it in `clear()`, and in
`ActivityAnalyzer.get_fraud_assessment` pass
`window_minutes=self._thresholds.window_minutes` to `record_window_metrics`.
Add the signal to the class docstring list.

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=. python3 -m pytest tests/test_activity_analyzer.py -v && PYTHONPATH=. python3 -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/config.py src/sync/activity_analyzer.py tests/test_activity_analyzer.py
git commit -m "feat(fraud): flag physically implausible typing rates"
```

---

### Task 3: Detect and self-heal a silent Windows input hook

**Files:**
- Modify: `src/sync/input_source.py` (`InputSource`: liveness timestamp + `capture_state`; `_WindowsHookBackend`: note every event incl. `WM_MOUSEMOVE`, declare `observes_all_input = True`)
- Modify: `src/main.py` (`_build_health_telemetry`: emit `input_capture_state` and reinstall on `silent`; move the "In-process input source: active/inactive" log to after `input_source.start()`)
- Modify: `src/sync/bf_client.py` (`HEARTBEAT_HEALTH_KEYS` += `"input_capture_state"`)
- Modify: `src/disclosure_baseline.py` (same key + reason)
- Modify: `CLAUDE.md` ("Device identifiers sent on the heartbeat": one bullet)
- Test: `tests/test_input_source.py`, `tests/test_heartbeat_health_telemetry.py`, `tests/test_disclosure_baseline.py` (must stay green)

**Interfaces:**
- Produces:
  - `InputSource.note_event(now: Optional[float] = None) -> None` — sets `self._last_event_mono` (plain attribute store, no lock: called from the hook callback, must stay cheap).
  - `InputSource.mark_started(now: Optional[float] = None) -> None` — called by `start()` on success; records `self._started_mono`.
  - `InputSource.capture_state(os_idle_seconds: Optional[float], now: Optional[float] = None) -> Optional[str]` returning `None` (no backend), `"unavailable"`, `"silent"`, or `"ok"`.
  - Constants `SILENT_GRACE_S = 300`, `SILENT_NO_EVENT_S = 300`, `SILENT_OS_IDLE_MAX_S = 60`.
  - Heartbeat key `input_capture_state` (string or absent).

State rule, verbatim:
```python
    def capture_state(self, os_idle_seconds, now=None):
        if self._backend is None:
            return None
        if not self.available():
            return "unavailable"
        if not getattr(self._backend, "observes_all_input", False):
            return "ok"   # macOS tap / others cannot see mouse moves: no silent verdict
        now = time.monotonic() if now is None else now
        started = self._started_mono
        if started is None or now - started < SILENT_GRACE_S:
            return "ok"
        if os_idle_seconds is None or os_idle_seconds > SILENT_OS_IDLE_MAX_S:
            return "ok"
        last = self._last_event_mono
        if last is None or now - last > SILENT_NO_EVENT_S:
            return "silent"   # the OS saw input in the last minute; our hook saw nothing for 5
        return "ok"
```

- [ ] **Step 1: Write the failing tests** in `tests/test_input_source.py`

```python
class _AllInputBackend:
    observes_all_input = True
    def __init__(self): self.ok = True; self.starts = 0; self.stops = 0
    def available(self): return self.ok
    def start(self): self.starts += 1; return True
    def stop(self): self.stops += 1

class _TapBackend(_AllInputBackend):
    observes_all_input = False

def _src(backend):
    return InputSource("h", backend=backend, frontmost_app_getter=None)

def test_silent_when_os_saw_input_but_hook_saw_nothing():
    s = _src(_AllInputBackend()); s.mark_started(now=0.0)
    assert s.capture_state(os_idle_seconds=5, now=1000.0) == "silent"

def test_mouse_moves_keep_a_reader_ok():                       # Review Focus 1
    s = _src(_AllInputBackend()); s.mark_started(now=0.0)
    s.note_event(now=990.0)   # a WM_MOUSEMOVE 10s ago, no press/click
    assert s.capture_state(os_idle_seconds=5, now=1000.0) == "ok"

def test_no_verdict_inside_grace_after_start():                # Review Focus 2
    s = _src(_AllInputBackend()); s.mark_started(now=900.0)
    assert s.capture_state(os_idle_seconds=5, now=1000.0) == "ok"

def test_unreadable_os_idle_is_never_silent():                 # Review Focus 3
    s = _src(_AllInputBackend()); s.mark_started(now=0.0)
    assert s.capture_state(os_idle_seconds=None, now=1000.0) == "ok"

def test_user_away_is_not_silent():
    s = _src(_AllInputBackend()); s.mark_started(now=0.0)
    assert s.capture_state(os_idle_seconds=900, now=1000.0) == "ok"

def test_tap_backend_never_silent():                           # Review Focus 4
    s = _src(_TapBackend()); s.mark_started(now=0.0)
    assert s.capture_state(os_idle_seconds=5, now=1000.0) == "ok"

def test_refused_hook_is_unavailable():
    b = _AllInputBackend(); b.ok = False
    assert _src(b).capture_state(os_idle_seconds=5, now=1000.0) == "unavailable"

def test_no_backend_reports_nothing():
    assert _src(None).capture_state(os_idle_seconds=5, now=1000.0) is None

def test_windows_mouse_move_notes_liveness_but_counts_nothing():
    """WM_MOUSEMOVE is liveness only: no press/click/scroll increments."""
    s = _src(None)
    s.note_event(now=5.0)
    assert s.counts() == (0, 0, 0)
    assert s._last_event_mono == 5.0
```

In `tests/test_heartbeat_health_telemetry.py` add (follow that file's existing
app-fixture pattern for building `_build_health_telemetry`):
- `input_capture_state == "silent"` is present in the dict when the injected input source reports silent, and `input_source.stop()` then `start()` were each called once.
- a second call within 600 s does NOT restart again (counts stay at 1).
- the key is ABSENT when `config.sync.in_process_input` is False.

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=. python3 -m pytest tests/test_input_source.py tests/test_heartbeat_health_telemetry.py -v`
Expected: FAIL — `AttributeError: 'InputSource' object has no attribute 'mark_started'`.

- [ ] **Step 3: Implement `InputSource`**

In `__init__`: `self._last_event_mono: Optional[float] = None` and
`self._started_mono: Optional[float] = None`. Add `note_event`,
`mark_started`, `capture_state` (verbatim above). Each of `_on_press`,
`_on_click`, `_on_scroll` also calls `self.note_event()`. In `start()`,
call `self.mark_started()` when the backend start returns True.

In `_WindowsHookBackend`: class attribute `observes_all_input = True`;
constant `_WM_MOUSEMOVE = 0x0200`; in `_mouse_proc` add
`elif wParam == self._WM_MOUSEMOVE: src.note_event()` and in `_kbd_proc`
call `src.note_event()` for every `HC_ACTION` (key-up included). Do NOT
add any other work to the callbacks — they run under the OS hook timeout.

- [ ] **Step 4: Implement the heartbeat side in `main.py`**

In `_build_health_telemetry`, after `os_idle_seconds` is read (~line 2161),
when `self.input_source is not None and self.config.sync.in_process_input`:

```python
            state = self.input_source.capture_state(idle_seconds)
            if state is not None:
                telemetry["input_capture_state"] = state
            if state == "silent":
                now = time.monotonic()
                if now - self._last_input_reinstall_mono >= 600:
                    self._last_input_reinstall_mono = now
                    logger.warning(
                        "Input hook silent: OS reports input %ss ago but the hook "
                        "saw nothing for %ss — reinstalling", idle_seconds,
                        SILENT_NO_EVENT_S,
                    )
                    self.input_source.stop()
                    self.input_source.start()
```

Import `SILENT_NO_EVENT_S` from `sync.input_source` using the file's
dual-import pattern, and initialise
`self._last_input_reinstall_mono = float("-inf")` in `__init__`.
Wrap in the same best-effort `try/except` the surrounding code uses. Move
the `"In-process input source: %s"` info log from `__init__` (~line 2549)
into `_start_watchers` immediately after `self.input_source.start()`, so it
reports the install result instead of a pre-install probe.

- [ ] **Step 5: Register the key**

`bf_client.py` `HEARTBEAT_HEALTH_KEYS` and `disclosure_baseline.py`'s copy:
add `"input_capture_state"` with the reason: *"Whether this machine's input
counter is working: ok / silent (the OS saw input, our counter did not) /
unavailable (the OS refused the hook). Describes the sensor, never the
person; derived from a local event timestamp that is never sent."* Add the
matching bullet to `CLAUDE.md`. Keep the tuples in identical order.

- [ ] **Step 6: Run everything**

Run: `PYTHONPATH=. python3 -m pytest -q`
Expected: PASS, including `tests/test_disclosure_baseline.py`.

- [ ] **Step 7: Mutation check (guard watched failing)**

On the committed code, one mutant at a time (re-run `tests/test_input_source.py` after each, then restore from `HEAD`):
1. make `note_event` a no-op → `test_mouse_moves_keep_a_reader_ok` must fail;
2. delete the `observes_all_input` check → `test_tap_backend_never_silent` must fail;
3. delete the grace check → `test_no_verdict_inside_grace_after_start` must fail.
Record the red test per mutant in the commit body of Step 8.

- [ ] **Step 8: Commit**

```bash
git add src/sync/input_source.py src/main.py src/sync/bf_client.py src/disclosure_baseline.py CLAUDE.md tests/test_input_source.py tests/test_heartbeat_health_telemetry.py
git commit -m "fix(input): report and self-heal a silent Windows input hook"
```

---

### Task 4 (internal-tool2, separate PR): store and expose the new signal

Without this the agent's `input_capture_state` is dropped on arrival — the
same three-repo trap as `os_idle_seconds`.

**Files (internal-tool2):**
- Create: migration `add_input_capture_state_to_agent_devices` — nullable `varchar(16)`.
- Modify: `src/app/Http/Controllers/Api/Agent/AgentHeartbeatController.php` — read `input_capture_state`, accept only `ok|silent|unavailable`, else leave the column unchanged (the controller's existing rule for unparseable values); `AgentDevice` `@property` + fillable.
- Modify: `mcp-server/src/tools/debugging.js` — `betterflow_agent_devices` returns `input_capture_state`; `betterflow_agent_activity_query` raw rows return `fraud_signals` (closes #2793).
- Modify: `src/app/Services/Agent/AnomalyDetectionService.php` — when the device row says `silent` or `unavailable`, treat it as `input_sensor_missing` (score 0, neutral indicator) even if a few input rows exist.
- Tests: feature test for the controller (valid, invalid, absent), MCP test rows carrying both fields, anomaly test for the new branch.

- [ ] Steps follow internal-tool2's own CLAUDE.md. **Run the migration against prod BEFORE merging** (root CLAUDE.md §Ship pipeline), verify the column exists, then merge. Restart the betterflow MCP afterwards (it serves the local tree).

---

### Task 5: Ship

- [ ] Fake-platform sweep (Windows + Linux via `sys.platform`/`platform.system` monkeypatching) over the three changed test files before review.
- [ ] Two `adversarial-reviewer`s in parallel on the agent PR (correctness + design), prepending `notes/review-house-conventions.md`; fix evidenced Critical/Important; re-review.
- [ ] `gh pr checks` must be green; merge; then the internal-tool2 PR (migration first).
- [ ] Release: PR #274 (1.5.139) is open — let it land first, then cut 1.5.140 with this change, publish the draft, verify downloads by byte count.
- [ ] Day after rollout: read `input_capture_state` for devices 60, 16, 24 and the Oct 10 keystroke counts; re-run the per-user score summary — expect no blanket 24.

## Out of scope (needs a decision, not code)

**Mouse movement as a fraud signal.** It is the only way to see a jiggler,
but uploading a movement count is a new data category: the one-time privacy
notice text changes (fleet-wide re-acknowledgement via `NOTICE_VERSION`),
and the English notice renders Regulament Intern art. 64^1, whose Romanian
text prevails. That is a legal/HR decision. Task 3 counts moves only as a
local liveness timestamp and sends nothing about them.
