# Watchdog outcome classification — design

Status: **designed, not scheduled.** Deliberately parked on 2026-07-22 in favour
of higher-value work (see "Priority" below). Pick this up when the noise or the
autofix spend justifies it.

## Problem

`SyncCoordinator._watchdog()` (`src/main.py:1358`) fires from a `threading.Timer`
and knows only that the cycle passed `_DO_SYNC_DEADLINE` (150s). It knows nothing
about *why*. So every overrun is reported identically:

```python
level="error", fingerprint="sync-watchdog-timeout",
message=f"Sync hung — exceeded {self._DO_SYNC_DEADLINE}s watchdog deadline"
```

That label is wrong for the common case. On 2026-07-22 device 18 (Razvan Zerfas,
macOS, agent 1.5.105) ran a cycle of **150.86s** — 0.86s past the deadline — that
was not hung at all:

```
12:35:21 sync.retry   Attempt 1 failed: Cannot connect to BetterFlow API. Retrying in 1.2s...
12:35:52 sync.retry   Attempt 2 failed: Cannot connect to BetterFlow API. Retrying in 2.0s...
12:36:24 sync_engine  Batch not accepted (transient failure: Cannot connect to BetterFlow API) - re-queuing all 1 events
12:36:24 sync_engine  Send budget spent (>=50s) — queuing 2 remaining bucket group(s) for next cycle
12:37:16 main         Sync failed: Cannot connect to BetterFlow API
12:37:20 main  ERROR  _do_sync watchdog: sync exceeded 150s — resetting sessions
12:37:21 main         Sync complete: 0 sent, 3 queued, 10 filtered
```

The v1.5.92 send-budget guard fired correctly and deferred two bucket groups. The
three events were queued and delivered on the next cycle (`0 queued` at 12:37:55).
Nothing was lost. The log has **no gap at all** — the largest quiet stretch inside
the "hang" is ~23s. The root cause was that device's connectivity: `Cannot connect
to BetterFlow API` / `Request timed out` recur from 11:34 to 12:37, and no other
device or project logged anything in that window.

Cost of the mislabel: it pages as an ERROR, and it draws the autofix drafter
(one held draft, $2.63, on a non-problem).

## Signal

`BetterFlowClientError.is_transient` (`src/sync/http_client.py:142`) is already the
single place the codebase decides "network problem vs definitive rejection" — the
queue-durability fix depends on it. Increment a monotonic counter at exactly that
decision point. One writer, integer increment under the GIL, no new lock.

Do not add a second classifier. See `rules/one-rule-one-implementation.md`.

## Classification

`_do_sync` snapshots the counter at cycle start into a local. `_watchdog()` closes
over that snapshot and compares against the live value.

| Counter | Level | Fingerprint | Message |
|---|---|---|---|
| moved | `warning` | `sync-watchdog-timeout-offline` | "Sync slow — exceeded 150s while the BetterFlow API was unreachable (N transient failures this cycle)" |
| unmoved | `error` | `sync-watchdog-timeout` (unchanged) | "Sync hung — exceeded 150s watchdog deadline" (unchanged) |

The rule is deliberately permissive: **any** transient failure during the cycle
downgrades it. A real wedge that coincides with one flaky request gets downgraded
at 150s — acceptable, because `_SYNC_WEDGE_CEILING` still pages it at 420s.

## Unchanged by design

Called out so they read as deliberate, not oversights:

- `bf.reset_session()` / `aw.reset_session()` run in **both** branches. Discarding
  pooled connections after a network outage is the correct recovery, and the
  stale-socket backstop was the watchdog's original purpose.
- `_SYNC_WEDGE_CEILING` (420s, fingerprint `sync-wedged`) is untouched and stays
  ERROR. This is what makes the permissive 150s rule safe.
- `_DO_SYNC_DEADLINE` stays 150s. This is a classification change, not tuning.

## Testing

Against the real `SyncCoordinator._do_sync` with a stub client — assert on captured
reports, never on arguments forwarded between functions
(`rules/test-fixture-discipline.md`):

1. Overrun **with** transient failures → exactly one `warning` capture at
   `sync-watchdog-timeout-offline`, zero `error` captures.
2. Overrun with **zero** transient failures → `error` capture at
   `sync-watchdog-timeout`, unchanged. **Required** — this is the assertion that
   catches the rule being implemented backwards.
3. The counter increments on a transient error and does **not** on a definitive 4xx.

`tests/test_sync_watchdog_budget.py` guards the deadline arithmetic and must stay
green.

## Blast radius

Two files (`src/sync/http_client.py` counter, `src/main.py` classification). No
schema change, no server change, no config flag, no new dependency.

Failure mode if the rule is inverted: a real wedge reports as a warning for 270s
until the 420s ceiling pages it. Test 2 exists to prevent exactly that.

## Priority

Parked 2026-07-22. The issue it silences fires ~once per three days fleet-wide on
data that was never at risk, and half the original value — humans misreading the
alert — was already delivered by betterqa-bot#219, which stopped the alert
presenting a lifetime occurrence counter as a live burst. What remains is mostly
saving autofix spend.

Ranked below: agents running without macOS Accessibility permission, where per-app
attribution is silently dead and real product data goes missing daily.
