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
| Universal2 single-binary macOS builds | Universal2 requires every bundled wheel + native dep to also be universal2; PyInstaller universal2 support is patchy. Ship two separate DMGs instead (see §4 dual-arch pipeline). |

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

**Existing logic — no change needed for the core flow.** The current code already:

- Verifies `codesign --verify --deep --strict` on the staged update (line 491)
- Rejects signed → unsigned downgrade (line 518)
- Rejects team-ID disappearance (line 524)
- Rejects team-ID mismatch against current install (line 528)
- Rejects version downgrade (line 535)

The current adhoc → Developer ID transition is safe: the current install has `team_id = None`, so the team-ID-mismatch check is short-circuited by the `current.team_id and …` guard. From the first Developer ID build onward, the team ID is pinned to `87NVC57J44` by virtue of being the team that signed the running app.

**One small additive change: pin the expected team ID.** Add a module-level constant:

```python
EXPECTED_TEAM_ID = "87NVC57J44"  # Better Quality Assurance SRL
```

In `_verify_codesign`, after extracting `new.team_id`, reject any update where `new.team_id != EXPECTED_TEAM_ID`. This means even a fresh install with a tampered first DMG cannot be tricked into accepting a binary signed by a different (but otherwise valid) Apple Developer ID team. The current check only catches a team-ID *change* between two installs; pinning catches a wrong team on first install too. The auto-updater is the highest-risk surface of the app for remote code execution, so this hardening is worth the ~3 lines.

### 3.5 `.github/workflows/build.yml`

**No changes in this spec.** CI continues to produce unsigned dev artifacts for PR validation. Releases are produced locally on Tudor's Mac, then uploaded with `gh release create`.

If the existing CI release-on-tag step would conflict with the local release, we either disable that step in the workflow or just let CI create a draft release and overwrite its assets with the local stapled DMG. (Read the current workflow before deciding — see plan-phase TODO.)

---

## 4. Build pipeline (end-to-end, dual-arch)

The signing Mac is Apple Silicon. Both arm64 and x86_64 builds happen on it:

- **arm64 build:** native — `.venv-arm64/bin/python -m PyInstaller build.spec --clean`
- **x86_64 build:** Rosetta-emulated — `arch -x86_64 .venv-x86_64/bin/python -m PyInstaller build.spec --clean` (the existing `.venv-x86_64` is a universal2 Python venv with x86_64 wheels; both venvs already exist in the repo)

Each architecture produces a separately-signed, separately-notarized DMG. The asset names must match `update_checker.py`'s arch-detection pattern (`grep "arch in name"`, line 90): `BetterFlow-macOS-arm64.dmg` and `BetterFlow-macOS-x86_64.dmg`.

Full per-release sequence:

| # | Step | Command (conceptual) | Success criterion |
| --- | --- | --- | --- |
| 1 | Bump version | edit `src/__init__.py` | new version > previous |
| 2 | Build arm64 .app | `TARGET_ARCH=arm64 make build-mac` | `dist/BetterFlow.app` is `Mach-O thin arm64` |
| 3 | Sign arm64 (inside-out + outer) | inside `build-mac` via updated `sign-mac` | `codesign --verify --deep --strict` returns 0 |
| 4 | Package + name arm64 DMG | `make dmg` produces `dist/BetterFlow-macOS-arm64.dmg` | file exists at the arch-suffixed path |
| 5 | Notarize arm64 DMG | `xcrun notarytool submit … --wait` | `status: Accepted` |
| 6 | Staple arm64 DMG + .app | `xcrun stapler staple` | `stapler validate` returns 0 on both |
| 7 | Clean `dist/`, repeat steps 2-6 for x86_64 | `arch -x86_64` everywhere | second DMG at `BetterFlow-macOS-x86_64.dmg`, stapled |
| 8 | Gatekeeper sanity check on each | `spctl -a -vv -t install <dmg>` | both report `source=Notarized Developer ID` |
| 9 | Tag + push | `git tag vX.Y.Z && git push --tags` | tag visible on GitHub |
| 10 | Create GitHub Release with both DMGs | `gh release create vX.Y.Z dist/BetterFlow-macOS-arm64.dmg dist/BetterFlow-macOS-x86_64.dmg --notes-from-tag` | release has both assets |

A new convenience target `make ship` will orchestrate steps 2–7 (both architectures) end-to-end. Tagging + GitHub Release creation remain manual gates so a release is never auto-cut from a dirty working copy.

**Note on Rosetta dependency:** Apple has signalled Rosetta will be removed in a future macOS, but as of macOS 26 it's still available as an optional install. Once Rosetta retires on the signing Mac, x86_64 builds must move to CI (a x86_64 GitHub Actions runner like the existing `macos-14-large` matrix entry).

---

## 5. Distribution

- **Source of truth:** GitHub Releases on this repo's GitHub remote (confirm exact `<org>/betterflow-sync` slug in plan phase).
- **Release assets:** both `BetterFlow-macOS-arm64.dmg` and `BetterFlow-macOS-x86_64.dmg` attached to every release. (Windows ZIP + Linux AppImage continue from CI as before — out of scope for this spec.)
- **Update channel:** `self_updater` polls the GitHub Releases API for the latest tag. `update_checker._find_platform_asset` (lines 54–106) already filters by `platform.machine()` for `arm64` vs `x86_64` in the asset name, so each Mac auto-picks the right DMG.
- **First migration:** existing users on the adhoc-signed v1.5.25 will get the first Developer ID release via the normal in-app updater (analysis in §3.4). No manual reinstall required.

---

## 6. Verification / acceptance criteria

Pass criteria for considering this work shipped:

1. Both `dist/BetterFlow-macOS-arm64.dmg` and `dist/BetterFlow-macOS-x86_64.dmg` from `make ship` are signed by Team `87NVC57J44` and notarized.
2. `xcrun stapler validate` exits 0 on both DMGs and on the .app inside each.
3. `spctl -a -vv -t install <each dmg>` exits 0 with `source=Notarized Developer ID`.
4. After installing from either notarized DMG to `/Applications`, the app launches without any Gatekeeper warning on a fresh Mac (or one where TCC has been reset for `co.betterqa.betterflow`).
5. After granting Input Monitoring + Accessibility once, rebuilding with `make ship` and reinstalling does NOT cause macOS to require re-granting. This is the original problem this whole exercise solves.
6. The in-app self-updater on an arm64 Mac downloads the arm64 DMG (not the x86_64 one), and vice versa. Tested by inspecting `update_checker._find_platform_asset` output, not just blind faith in the regex.
7. The pinned `EXPECTED_TEAM_ID = "87NVC57J44"` in `self_updater.py` rejects a hand-crafted DMG signed by a different Apple Developer ID team. Tested with a unit test that mocks `_get_signing_info` to return a different team.
8. All 212 existing tests still pass (`make test`), plus the new team-ID-pin test.
9. No new secrets land in the repo (cert + notarytool password live in the Keychain only).

---

## 7. Risks + mitigations

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| First notarization rejected because of bundled binaries (aw-server-rust, bf-window-tracker, bf-idle-tracker) being unsigned or missing hardened runtime | Medium | Build pipeline blocked | Sign from the inside out (per §3.1) rather than relying on `--deep`. Read notarytool log on `Invalid`, iterate. |
| Notary service slow (10+ minutes per DMG; up to 20 min wall-clock for both archs) blocks `make ship` | Low | Slower releases | Submit both DMGs in parallel via `notarytool submit --wait` in background subshells, then join. If it stays painful, split into submit/wait targets. |
| Rosetta unavailable on signing Mac (future macOS deprecates it) | Low (today) → High (future) | x86_64 builds fail locally | Move x86_64 build to CI (`macos-14-large` runner already in the matrix). Spec deferred this; revisit when Apple announces a removal date. |
| x86_64 wheels for some dep have been dropped upstream | Low | x86_64 venv install fails | Pin compatible older versions in a second `requirements-x86_64.txt` if it happens. Most BetterFlow deps are pure-Python or have both wheels through 2027+. |
| App-specific password revoked or expires | Low | Notarization fails | Regenerate at appleid.apple.com and re-run `notarytool store-credentials betterqa …`. No code change. |
| Developer ID cert expires (~5 years from issuance) | Long-term | Cannot ship new builds; existing installs unaffected | Calendar reminder for renewal. Stapled tickets keep old releases trusted indefinitely. |
| `--deep` style signing leaves a nested binary unsigned, notarization fails | Medium | Iteration loop | Inside-out signing addresses this. If a missed file shows up in the notary log, add it to the explicit sign list. |
| User on the current adhoc-signed v1.5.25 has a corrupted code-signature record and update verifier rejects | Low | One-off user pain | Document fallback: download the DMG manually and reinstall. |

---

## 8. Open questions / decisions to revisit at plan-phase

1. Does the existing CI release-on-tag step conflict with the local release flow? (Need to read the full `build.yml` past line 80 to know — if it auto-creates a release with unsigned CI artifacts on tag push, we either disable that step or have the local flow overwrite assets.)
2. Does the `create-dmg` tool need any flags adjusted to produce a notarization-friendly DMG (no APFS-only, sufficient padding for codesign metadata)?
3. Should arm64 and x86_64 notarizations run in parallel (faster, more complex shell) or serially (simpler, ~20 min total)? Lean: serial first, parallelize if it gets painful.
4. The current `make dmg` target produces `dist/BetterFlow.dmg`. The new `make ship` target needs arch-suffixed output names. Does anything else (CI, install scripts, docs) reference the legacy name? Spot-check before renaming.

---

## 9. Out-of-scope follow-ups

- Windows code signing (EV cert from Sectigo/DigiCert, ~$300/yr).
- CI-based signing + automated release on tag push.
- Universal2 single-binary macOS builds (single .app with both archs fat-merged).
- Migrate x86_64 builds to CI before Rosetta deprecation.
- Mac App Store rearchitecture (sandbox + non-PyInstaller bundling + remove self-updater).
- Linux package signing (Flatpak/Snap signed via their respective key flows).
