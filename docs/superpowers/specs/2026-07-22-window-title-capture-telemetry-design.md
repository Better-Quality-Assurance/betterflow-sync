# Cross-platform window-title capture telemetry — design

Status: **specced, not built.** Written 2026-07-22 off the back of a fleet sweep
that should never have needed to happen.

## Problem

Nothing tells us when an agent stops capturing window titles. Tracked time keeps
flowing, app attribution keeps working, categorization keeps working — and the
title detail silently goes missing, indefinitely, with no signal anywhere.

On 2026-07-22 a manual sweep found **14 of the 18 macOS devices we could measure
(78%)** running without Accessibility permission, so `AXIsProcessTrusted()` is
false and every window title is empty. Two of the affected users had been
onboarded the previous day. Confirmed healthy: 4. Undeterminable because the
device never delivered a log: 11.

Finding that out cost four parallel subagents, 29 remote log requests and ~15
minutes of wall-clock, and it still left 38% of the fleet unmeasured. It is not
repeatable, and it does not scale to "is this still true next month".

### Why a macOS permission check is the wrong fix

The same user-visible symptom has a different cause per platform:

| Platform | Cause of missing titles | Permission involved |
|---|---|---|
| macOS | Accessibility not granted (`AXIsProcessTrusted()` false) | yes |
| Windows | bundled `bf-window-tracker` stalled/blind | **none** — `GetWindowText` needs no consent |
| Linux | X11 watcher not running / no window manager support | none |

Building a per-platform cause detector means three detectors, three ways to be
wrong, and nothing at all for the next platform. Windows has no permission to
check, so a "Windows version of the Accessibility check" would detect a fault
that cannot exist there.

## Design: report the symptom, not the cause

Add one field to the existing heartbeat health payload
(`AWManager.get_health_snapshot()`, `src/aw_manager.py:1014`, which already
carries `window_tracker_blind`, `window_event_age_seconds`, `inproc_afk`):

```
"window_titles_captured_recently": <bool | null>
```

Definition: of the window events observed in the last **15 minutes**, at least
one had a non-empty title.

- `true` — titles are arriving. Platform-independent, cause-independent.
- `false` — window events exist but every one has an empty title. This is the
  finding: macOS permission missing, Windows tracker blind, Linux watcher dead.
- `null` — no window events at all in the window. Distinct from `false` on
  purpose: "not tracking" is a different fault from "tracking without titles",
  and `window_event_age_seconds` already covers the former. Never collapse these
  into one boolean.

### Why a boolean over a ratio

A ratio (`empty_titles / total`) is tempting and worse. Some apps legitimately
report empty titles (a screensaver, a login window, an app mid-launch), so any
threshold on a ratio needs tuning per platform and per app mix, and a tuned
threshold is a thing that silently drifts. "Did ANY title arrive in 15 minutes"
has no threshold to get wrong: on a working machine the answer is yes within one
tick; on a broken one it is no, forever.

## Server side

1. Persist the field on `agent_devices` alongside the existing health columns.
2. Surface it as a column on `/admin/agents/fleet`. That column is the entire
   deliverable — it replaces the sweep this spec was born from.
3. **Do not alert on it initially.** With 78% of the Mac fleet currently failing,
   an alert on day one is a 14-device page storm that teaches everyone to ignore
   it. Ship the column, fix the backlog by hand, and only then consider a
   threshold alert once the normal state is "nearly all true".

## Onboarding (the actual root cause)

The telemetry tells you the fleet is broken; it does not stop the next hire
joining it broken. Two users onboarded 2026-07-21 were already affected on
2026-07-22, so the setup flow is not getting this permission granted.

The setup wizard already handles an Input Monitoring disclosure. macOS
Accessibility needs the same treatment: request it explicitly, show whether it
was actually granted, and re-check on later launches rather than asking once and
assuming. `src/sync/macos_window_watcher.py:358` already re-checks
`AXIsProcessTrusted()` periodically and logs the transition — that check exists
and nothing acts on it.

Scope this as a **separate change** from the telemetry. They fix different halves
(stop the bleeding vs. stop new bleeding) and shipping them together makes both
harder to verify.

## Testing

- `window_titles_captured_recently` is `true` when a window event carries a
  non-empty title, `false` when events exist but all titles are empty, and
  `null` when there are no window events. Three cases, asserted on the built
  payload, not on arguments forwarded between functions.
- A macOS case with `AXIsProcessTrusted()` stubbed false produces `false`, not
  `null` — the distinction that makes the field actionable.
- The field survives the heartbeat envelope round-trip. See
  `memory/heartbeat-response-envelope-bug` — a whole generation of heartbeat
  features silently no-op'd because the agent read fields at the wrong nesting
  level and the tests mocked the wrong shape.
- **Name the consumer before merging.** A telemetry field with no reader is the
  write-with-no-reader defect in `rules/one-rule-one-implementation.md`. The
  dashboard column ships with the field or the field does not ship.

## Blast radius

Agent: one field in an existing payload, no new thread, no new permission, no new
dependency. Server: one column, one dashboard cell. No migration risk beyond an
additive column. Nothing in the billing path — this field must never influence
tracked or billed time, only visibility.

## Known open question

Windows devices went unmeasured in the 2026-07-22 sweep because there was nothing
to measure. After this ships they will report like everything else, and Sachi's
recurring app-attribution gap (device 16, a stalled `bf-window-tracker` — see
`memory/window-tracker-restart-churn`) should become visible as `false` rather
than being inferred from an alert. Treat that as the first validation case: if it
does not light up for her, the field is not measuring what this spec claims.

## Related

- `memory/app-name-vs-window-title-signals` — why app-level aggregates cannot
  detect this, and how a fleet sweep scored the known-positive control HEALTHY.
- `docs/superpowers/specs/2026-07-22-watchdog-outcome-classification-design.md` —
  the other parked design from the same day.
