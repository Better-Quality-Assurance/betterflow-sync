# Agent permission & capture visibility — design

Status: **specced, not built.** Written 2026-07-27 after a user reported a
mis-attributed meeting and the investigation could not answer the follow-up
question — "who else is broken?" — from any existing surface.

## Problem

The agent knows, accurately and continuously, whether it has the OS permissions
it needs. None of that ever leaves the machine, and nothing tells a human when a
device stops capturing.

`src/ui/permissions.py` detects macOS Accessibility (`AXIsProcessTrusted`, with
a real `AXUIElementCopyAttributeValue` probe as the authoritative fallback) and
Input Monitoring (`IOHIDCheckAccess`). `_check_permissions` re-runs on the 60s
tick. The window watcher re-checks Accessibility every 30s and logs
`"Accessibility permission revoked — window titles will be empty"`.

Every one of those readings dies on the device. `HEARTBEAT_HEALTH_KEYS`
(`src/sync/bf_client.py`) is an allowlist, and no permission grant is in it. The
tray shows exactly one permission row (Input Monitoring); a Mac with
Accessibility revoked renders a fully green tray. Server-side there is no
permissions column, no permissions panel, and no filter — `admin/agents` and
`admin/agents/fleet` render zero health columns, and the only human surface is a
degraded badge on the single-device page.

### Three failure classes, not one

The investigation's central finding. These fail independently and need different
fixes, and today they are not distinguished anywhere:

| # | Failure | Signal that exists | Reaches a human? |
|---|---|---|---|
| 1 | Window titles blind — macOS Accessibility missing, Windows tracker blind, Linux X11 watcher dead | `window_titles_captured_recently = false` (stored) | **No** — alert path disabled by a constant |
| 2 | AFK/idle tracker blind — Input Monitoring denied on the tracker, survives restarts | `idle_tracker_blind` (**on the wire today**) | **No** — server has no column and never reads it |
| 3 | Input counters dead — keystrokes and clicks zero while window and AFK capture are healthy | **none — no signal exists** | **No** |

Class 3 is the one the investigation actually caught, and it is invisible to
both other signals. Two Windows devices have logged **zero keystrokes and zero
clicks across every day recorded** (10 and 7 consecutive days) while reporting
8-9h/day of active time — and both report `window_titles_captured_recently:
true` and healthy idle data, so classes 1 and 2 are clean on them. Their
fraud-detection input stream is empty and nothing anywhere says so.

### Measured state, 2026-07-27 (VERIFIED via `betterflow_agent_devices`)

48 devices: 28 titles OK, **3 titles broken**, 17 unknown. Filtered to devices
seen in the last 24h, the unknown population collapses to **two**, both on agent
1.5.95, which predates the field — an upgrade problem, not a permission problem.
13 of the 17 unknowns have not checked in for 12-139 days.

This supersedes the 38/47 figure quoted in `AgentHeartbeatController`'s
`ALERT_ON_BLIND_WINDOW_TITLES` docblock. That number came from a log-warning
sweep on 2026-07-22/23; the column that the flag actually keys on now reads 3
broken against 28 healthy. Different instruments — the backlog is not provably
"fixed" — but the page-storm the constant guards against would not happen today.

## Why not simply flip `ALERT_ON_BLIND_WINDOW_TITLES`

Volume is no longer the objection. Coupling is.

Flipping the constant routes class 1 through `tracking_degraded = true`, which
changes what **every** existing consumer of that flag sees — the
`telemetry_degraded` population in `/api/internal/device-health`, the bot's
severity escalation, the admin badge. A missing permission is a standing
configuration fault, not the "tracking is degraded right now" condition that
flag was built to mean.

The constant stays `false` permanently and is superseded by a dedicated reason
code. Its docblock should be updated to say so rather than left implying someone
should eventually flip it.

## Design

Two milestones. M1 is independently shippable and needs no agent release, so it
covers devices on old builds that will never report grants.

### M1 — server-derived detection and alerting

**Detection.** A new `permissions_degraded` category in
`InternalStatsController::deviceHealth()`, modelled on the existing
`ingest_stalled` block and emitting `tracking_degraded => false` so it never
contaminates the self-reported population. Three reason codes:

- `window_titles_blind` — active, not soft-deleted, fresh `last_seen_at`,
  `window_titles_captured_recently = false`.
- `idle_tracker_blind` — the agent already sends this key; the server has
  nowhere to put it. Requires the one migration in M1: a nullable boolean column,
  persistence in the heartbeat, and an entry in `hasHealthTelemetry()`.
- `input_counters_dead` — fully derived from `agent_activity_aggregates`: rows
  exist for the day, `SUM(active_seconds) >= 4h`, and
  `SUM(keystrokes_count + clicks_count + scrolls_count) = 0`.

**Why all three counters, and why same-day.** Scrolls are the discriminator. A
quiet user still scrolls constantly — a healthy device logged 58,631 scrolls in
Chrome in one day. The two broken devices report zero keystrokes, zero clicks
**and** zero scrolls across every app, including hours in Excel and Slack. That
is not a person working quietly; it is a tracker that is not reporting, and one
day of it is already conclusive. Requiring a second consecutive day would add no
confidence and delay the alert by a day. Including scrolls makes the predicate
strictly harder to satisfy than keystrokes+clicks alone, which is the safe
direction: a device with scrolls but no keystrokes is a different, partial
failure and must NOT be flagged under this reason. The 4h active floor exists so
a machine left awake and idle cannot trip it.

**Fail-closed rules, non-negotiable in review.** Never-reported devices are
excluded by an explicit predicate, never by relying on `DEFAULT false` —
`tracking_degraded`, `consecutive_sync_failures`, `idle_tracker_stale_restarts`
and `idle_while_active_detections` are all `NOT NULL DEFAULT` columns where a
device that has never reported is byte-identical to a healthy one, and
`health_reported_at` is the only column that separates them. `input_counters_dead`
fires only when aggregate rows **exist** and sum to zero; "no rows" is not "no
input". The new `idle_tracker_blind` column is nullable with no default, matching
`window_titles_captured_recently` rather than its neighbours.

**Alerting (betterqa-bot).** The pipeline already exists end-to-end and is
deterministic — a 10-minute cron, an `alert_state` table keyed on
`device_id:reason` giving per-reason cooldown for free, pure template-literal
formatting with no LLM anywhere, and a Slack path with retry, coalescing and an
audit row. Reuse all of it. The additions are three `reasonText()` cases naming
the actual fix, a `severityFor()` clause pinning this class to `warning`, and the
existing seen-on-a-prior-run persistence filter so a blip cannot page.

Severity matters more than it looks: `critical` bypasses Slack coalescing and
triggers the Telegram fallback. A standing config fault must never reach it.

**The one change to shared code.** `COOLDOWN_SECONDS` is a module-level constant
at 6h shared by every reason. A permission fault is cleared only by a human, so
6h means four pings per device per day indefinitely. It becomes per-reason;
permissions get 72h.

### M2 — agent-authoritative grants

The agent adds `accessibility` and `input_monitoring` booleans to its health
snapshot and to `HEARTBEAT_HEALTH_KEYS`. It already computes both — this is
plumbing, not new detection. Two nullable columns server-side.

The alert prefers the explicit grants when non-null and falls back to the derived
signals when null, so old builds keep working unchanged. Wording upgrades from
"this tracker is blind" to "grant Input Monitoring", which is the difference
between an alert someone can act on and one they have to investigate.

## Deliberately not built

- **No grouping or roll-up.** No such primitive exists and the current backlog is
  3 devices, so it would be 3 messages. Build it when it hurts.
- **No new "old agent build" nag.** `AGENT_MINIMUM_VERSION` on Railway already
  pushes the fleet forward: the agent reads `minimum_agent_version` off the
  heartbeat and fires `on_update_required` when it is below the floor
  (`src/sync/sync_engine.py:3772-3783`, VERIFIED). A second mechanism for the two
  1.5.95 machines duplicates a working lever. Confirm those two builds carry that
  handler before relying on it for them.
- **No admin UI work in M1.** The chosen model is push, not pull — and the pull
  surface already exists and is better than what would be built: the
  `betterflow_agent_devices` MCP tool is tri-state aware (`tracking_degraded_known`,
  the true/false/null split on title capture) and filterable by
  `title_capture="broken"`, neither of which the admin screen does.

  This makes one thing mandatory rather than optional: **the Slack line must be
  self-sufficient.** It names the person, the device, the platform and the exact
  remedial action, because there is deliberately nowhere to click through to. An
  alert that says "device 24 is degraded" fails this bar; "Claudia Malau
  (Windows, device 24): input tracker reporting nothing — 8h active today with
  zero keystrokes, clicks and scrolls. Reinstall the agent or check AV blocking
  bf-idle-tracker" meets it.

  Accepted risk: this reasoning holds while the people acting on alerts can reach
  the MCP. If someone needs to self-serve without it, the UI becomes real work —
  but nothing in M1 blocks adding it later, and the derived reasons would feed it
  unchanged.

## Testing

Per `test-fixture-discipline.md`, each reason needs a test that fails before the
change. The valuable cases are the adversarial ones, not the happy path:

- A never-reported device is **not** flagged (guards the `DEFAULT false` trap).
- A device with no aggregate rows at all is **not** flagged as
  `input_counters_dead` (guards the `fail-closed.md` Rule 4 shape: a read that
  returned nothing is not proof there is nothing).
- A device with **zero keystrokes and zero clicks but non-zero scrolls** is
  **not** flagged. This is the adversarial case — it is the arrangement the
  author would not naturally pick, and it separates a partial failure from a dead
  tracker.
- A device under the 4h active floor is **not** flagged, however quiet.
- A soft-deleted or non-active device is never flagged.

Plus a **contract test** that every reason code the server can emit has a
matching `reasonText()` case in the bot. The bot's `default:` branch is honest
and names the gap rather than fabricating, but it is the drift point, and this is
the Phantom-2 shape — assert against the artifact that receives traffic.

**Acceptance is checkable against reality.** Run against today's fleet, M1 must
produce exactly 3 `window_titles_blind` and 2 `input_counters_dead`, naming those
specific devices. 14 or 0 means the query is wrong.

## Blast radius

- **Migrations run before merge**, not after. Railway auto-deploys `main`, so
  merging is deploying.
- **internal-tool2 is PR-only, no auto-merge.** 31 commits in 7 days across two
  authors, and the agent-health code in scope changed twice in the three days
  before this was written (#2170, #2178). Worktree, explicit paths, and per
  `cross-repo-safety.md` Rule 1 the auto-merge default is suspended.
- The new category is additive. Existing consumers of `telemetry_degraded` and
  `ingest_stalled` see no change, which is the point of not flipping the constant.

## Known open questions

- **Is the class-1 backlog genuinely cleared, or did the fleet upgrade past the
  measurement?** 28 OK against 3 broken is measured on `window_titles_captured_recently`;
  the 38/47 it supersedes was a log sweep. Worth one deliberate reconciliation
  before anyone concludes the onboarding problem is solved.
- **No ack/snooze exists.** `alert_state.suppressed_count` is present but never
  written by this monitor. Nobody can say "yes, we know about that Mac, stop
  telling me" without a code change. A 72h cooldown makes this tolerable, not
  solved.
- **Admin UI remains blind.** `hardware_serial`, `window_titles_captured_recently`,
  `consecutive_sync_failures` and the whole never-reported distinction are stored,
  exposed via MCP, and rendered nowhere. Out of scope here by choice.

## Related

- `docs/superpowers/specs/2026-07-22-window-title-capture-telemetry-design.md` —
  specced the class-1 signal this builds on.
- `rules/fail-closed.md` Rule 4 — the read-returned-nothing trap, which classes 1
  and 3 both sit on.
- `rules/test-fixture-discipline.md` Phantom 2 — the contract-test pattern for the
  server/bot reason-code boundary.
- The macOS background-meeting gap (a Meet detected only while frontmost, mic
  credit off on macOS by the 2026-07-17 usage-only policy) is **deliberately not
  in this spec**. It is a product decision, not a bug, and is tracked separately.
