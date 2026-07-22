# MDM package deployment for macOS — design

Status: **specced, blocked on a certificate.** Written 2026-07-22 after the PPPC
profile shipped and exposed the other half of the problem.

## Where this came from

On 2026-07-22 a PPPC configuration profile granting Accessibility to
`co.betterqa.betterflow` was created in Miradore and queued to all 31 macOS
devices. That fixed the *permission* half of onboarding — a Mac that receives the
profile will grant Accessibility to BetterFlow automatically, with no user action.

It does not install anything. The profile is a TCC grant keyed to a bundle ID and
code requirement; on a Mac without BetterFlow it sits inert. So a new hire still
receives a DMG by hand and drags it to Applications, and every step after that is
manual.

## The blocker, verified

Three findings, each checked rather than assumed:

| Check | Result |
|---|---|
| `grep -rn "pkgbuild\|productbuild\|\.pkg" Makefile scripts/ .github/workflows/` | **zero hits** — no package build exists |
| `make` targets | `dmg`, `build-mac`, `build-windows`, `appimage` — DMG only on macOS |
| `security find-identity -v \| grep "Developer ID Installer"` | **zero certificates** |

macOS MDM installs software via `InstallEnterpriseApplication`, which takes a
**signed `.pkg`**. A DMG cannot be deployed by MDM. So Miradore has nothing it
could push even if we configured it.

And signing a package requires a **`Developer ID Installer`** certificate, which
is a different type from the `Developer ID Application` certificate the project
already uses (`Developer ID Application: Better Quality Assurance SRL
(87NVC57J44)`, per `scripts/sign-mac.sh`). Having one does not give you the other.

**Creating that certificate is normally restricted to the Account Holder**, which
for this team is ana@betterqa.co — the same account that blocked notarisation in
`memory/apple-agreement-blocks-notarization`. Treat it as a dependency with a
human in it, not a build task.

## Design

### 1. Package build target

Add a `pkg` target alongside `dmg`, taking the same signed+notarised
`dist/BetterFlow.app` as input:

```
pkgbuild  --component dist/BetterFlow.app --install-location /Applications \
          --identifier co.betterqa.betterflow --version $(VERSION) component.pkg
productbuild --distribution ... --package-path ... unsigned.pkg
productsign --sign "Developer ID Installer: Better Quality Assurance SRL (87NVC57J44)" \
          unsigned.pkg dist/BetterFlow-macOS-$(TARGET_ARCH).pkg
```

The pkg needs its **own** notarisation pass — notarising the .app does not cover
the package that contains it. Reuse `scripts/notarize-mac.py`, then staple the
pkg.

Keep the DMG. It stays the download for anyone outside MDM and for the 3
unmanaged devices, which cannot receive a pkg by definition.

### 2. Who owns the version — decide this BEFORE shipping

The agent already self-updates (`AGENT_MINIMUM_VERSION`, the staged relaunch
path). If MDM also manages the version, two systems fight over the same install
and the loser reinstalls on the next check-in. That is the version-churn failure
mode already seen on a Windows device where the app updated from a Downloads
folder (`memory/window-tracker-restart-churn`, v1.5.91).

**Recommended:** MDM installs the app **once** on a new machine, and never
touches it again. The in-app updater keeps it current, as today. MDM is the
bootstrap, not the update channel. That keeps one owner for "which version is
running" and leaves the existing, well-tested update path in charge.

The alternative — MDM owns the version, agent auto-update disabled — is
defensible for a locked-down fleet, but it means every release needs a pkg
rebuild and a Miradore upload before any device gets it, and the fleet-push lever
(`AGENT_MINIMUM_VERSION`) stops working. Do not do both.

### 3. Miradore side

Upload the signed pkg under Management → Applications, then deploy to the macOS
device group. Pair it with the existing `BetterFlow - Accessibility (PPPC)`
profile so a newly enrolled Mac gets the app and its permission together.

## Ordering

1. Certificate created (blocked — Account Holder).
2. `pkg` build target + notarisation, verified by installing the pkg on a clean
   Mac and confirming the app launches signed and notarised.
3. Version-ownership decision recorded before anything reaches Miradore.
4. Upload + deploy to the macOS group.

Steps 2-4 are a day's work. Step 1 is a five-minute task for whoever holds the
role, and everything waits on it.

## Verification

- `spctl -a -vv -t install dist/BetterFlow-*.pkg` reports accepted and notarised.
- Install on a Mac that has never had BetterFlow; confirm the app is in
  /Applications, launches, and — with the PPPC profile present — reports
  Accessibility as granted with no user interaction. That combination is the
  actual deliverable; either half alone is not.
- Confirm the installed build still self-updates, i.e. the MDM install did not
  produce something the updater refuses to replace.

## Related

- `docs/superpowers/specs/2026-07-22-window-title-capture-telemetry-design.md` —
  the signal that tells you whether any of this worked, on any platform.
- `memory/apple-agreement-blocks-notarization` — the last time an Apple account
  role blocked a macOS release.
