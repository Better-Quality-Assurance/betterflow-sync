#!/usr/bin/env bash
# Inside-out codesign for dist/BetterFlow.app.
#
# Signs every nested Mach-O binary first (frameworks, dylibs, helper
# executables), then seals the outer bundle. `--deep` is unreliable
# for PyInstaller bundles since macOS 11 — Apple recommends per-binary
# signing for notarization.
#
# Identity is hardcoded: this app has exactly one valid signing
# identity for distribution. No env-var indirection.

set -euo pipefail

IDENTITY="Developer ID Application: Better Quality Assurance SRL (87NVC57J44)"
ENTITLEMENTS="resources/entitlements.mac.plist"
APP="${1:-dist/BetterFlow.app}"

if [ ! -d "$APP" ]; then
    echo "[sign-mac] $APP not found" >&2
    exit 1
fi

if [ ! -f "$ENTITLEMENTS" ]; then
    echo "[sign-mac] Entitlements file not found: $ENTITLEMENTS" >&2
    echo "[sign-mac] Run this script from the repo root, not from scripts/" >&2
    exit 1
fi

if ! security find-identity -v -p codesigning | grep -q "87NVC57J44"; then
    echo "[sign-mac] Developer ID Application cert for Team 87NVC57J44 not in Keychain" >&2
    echo "[sign-mac] See docs/SIGNING.md for setup" >&2
    exit 1
fi

# Refuse to sign if the dist app we're about to mutate is currently
# running — codesign --force on an mmap'd binary races and can produce
# subtly-broken signatures that notarization still accepts but
# Gatekeeper later rejects on launch.
if pgrep -f "$APP/Contents/MacOS/BetterFlow" > /dev/null; then
    echo "[sign-mac] $APP is running — kill it before signing" >&2
    exit 1
fi

echo "[sign-mac] Signing nested binaries inside $APP"

# Process-substitution form keeps the `while` body in the parent shell.
# A pipe ( find | while ) puts the loop in a subshell where `set -e`
# cannot abort the outer script — a single codesign failure would be
# logged and silently swallowed, the outer bundle would seal anyway,
# and notary would reject the build with a per-binary error days later.
# Find every Mach-O file under Contents/. The `file` filter is needed
# because PyInstaller also bundles non-Mach-O files (icons, plist, etc.)
# that codesign would refuse.
while IFS= read -r -d '' binary; do
    if file "$binary" | grep -q "Mach-O"; then
        echo "[sign-mac]   $binary"
        if ! codesign --force --options runtime \
                --entitlements "$ENTITLEMENTS" \
                --sign "$IDENTITY" \
                --timestamp \
                "$binary"; then
            echo "[sign-mac] codesign FAILED on $binary" >&2
            exit 1
        fi
    fi
done < <(find "$APP/Contents" -type f \( -perm -u+x -o -name "*.dylib" -o -name "*.so" \) -print0)

# Sign all framework bundles (Python.framework, etc.). Frameworks are
# directories ending in .framework — codesign treats them as a unit.
while IFS= read -r -d '' fw; do
    echo "[sign-mac]   framework: $fw"
    if ! codesign --force --options runtime \
            --entitlements "$ENTITLEMENTS" \
            --sign "$IDENTITY" \
            --timestamp \
            "$fw"; then
        echo "[sign-mac] codesign FAILED on framework $fw" >&2
        exit 1
    fi
done < <(find "$APP/Contents" -type d -name "*.framework" -print0)

echo "[sign-mac] Sealing outer bundle"
codesign --force --options runtime \
    --entitlements "$ENTITLEMENTS" \
    --sign "$IDENTITY" \
    --timestamp \
    "$APP"

echo "[sign-mac] Verifying signature"
codesign --verify --deep --strict --verbose=2 "$APP"

echo "[sign-mac] Confirming hardened runtime"
codesign_info=$(codesign -d --verbose=4 "$APP" 2>&1 || true)
if printf '%s\n' "$codesign_info" | grep -q "flags=0x10000(runtime)"; then
    echo "[sign-mac] Hardened runtime enabled"
else
    echo "[sign-mac] Hardened runtime NOT enabled on outer bundle" >&2
    printf '%s\n' "$codesign_info" | grep -i "flags=" >&2 || true
    exit 1
fi

echo "[sign-mac] Done"
