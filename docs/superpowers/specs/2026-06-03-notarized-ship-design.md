# Notarized Developer ID ship — BetterFlow Sync (macOS)

**Status:** Draft — pending user approval
**Author:** Tudor Brad + Claude
**Date:** 2026-06-03
**Team:** Better Quality Assurance SRL (Apple Team ID `87NVC57J44`)

---

## 1. Goal & non-goals

### Goal
Replace the current adhoc-signed `BetterFlow.app` with a build that is:

1. Signed by **`Developer ID Application: Better Quality Assurance SRL (87NVC57J44)`**
2. Built with the hardened runtime + the existing PyInstaller entitlements
3. Notarized and stapled by Apple's notary service
4. Distributed as a notarized + stapled DMG via **GitHub Releases**
5. Consumed by the existing in-app `self_updater` for ongoing updates

### Why
The current build is adhoc-signed (`Signature=adhoc`, `TeamIdentifier=not set`). Every rebuild produces a new code-directory hash, which macOS TCC treats as a new app, forcing users to re-grant Input Monitoring + Accessibility on every update. Signing with a stable Developer ID keeps the TCC subject identity stable across rebuilds, and notarization removes Gatekeeper "unidentified developer" friction.

### Non-goals
| Out of scope | Why deferred |
| --- | --- |
| Mac App Store distribution | Requires App Sandbox + rearchitecting subprocess trackers + removing the self-updater. Decided in brainstorm Q1. |
| CI-based signing | Local-only is sufficient for now. Decided in brainstorm Q2. |
| Sparkle migration | Existing `self_updater` already verifies signature, team ID, version. Decided in brainstorm Q3. |
| Windows code signing | Requires a separate EV cert from a CA (~$300/yr). Tracked as a follow-up. |
| Linux signing | AppImage has no notarization equivalent; current path stays. |
| Universal2 / x86_64 macOS builds | Tudor's signing Mac is Apple Silicon. Ship arm64 first; x86_64 cross-build is a follow-up if user base demands it. |

---

## 2. Prerequisites (one-time, manual)

These happen once on Tudor's Mac. They are inputs to the automated build flow, not part of the build itself.

1. **Sign Xcode into `brad@betterqa.eu`** (already done — verified Team `87NVC57J44`, role Admin).
2. **Request a Developer ID Application certificate** via Xcode → Settings → Accounts → Better Quality Assurance SRL → Manage Certificates → "+" → "Developer ID Application". Verify it lands in the login Keychain with common name `Developer ID Application: Better Quality Assurance SRL (87NVC57J44)`.
3. **Generate an app-specific password** for `brad@betterqa.eu` at appleid.apple.com → Sign-In and Security → App-Specific Passwords. Label it something like "notarytool-betterflow".
4. **Store notarytool credentials in the Keychain** (one command, then it is reusable forever):
   ```
   xcrun notarytool store-credentials betterqa \
       --apple-id brad@betterqa.eu \
       --team-id 87NVC57J44 \
       --password <app-specific-password>
   ```
   The profile name `betterqa` is what the Makefile will reference. No secrets land in the repo.
5. **Verify the cert is usable for signing:**
   ```
   security find-identity -v -p codesigning | grep "87NVC57J44"
   ```
   Should print one line containing `Developer ID Application`.

---

## 3. Component changes

### 3.1 `Makefile`

Three targets change:

**`sign-mac`** — switches from opt-in env-var lookup to the explicit Developer ID identity. Drops `--timestamp=none` (which would break notarization — Apple requires a secure timestamp). Signs from the inside out rather than relying on `--deep` (deprecated for sealing nested bundles since macOS 11), so the bundled ActivityWatch tracker binaries and Python frameworks get sealed correctly. Steps:

1. Find every Mach-O file inside `dist/BetterFlow.app/Contents` (frameworks, dylibs, embedded helper executables).
2. Sign each one with the same identity, hardened runtime, and entitlements.
3. Sign the outer `BetterFlow.app` last.
4. Verify with `codesign --verify --deep --strict --verbose=2 dist/BetterFlow.app`.
5. Verify hardened runtime is on with `codesign -d --verbose=4 dist/BetterFlow.app | grep "runtime"`.

Identity is hardcoded as `Developer ID Application: Better Quality Assurance SRL (87NVC57J44)`. No env-var indirection — this app only has one valid signing identity for distribution.

**`notarize-mac`** (NEW) — submits the signed DMG (not the .app) to Apple's notary service, waits synchronously for the verdict, and exits non-zero on rejection:

```
xcrun notarytool submit dist/BetterFlow.dmg \
    --keychain-profile betterqa \
    --wait
```

`--wait` blocks until Apple returns "Accepted" or "Invalid", typically 1–10 minutes. On `Invalid`, the script pulls the log with `xcrun notarytool log <submission-id> --keychain-profile betterqa` and prints it before failing.

**`staple-mac`** (NEW) — staples the notarization ticket onto the DMG and the .app:

```
xcrun stapler staple dist/BetterFlow.dmg
xcrun stapler staple dist/BetterFlow.app
xcrun stapler validate dist/BetterFlow.dmg
xcrun stapler validate dist/BetterFlow.app
```

Stapling embeds the notary ticket so Gatekeeper does not need a network call to verify on first launch.

**`dmg`** — restructured pipeline becomes:

```
build-mac  →  sign-mac  →  create-dmg (existing)  →  notarize-mac  →  staple-mac
```

Failure at any step aborts the rest. The DMG is signed by stapling alone; the `.app` inside it is signed + stapled per the steps above.

### 3.2 `build.spec`

Minor change only:

- Confirm `bundle_identifier="co.betterqa.betterflow"` stays (already correct, line 195).
- Confirm `LSMinimumSystemVersion: "10.15"` stays (10.15 is the floor for hardened runtime + notarization).
- Confirm `NSAppleEventsUsageDescription` stays (required for accessibility-style app metadata extraction).
- `codesign_identity` and `entitlements_file` on the `EXE()` call stay `None` — we sign post-build in the Makefile rather than during PyInstaller (clearer pipeline, easier to retry just the sign step).

No structural changes.

### 3.3 `resources/entitlements.mac.plist`

**No changes.** The current set is the correct minimum for a PyInstaller-bundled Python app distributed via Developer ID:

- `com.apple.security.cs.disable-library-validation` — required (Python loads unsigned dylibs)
- `com.apple.security.cs.allow-dyld-environment-variables` — required (PyInstaller bootloader exports DYLD_*)
- `com.apple.security.cs.allow-unsigned-executable-memory` — required (some C extensions allocate W+X pages)
- `com.apple.security.cs.allow-jit` — defense-in-depth on Apple Silicon

These are all permitted under Developer ID + notarization. They would all be **rejected** under Mac App Store sandbox, which is why MAS is out of scope.

### 3.4 `self_updater`

**No code changes.** The existing logic already:

- Verifies `codesign --verify --deep --strict` on the staged update (line 491)
- Rejects signed → unsigned downgrade (line 518)
- Rejects team-ID disappearance (line 524)
- Rejects team-ID mismatch against current install (line 528)
- Rejects version downgrade (line 535)

The current adhoc → Developer ID transition is safe: the current install has `team_id = None`, so the team-ID-mismatch check is short-circuited by the `current.team_id and …` guard. From the first Developer ID build onward, the team ID is pinned to `87NVC57J44` by virtue of being the team that signed the running app.

**Optional defense-in-depth (recommended but skippable):** add a constant `EXPECTED_TEAM_ID = "87NVC57J44"` in `self_updater.py` and refuse any update whose `new.team_id != EXPECTED_TEAM_ID`. This prevents a malicious update server from serving a build signed by a different (but valid) Developer ID team. Low risk to ship later if we want to keep this change focused.

### 3.5 `.github/workflows/build.yml`

**No changes in this spec.** CI continues to produce unsigned dev artifacts for PR validation. Releases are produced locally on Tudor's Mac, then uploaded with `gh release create`.

If the existing CI release-on-tag step would conflict with the local release, we either disable that step in the workflow or just let CI create a draft release and overwrite its assets with the local stapled DMG. (Read the current workflow before deciding — see plan-phase TODO.)

---

## 4. Build pipeline (end-to-end)

Steps a release runs through, with success criteria for each:

| # | Step | Command | Success criterion |
| --- | --- | --- | --- |
| 1 | Bump version | edit `src/__init__.py` | new version > previous |
| 2 | Build .app | `make build-mac` | `dist/BetterFlow.app` exists |
| 3 | Sign all nested binaries + outer bundle | `make sign-mac` | `codesign --verify --deep --strict` returns 0 |
| 4 | Verify hardened runtime | `codesign -d --verbose=4 dist/BetterFlow.app` | output contains `flags=0x10000(runtime)` |
| 5 | Build DMG | existing `create-dmg` invocation | `dist/BetterFlow.dmg` exists |
| 6 | Notarize | `make notarize-mac` | notarytool returns `status: Accepted` |
| 7 | Staple | `make staple-mac` | `xcrun stapler validate dist/BetterFlow.dmg` returns 0 |
| 8 | Gatekeeper sanity check | `spctl -a -vv -t install dist/BetterFlow.dmg` | output contains `source=Notarized Developer ID` |
| 9 | Tag + push | `git tag vX.Y.Z && git push --tags` | tag visible on GitHub |
| 10 | Create GitHub Release | `gh release create vX.Y.Z dist/BetterFlow.dmg --notes-from-tag` | release URL returned, DMG attached |

The whole sequence after the version bump is a single `make dmg` call once the targets are wired up.

---

## 5. Distribution

- **Source of truth:** GitHub Releases on `Better-Quality-Assurance/betterflow-sync` (or wherever this repo lives — confirm in plan phase).
- **Update channel:** `self_updater` polls the GitHub Releases API for the latest tag, downloads the DMG, verifies signature + team + version, mounts, copies the .app into place, relaunches.
- **First migration:** existing users on the adhoc-signed v1.5.25 will get the first Developer ID release via the normal in-app updater (analysis in §3.4). No manual reinstall required.

---

## 6. Verification / acceptance criteria

Pass criteria for considering this work shipped:

1. `dist/BetterFlow.dmg` from `make dmg` is signed by Team `87NVC57J44` and notarized.
2. `xcrun stapler validate dist/BetterFlow.dmg` exits 0.
3. `spctl -a -vv -t install dist/BetterFlow.dmg` exits 0 with `source=Notarized Developer ID`.
4. After installing from the notarized DMG to `/Applications`, the app launches without any Gatekeeper warning on a fresh Mac (or one where TCC has been reset for `co.betterqa.betterflow`).
5. After granting Input Monitoring + Accessibility once, rebuilding with `make dmg` and reinstalling does NOT cause macOS to require re-granting. This is the original problem this whole exercise solves.
6. The in-app self-updater successfully applies an update from this signed build to a subsequent signed build with the same team ID.
7. All 212 existing tests still pass (`make test`).
8. No new secrets land in the repo (cert + notarytool password live in the Keychain only).

---

## 7. Risks + mitigations

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| First notarization rejected because of bundled binaries (aw-server-rust, bf-window-tracker, bf-idle-tracker) being unsigned or missing hardened runtime | Medium | Build pipeline blocked | Sign from the inside out (per §3.1) rather than relying on `--deep`. Read notarytool log on `Invalid`, iterate. |
| Notary service slow (10+ minutes) blocks `make dmg` | Low | Slower releases | `notarytool submit --wait` is fine for occasional releases. If it becomes painful, split into `notarize-mac-submit` (non-blocking) + `notarize-mac-wait`. |
| App-specific password revoked or expires | Low | Notarization fails | Regenerate at appleid.apple.com and re-run `notarytool store-credentials betterqa …`. No code change. |
| Developer ID cert expires (~5 years from issuance) | Long-term | Cannot ship new builds; existing installs unaffected | Calendar reminder for renewal. Stapled tickets keep old releases trusted indefinitely. |
| `--deep` style signing leaves a nested binary unsigned, notarization fails | Medium | Iteration loop | Inside-out signing addresses this. If a missed file shows up in the notary log, add it to the explicit sign list. |
| User on the current adhoc-signed v1.5.25 has a corrupted code-signature record and update verifier rejects | Low | One-off user pain | Document fallback: download the DMG manually and reinstall. |

---

## 8. Open questions / decisions to revisit at plan-phase

1. Does the existing CI release-on-tag step conflict with the local release flow? (Need to read the full `build.yml` past line 80 to know.)
2. Should we add the optional `EXPECTED_TEAM_ID` pinning constant to `self_updater.py` now or as a follow-up?
3. Does the `create-dmg` tool need any flags adjusted to produce a notarization-friendly DMG (no APFS-only, sufficient padding)?
4. Should `make dmg` continue to default to `arch arm64` only on Tudor's Mac, or attempt x86_64 too via Rosetta? (Lean: arm64 only for first ship.)

---

## 9. Out-of-scope follow-ups

- Windows code signing (EV cert from Sectigo/DigiCert, ~$300/yr).
- CI-based signing + automated release on tag push.
- Universal2 macOS builds (single binary supporting arm64 + x86_64).
- Mac App Store rearchitecture (sandbox + non-PyInstaller bundling + remove self-updater).
- Linux package signing (Flatpak/Snap signed via their respective key flows).
