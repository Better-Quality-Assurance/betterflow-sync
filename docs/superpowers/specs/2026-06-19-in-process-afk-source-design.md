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
2. Walk consecutive activity instants `a_i -> a_{i+1}` (NO grace):
   - if `a_{i+1} - a_i <= afk_timeout`: **not-afk** for the whole `[a_i, a_{i+1}]`
     (a pause that never reached the timeout never went afk);
   - else: **afk** for the whole `[a_i, a_{i+1}]` (backdated to the last input).
3. Tail after the last activity instant `a_n` up to `range_end`: **not-afk** if
   `range_end - a_n <= afk_timeout`, else **afk** (whole tail). The caller passes
   `range_end = finalize_point(now)` so this tail is only ever the settled region.
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
**Billing parity (critical — corrected after live measurement 2026-06-19).**
`aw-watcher-afk` **backdates `afk` to the last input — there is NO grace.**
Verified on a live machine: across 8 clean idle transitions, the gap from the
last real keystroke/click to the start of the stored `afk` span was a **median of
9 seconds**, not 600s. (Had a grace existed, every gap would cluster near 600s; a
9s gap is only possible if `afk` starts at the last input.) So the rule is:

- consecutive activity instants `a -> b`: `not-afk [a, b]` iff `b - a <=
  afk_timeout`; otherwise **`afk` for the entire `[a, b]`** (no 600s of leading
  not-afk).

The earlier draft assumed a grace and would have credited ~`afk_timeout` of extra
active time per break across the fleet — an over-bill. The `afk_timeout` only
governs how long real-time detection waits before *declaring* afk; the stored/
billed span is backdated to the last input.

**Ids & no overlap.** Each cycle finalizes only up to `finalize_point(now)` (the
last instant whose afk status is settled — see integration), and the checkpoint
advances to that point. `build_afk_events` emits spans only for the new
`[checkpoint, finalize_point]` slice, so consecutive cycles produce
**non-overlapping** segments uploaded once. No growing-span / re-emit and no
server-side status-flip patching is relied on; the `span_start`-keyed id is a
stable unique key (and lets a re-queued offline event dedupe on retry).

### SyncEngine integration

A single flag-gated branch in `sync()`:
- At sync start, `afk_source.record_sample(now)`.
- If `config.sync.in_process_afk and afk_source.available()`:
  - **exclude `afk_buckets`** from the non-window upload loop (don't upload the
    external tracker's bucket);
  - `finalize_to = afk_source.finalize_point(now)`; if `finalize_to >
    checkpoint`, `events = afk_source.build_afk_events(checkpoint, finalize_to)`,
    append to `all_events`, and advance `_afk_inproc_checkpoint` to `finalize_to`
    (the trailing region within the timeout of the last input stays pending for a
    later cycle, so it can resolve not-afk OR afk without re-emit);
  - **skip** `_synthesize_for_stale_afk` (#67 — subsumed).
- Else (flag off, or Linux): today's behavior verbatim (external bucket + #67).

Checkpoint: a single `_afk_inproc_checkpoint: datetime` (last covered instant),
**initialized to `now` on the first cycle** — we only account for time while the
agent is running and sampling, and emit nothing for the unknown pre-start period.
Failed uploads ride the existing offline queue (events are appended to `all_events`
→ `_send_and_advance_checkpoints`, which queues on failure), so advancing the
checkpoint after a failed send never loses data.

**Backlog reconcile does NOT touch `_afk_inproc_checkpoint`.** The day-start
checkpoint rewind exists to re-fetch stranded events from real AW buckets; the
in-process AFK stream has no AW bucket and the sample log only retains ~2h, so
rewinding it would re-emit `afk` over a morning we already billed correctly.
In-process AFK relies solely on the offline queue for delivery, never on rewind.

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
- **billing-parity boundary**: a pause of `afk_timeout − 1s` is entirely not-afk
  (grace), a pause of `afk_timeout + 1s` flips to afk exactly at
  `last_input + afk_timeout` — pins that short pauses stay billed active like
  aw-watcher-afk;
- consecutive cycles produce non-overlapping, contiguous segments (no gaps, no
  double-cover) for a steadily-active user.

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
- **afk-transition semantics** — VERIFIED on a live macOS machine 2026-06-19:
  `aw-watcher-afk` backdates afk to the last input (median 9s gap from last
  keystroke/click to afk-span start across 8 transitions; a grace would show
  ~600s). The design uses the no-grace rule accordingly. An earlier grace draft
  would have over-billed ~`afk_timeout` per break fleet-wide; caught by this check.
- **Default ON, no beta** — any timeline bug bills wrong fleet-wide before it's
  caught; the kill-switch flag is the rollback. (Accepted: Brad, 2026-06-19.)
- **Coarse Windows granularity** — sampling at sync cadence (~30s) vs the 600s
  afk_timeout is far finer than needed; sub-sample idle blips are correctly ignored.
