#!/usr/bin/env bash
# Build a signed macOS installer package (.pkg) from dist/BetterFlow.app.
#
# The DMG is the download for humans; the pkg exists because macOS MDM
# (Miradore -> InstallEnterpriseApplication) can only deploy a signed
# .pkg. See docs/superpowers/specs/2026-07-22-mdm-pkg-deployment-design.md.
#
#   pkgbuild     -> component package (the .app, installed to /Applications)
#   productbuild -> distribution package (what MDM and Installer.app accept)
#   productsign  -> signs it with the Developer ID INSTALLER identity
#
# The installer identity is a DIFFERENT certificate type from the
# Developer ID Application identity used by scripts/sign-mac.sh. Both
# live under Team 87NVC57J44. Identity is hardcoded for the same reason
# it is in sign-mac.sh: there is exactly one valid one.
#
# This script does NOT notarize. The pkg needs its own notarization pass
# (notarizing the .app does not cover the package containing it) — the
# `pkg` make target runs scripts/notarize-mac.py and staples afterwards.
#
# Usage:
#   ./scripts/build-pkg.sh [APP] [OUTPUT_PKG]
# Defaults:
#   APP=dist/BetterFlow.app  OUTPUT_PKG=dist/BetterFlow-macOS-<arch>.pkg

set -euo pipefail

IDENTITY="Developer ID Installer: Better Quality Assurance SRL (87NVC57J44)"
PKG_IDENTIFIER="co.betterqa.betterflow"
DISTRIBUTION_TEMPLATE="installer/macos/distribution.xml"

APP="${1:-dist/BetterFlow.app}"
DEFAULT_ARCH="$(uname -m | sed 's/aarch64/arm64/')"
OUTPUT_PKG="${2:-dist/BetterFlow-macOS-${TARGET_ARCH:-$DEFAULT_ARCH}.pkg}"

if [ ! -d "$APP" ]; then
    echo "[build-pkg] $APP not found — run make build-mac first" >&2
    exit 1
fi

if [ ! -f "$DISTRIBUTION_TEMPLATE" ]; then
    echo "[build-pkg] Distribution template not found: $DISTRIBUTION_TEMPLATE" >&2
    echo "[build-pkg] Run this script from the repo root, not from scripts/" >&2
    exit 1
fi

# `security find-identity -p codesigning` does NOT list installer certs —
# they carry a different policy. Query the default (all) policy instead.
if ! security find-identity -v | grep -q "Developer ID Installer: Better Quality Assurance SRL (87NVC57J44)"; then
    echo "[build-pkg] Developer ID Installer cert for Team 87NVC57J44 not in Keychain" >&2
    echo "[build-pkg] It is a separate certificate from the Developer ID Application" >&2
    echo "[build-pkg] one used by sign-mac.sh, and is created by the Apple Developer" >&2
    echo "[build-pkg] account holder. See docs/SIGNING.md." >&2
    exit 1
fi

# Wrapping an unsigned or broken .app produces a pkg that signs fine and
# then fails notarization ~10 minutes later. Fail here instead.
if ! codesign --verify --strict "$APP" 2>/dev/null; then
    echo "[build-pkg] $APP is not validly signed — run ./scripts/sign-mac.sh first" >&2
    exit 1
fi

# src/__init__.py is the single source of truth for the version (build.spec
# reads it the same way).
VERSION="$(python3 -c "
import re, pathlib, sys
m = re.search(r'__version__\s*=\s*[\"\']([^\"\']+)[\"\']', pathlib.Path('src/__init__.py').read_text())
if not m:
    sys.exit('could not parse __version__ from src/__init__.py')
print(m.group(1))
")"

echo "[build-pkg] App:        $APP"
echo "[build-pkg] Version:    $VERSION"
echo "[build-pkg] Identifier: $PKG_IDENTIFIER"
echo "[build-pkg] Output:     $OUTPUT_PKG"

WORKDIR="$(mktemp -d -t betterflow-pkg)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

COMPONENT_PKG_NAME="BetterFlow-component.pkg"

echo "[build-pkg] pkgbuild — component package"
pkgbuild \
    --component "$APP" \
    --install-location /Applications \
    --identifier "$PKG_IDENTIFIER" \
    --version "$VERSION" \
    "$WORKDIR/$COMPONENT_PKG_NAME"

echo "[build-pkg] productbuild — distribution package"
sed -e "s|@IDENTIFIER@|$PKG_IDENTIFIER|g" \
    -e "s|@VERSION@|$VERSION|g" \
    -e "s|@COMPONENT_PKG@|$COMPONENT_PKG_NAME|g" \
    "$DISTRIBUTION_TEMPLATE" > "$WORKDIR/distribution.xml"

productbuild \
    --distribution "$WORKDIR/distribution.xml" \
    --package-path "$WORKDIR" \
    "$WORKDIR/BetterFlow-unsigned.pkg"

echo "[build-pkg] productsign — Developer ID Installer"
mkdir -p "$(dirname "$OUTPUT_PKG")"
rm -f "$OUTPUT_PKG"
productsign --sign "$IDENTITY" "$WORKDIR/BetterFlow-unsigned.pkg" "$OUTPUT_PKG"

echo "[build-pkg] Verifying package signature"
pkgutil --check-signature "$OUTPUT_PKG"

echo "[build-pkg] Created $OUTPUT_PKG (signed, NOT yet notarized)"
