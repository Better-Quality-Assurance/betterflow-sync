# In-process AFK source — design

Date: 2026-06-19
Status: approved design, pending spec review
Author: pairing session (Brad + Claude)

## Problem

The server bills active-vs-idle from the uploaded AFK stream. That stream comes
from the external `bf-idle-tracker` binary (renamed `aw-watcher-afk`), a separate
process under its own TCC subject that repeatedly freezes / goes blind on macOS,
and wedges on Windows. When it does, the worked span is billed idle ("Active time
not advancing" fleet alerts; Razvan 2026-06-19 lost ~2.9h in one bucket).

PR #67 patched the worst symptom by synthesizing a not-afk span on upload when the
tracker is stale, and PR #68 made the telemetry honest. But the agent still
*depends* on the flaky binary for the steady-state AFK signal, and recovery from
"blind" is gated on a user re-granting Input Monitoring. This design removes the
dependence: the agent itself becomes the authoritative AFK source.

## Decisions (locked)

1. **Sole source.** Stop uploading the external `bf-idle-tracker` bucket; the agent
   uploads ONE AFK stream built in-process. No server-side conflict between two
   sources. The external tracker keeps *running* (stopping the process is a
   follow-up) but its bucket is ignored.
2. **Platforms: macOS + Windows.** Both have a working OS idle clock
   (`os_idle.get_system_idle_seconds()` — `HIDIdleTime` / `GetLastInputInfo`).
   macOS also has the in-process input watcher (holds the main app's grant) for
   finer transitions. **Linux** has no OS idle clock → keeps the external bucket +
   #67 synthesis unchanged.
3. **Rollout: straight to main + prod, default ON.** `Config.sync.in_process_afk`
   defaults to `True`. The flag is a **kill-switch** back to the exact current
   behavior (external bucket + #67), not a staging gate. No beta channel.

## Architecture

### `sync/afk_source.py` — `AfkSource`

The only component with real logic. Turns a log of activity observations into AFK
spans. Pure and isolated → exhaustively unit-testable.

State:
- `afk_timeout: float` (seconds; from `config.aw.afk_timeout_minutes`).
- `_samples: deque[(sample_time: datetime, last_input_at: datetime)]`, thread-safe
  under a leaf lock, retained ~2h (pruned as the checkpoint advances).
- `_input_watcher` (optional, macOS) for `get_last_input_at()`.

Methods:
- `record_sample(now)` — `idle = get_system_idle_seconds()`; `last_input_at =
  now - idle`. On macOS, if the input watcher reports a *more recent* last input,
  use that. Returns early (records nothing) when the OS idle clock is unavailable
  (Linux), which is also how callers detect "unsupported platform". Append
  `(now, last_input_at)`.
- `build_afk_events(range_start, range_end) -> list[dict]` — pure timeline
  reconstruction (algorithm below). Returns synthetic AFK bf-event dicts.
- `available() -> bool` — whether the OS idle clock is readable on this platform
  (drives the SyncEngine gate).

### Timeline reconstruction (where billing correctness lives)

Activity is known only at each sample's `last_input_at` (the instant of the user's
most recent input as of that sample). Reconstruct active/idle over
`[range_start, range_end]`:

1. Collect the distinct `last_input_at` values (activity instants) within or
   bordering the range, sorted ascending. Prepend the newest activity instant at
   or before `range_start` so the leading edge is anchored.
2. Walk consecutive activity instants `a_i -> a_{i+1}`:
   - The user is **not-afk** from `a_i` until `min(a_{i+1}, a_i + afk_timeout)`.
   - If `a_{i+1} - a_i > afk_timeout`, an **afk** span covers
     `[a_i + afk_timeout, a_{i+1}]`.
3. For the tail after the last activity instant `a_n` up to `range_end`:
   - not-afk `[a_n, min(range_end, a_n + afk_timeout)]`;
   - afk `[a_n + afk_timeout, range_end]` if it exceeds the timeout.
4. Clip every emitted span to `[range_start, range_end]`; drop zero/negative spans.
5. **No samples covering a sub-range → emit afk** for it. Never invent activity
   (the conservative, never-over-bill rule).

Each emitted event:
```
{ "id": f"afk-inproc_{hostname}_{int(span_start.timestamp())}",
  "timestamp": span_start.isoformat(), "duration": round(secs, 2),
  "bucket_id": f"bf-afk-inproc_{hostname}", "bucket_type": BUCKET_TYPE_AFK,
  "data": {"status": "not-afk"|"afk", "synthetic": True},
  "project_id": <if set> }
```
The id is keyed on `span_start`, so the current (growing) span re-sends the same id
each cycle and the server upserts (patches) — same heartbeat-extend contract real
AW events and #67 rely on. Closed past spans keep their id permanently.

### SyncEngine integration

A single flag-gated branch in `sync()`:
- At sync start, `afk_source.record_sample(now)`.
- If `config.sync.in_process_afk and afk_source.available()`:
  - **exclude `afk_buckets`** from the non-window upload loop (don't upload the
    external tracker's bucket);
  - `events = afk_source.build_afk_events(self._afk_inproc_checkpoint, now)`;
    append to `all_events`; advance `_afk_inproc_checkpoint` to `now`;
  - **skip** `_synthesize_for_stale_afk` (#67 — subsumed).
- Else (flag off, or Linux): today's behavior verbatim (external bucket + #67).

Checkpoint: a single `_afk_inproc_checkpoint: datetime` (last covered instant). On
first cycle, initialize to a short lookback (e.g. `now - afk_timeout`) so a restart
doesn't claim a huge active span. The server upserts by event id, so re-emitting an
overlapping current span is safe.

The local pause decision (`idle_manager`) already uses the in-process input watcher
+ OS idle clock (`_has_recent_input`, `_get_system_idle_seconds`), so it needs no
change — it is already in-process-authoritative.

### aw_manager

When in-process AFK is active, **suppress idle-tracker stale/blind detection +
alerting** (the `bf-idle-tracker` stale path in `_restart_if_needed_locked` and the
`idle_tracker_blind` flag): we no longer consume that bucket, so it must not raise
"Active time not advancing" alerts or drive the permission re-prompt. Gate via a
constructor flag/setter the app sets from config. The window-tracker stale path is
untouched.

## Testing (billing-critical → heavy)

`tests/test_afk_source.py` — pure-function timeline:
- continuous activity across the range → one not-afk span, no afk;
- idle past the timeout → not-afk up to `last_input + timeout`, then afk to end;
- exact transition boundary (`gap == afk_timeout`, `+1s`);
- multi-hour offline gap (no samples) → afk for the gap, never not-afk;
- interleaved active/idle/active → correct alternating spans, no fabricated
  activity over the idle middle;
- empty log → afk for the whole range;
- stable id across cycles for the growing current span (server patches in place).

`tests/test_sync_engine_inproc_afk.py` — integration on the real sync path:
- flag ON + readable clock → external afk bucket NOT uploaded; in-process afk
  events present in the sent batch with `bucket_id` `bf-afk-inproc_*`;
- flag OFF → external afk bucket uploaded + `_synthesize_for_stale_afk` runs (today);
- `available()` False (Linux / clock unreadable) → forced OFF regardless of flag.

Proof-of-failure per fixture discipline: each behavior test fails on pre-change code
(no `AfkSource`, external bucket always uploaded) and passes after.

## Out of scope (follow-ups)
- Stopping the external `bf-idle-tracker` process entirely.
- Server-controlled flag in the config payload.
- Removing #67 / the external path (retained for Linux + flag-off).

## Risks
- **Billing correctness of the timeline builder** — mitigated by the conservative
  never-over-bill rule (unknown → afk) and exhaustive pure-function tests.
- **Default ON, no beta** — any timeline bug bills wrong fleet-wide before it's
  caught; the kill-switch flag is the rollback. (Accepted: Brad, 2026-06-19.)
- **Coarse Windows granularity** — sampling at sync cadence (~30s) vs the 600s
  afk_timeout is far finer than needed; sub-sample idle blips are correctly ignored.
