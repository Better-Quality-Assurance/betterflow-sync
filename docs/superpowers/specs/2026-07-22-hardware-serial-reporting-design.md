# Hardware serial reporting — design

Status: **specced 2026-07-22**, agent side ready to build, server side bundled with
the window-title telemetry column.

## Problem

The MDM (Miradore) and the BetterFlow agent fleet describe the same laptops and
share **no common key**. Miradore identifies a Mac by hardware serial
(`C02Z60U3LVCJ`); the agent identifies itself as `sync:<uuid>` generated at first
run, with no tie to the hardware. Nothing joins them.

Consequences, all hit on 2026-07-22:

- **"Who runs the agent on a machine MDM doesn't manage?" is unanswerable.**
  Tiberiu Onisor was found running the agent with *no Miradore record at all* —
  outside the Accessibility profile and outside every other policy (FileVault,
  password). Six more current staff are suspected in the same position but could
  only be identified by fuzzy name matching across two systems that disagree on
  name order, so they remain unconfirmed.
- **"Was this leaver's laptop reassigned?" needed a manual serial hunt** through
  the MDM console, one device at a time.
- The only cross-reference available was by user name, which is exactly the kind
  of instrument that produces a confident wrong answer.

A serial makes both queries a join.

## Design

Collect the hardware serial once at startup, cache it, and report it on the
existing heartbeat alongside the other device metadata.

| Platform | Source |
|---|---|
| macOS | `IOPlatformSerialNumber` from `IOPlatformExpertDevice` (IOKit; no permission required) |
| Windows | `Win32_BIOS.SerialNumber` via WMI |
| Linux | `/sys/class/dmi/id/product_serial` (often root-only — degrade to `None`) |

Report as `hardware_serial: str | None`. **`None` is a first-class value**, not an
error: a VM, a container, a locked-down Linux box, or a failed probe all legitimately
have no serial, and the field must never block a heartbeat or affect sync.

Collect **once at startup and cache** — the serial cannot change for the life of
the machine, so probing per-heartbeat is waste. A failed probe caches `None` and
does not retry in a loop.

## Privacy — read before building

This is a **new category of data leaving the device**, and the agent's privacy
model is documented publicly (this repo is public; see `CLAUDE.md` §Privacy Model
and the in-app disclosure).

- A hardware serial is a stable, unique device identifier that survives OS
  reinstall. It is not personal data about the *user*, but it is a durable
  identifier for company-owned hardware.
- Scope is asset correlation only: joining the fleet to the MDM inventory. It must
  not be used to identify individuals, and it must never reach a client-facing
  surface.
- **`CLAUDE.md`'s privacy section and any user-facing disclosure MUST be updated
  in the same PR.** Shipping an undisclosed new identifier in a public repo whose
  documentation enumerates exactly what is sent is not acceptable — the docs
  currently give a specific list, and this would silently make that list wrong.
  Compare the `hash_titles` precedent, where the documented behaviour and the
  actual behaviour diverged and had to be corrected.

## Server side — bundle it, do not ship a write with no reader

Add `hardware_serial` to `agent_devices` and surface it in the
`betterflow_agent_devices` MCP output so the join is queryable.

**Bundle this with the `window_titles_captured_recently` column** from
`docs/superpowers/specs/2026-07-22-window-title-capture-telemetry-design.md`
(PR #152, currently blocked for exactly this reason). Both are heartbeat fields
needing the same migration + read path in internal-tool2. One server change,
two fields, and both agent PRs unblock together.

Until the reader exists, neither agent PR merges. A telemetry field with no
consumer is the write-with-no-reader defect in `rules/one-rule-one-implementation.md`.

## Testing

- macOS probe returns the real serial; assert format is plausible and non-empty
  on a real machine, and that it matches `ioreg`'s value.
- A failing probe (patched to raise) yields `None`, is cached, and does **not**
  re-probe on subsequent calls.
- `None` survives the heartbeat envelope — check the payload on the wire, not the
  built dict. **Add the key to the `bf_client` heartbeat forwarding whitelist**;
  #152 found the hard way that a field absent from that list is silently dropped
  at the wire boundary, and membership must be tested with `in`, not truthiness,
  or `None` disappears.
- The field never influences tracked or billed time.

## Payoff

Once both sides ship, "which agent devices are not in MDM?" is a serial join
instead of an afternoon of console scraping — and it stays answerable as staff
join and leave, rather than needing to be re-derived by hand each time.

## Related

- `docs/superpowers/specs/2026-07-22-window-title-capture-telemetry-design.md`
- `memory/app-name-vs-window-title-signals` — the sweep that had no control and
  was voided; same family of "confident answer about the wrong thing".
