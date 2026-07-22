#!/usr/bin/env bash
# Build a signed macOS installer package (.pkg) from dist/BetterFlow.app.
#
# The DMG is the download for humans; the pkg exists because macOS MDM
# (Miradore -> InstallEnterpriseApplication) can only deploy a signed
# .pkg. See docs/superpowers/specs/2026-07-22-mdm-pkg-deployment-design.md.
#
#   pkgbuild     -> component package (the .app, installed to /Applications),
#                   built from a staging root + a component plist that sets
#                   BundleIsRelocatable=false (see the long note below —
#                   without it the install silently no-ops)
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

# --- Bundle relocation must be OFF -------------------------------------
#
# pkgbuild marks every bundle it packages BundleIsRelocatable=true by
# default. At install time PackageKit then SEARCHES the machine (Spotlight
# + the receipt database) for any bundle carrying the same identifier
# (co.betterqa.betterflow) and installs OVER THAT COPY, silently ignoring
# --install-location. On a Mac with a stray or old BetterFlow.app anywhere
# — a Downloads copy, a build artifact, an old install — the MDM push
# reports "Install Succeeded" and /Applications/BetterFlow.app never
# appears. Observed 2026-07-22: a real install "succeeded" into a
# dist/BetterFlow.app inside a git worktree.
#
# Relocation cannot be turned off on the pkgbuild command line; it is a
# property of the component, so we have to --analyze into a component
# plist, flip the flag, and hand that back with --component-plist. That
# in turn requires --root (a staging tree) rather than --component.
STAGING="$WORKDIR/root"
mkdir -p "$STAGING"
# ditto, not cp: it preserves the code signature's extended attributes.
ditto "$APP" "$STAGING/$(basename "$APP")"

COMPONENT_PLIST="$WORKDIR/component.plist"
echo "[build-pkg] pkgbuild --analyze — component property list"
pkgbuild --analyze --root "$STAGING" "$COMPONENT_PLIST"

python3 - "$COMPONENT_PLIST" <<'PY'
import plistlib, sys

path = sys.argv[1]
with open(path, "rb") as fh:
    components = plistlib.load(fh)
if not components:
    sys.exit("[build-pkg] pkgbuild --analyze produced an empty component list")
for component in components:
    component["BundleIsRelocatable"] = False
with open(path, "wb") as fh:
    plistlib.dump(components, fh)

# Read it back rather than trusting the write.
with open(path, "rb") as fh:
    written = plistlib.load(fh)
bad = [c for c in written if c.get("BundleIsRelocatable") is not False]
if bad:
    sys.exit("[build-pkg] BundleIsRelocatable is still set on: %s" % bad)
print("[build-pkg] BundleIsRelocatable=false on %d component(s)" % len(written))
PY

echo "[build-pkg] Component plist:"
plutil -p "$COMPONENT_PLIST"

echo "[build-pkg] pkgbuild — component package"
pkgbuild \
    --root "$STAGING" \
    --component-plist "$COMPONENT_PLIST" \
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

# Assert against the ARTIFACT, not against the plist we wrote. A relocatable
# component lists the bundle inside <relocate> in its PackageInfo:
#     <relocate><bundle id="co.betterqa.betterflow"/></relocate>
# A non-relocatable one leaves that element empty (<relocate/>). This is the
# check that would have caught the silent no-op install, so it runs on every
# build.
echo "[build-pkg] Verifying the package does not permit bundle relocation"
EXPANDED="$WORKDIR/expanded"
pkgutil --expand "$OUTPUT_PKG" "$EXPANDED"
python3 - "$EXPANDED" <<'PY'
import pathlib, sys
import xml.etree.ElementTree as ET

root = pathlib.Path(sys.argv[1])
package_infos = sorted(root.rglob("PackageInfo"))
if not package_infos:
    sys.exit("[build-pkg] FAIL: no PackageInfo found in the expanded package")

offenders = []
for package_info in package_infos:
    for relocate in ET.parse(package_info).getroot().iter("relocate"):
        for bundle in relocate.findall("bundle"):
            offenders.append("%s: %s" % (package_info.name, bundle.get("id")))

if offenders:
    sys.stderr.write(
        "[build-pkg] FAIL: the built package still declares bundles relocatable:\n"
        + "".join("  %s\n" % o for o in offenders)
        + "[build-pkg] PackageKit would install over an existing copy of that\n"
        "[build-pkg] identifier found anywhere on the target Mac and report\n"
        "[build-pkg] success without ever touching /Applications.\n"
    )
    sys.exit(1)

print(
    "[build-pkg] OK: %d PackageInfo file(s), no bundle marked relocatable"
    % len(package_infos)
)
PY

echo "[build-pkg] Created $OUTPUT_PKG (signed, NOT yet notarized)"
