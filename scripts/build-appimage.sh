#!/usr/bin/env bash
#
# Package the PyInstaller one-dir Linux build (dist/BetterFlow) into a single
# portable .AppImage.
#
# Prereq: `pyinstaller build.spec --clean` has been run on Linux first, leaving
# dist/BetterFlow/BetterFlow (the launcher) + dist/BetterFlow/_internal/...
#
# Usage: scripts/build-appimage.sh [output.AppImage]
#   Default output: dist/BetterFlow-linux-x86_64.AppImage
#   (the "BetterFlow-linux" stem is what the in-app updater matches against —
#    see src/update_checker.py::_ASSET_PATTERNS)

set -euo pipefail

ARCH="${ARCH:-x86_64}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="${ROOT}/dist"
DIST_APP="${DIST}/BetterFlow"
APPDIR="${DIST}/BetterFlow.AppDir"
OUTPUT="${1:-${DIST}/BetterFlow-linux-${ARCH}.AppImage}"
ICON_SRC="${ROOT}/resources/icon.png"

if [ ! -x "${DIST_APP}/BetterFlow" ]; then
    echo "ERROR: ${DIST_APP}/BetterFlow not found. Run 'pyinstaller build.spec --clean' first." >&2
    exit 1
fi

echo "[appimage] Assembling AppDir at ${APPDIR}"
rm -rf "${APPDIR}"
mkdir -p "${APPDIR}/usr/bin" \
         "${APPDIR}/usr/share/applications" \
         "${APPDIR}/usr/share/icons/hicolor/256x256/apps"

# The PyInstaller one-dir payload (launcher + _internal/) lives under usr/bin.
cp -a "${DIST_APP}/." "${APPDIR}/usr/bin/"

# Icon — required at the AppDir root with a basename matching Icon= below.
if [ -f "${ICON_SRC}" ]; then
    cp "${ICON_SRC}" "${APPDIR}/betterflow.png"
    cp "${ICON_SRC}" "${APPDIR}/usr/share/icons/hicolor/256x256/apps/betterflow.png"
else
    echo "[appimage] WARNING: ${ICON_SRC} missing — building without an icon"
fi

# Desktop entry — required at the AppDir root (and mirrored under usr/share).
cat > "${APPDIR}/betterflow.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=BetterFlow
Comment=Sync ActivityWatch data to BetterFlow for automatic time tracking
Exec=BetterFlow
Icon=betterflow
Categories=Utility;Office;
Terminal=false
DESKTOP
cp "${APPDIR}/betterflow.desktop" "${APPDIR}/usr/share/applications/betterflow.desktop"

# AppRun — entrypoint appimagetool wires up; execs our launcher.
cat > "${APPDIR}/AppRun" <<'APPRUN'
#!/bin/sh
HERE="$(dirname "$(readlink -f "${0}")")"
exec "${HERE}/usr/bin/BetterFlow" "$@"
APPRUN
chmod +x "${APPDIR}/AppRun"

# Fetch appimagetool if it isn't already on PATH.
if command -v appimagetool >/dev/null 2>&1; then
    APPIMAGETOOL="$(command -v appimagetool)"
else
    APPIMAGETOOL="${DIST}/appimagetool-${ARCH}.AppImage"
    if [ ! -f "${APPIMAGETOOL}" ]; then
        echo "[appimage] Downloading appimagetool"
        curl -fsSL -o "${APPIMAGETOOL}" \
            "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
        chmod +x "${APPIMAGETOOL}"
    fi
fi

echo "[appimage] Building ${OUTPUT}"
# Extract-and-run avoids needing FUSE (unavailable on most CI runners).
export APPIMAGE_EXTRACT_AND_RUN=1
ARCH="${ARCH}" "${APPIMAGETOOL}" "${APPDIR}" "${OUTPUT}"

echo "[appimage] Done: ${OUTPUT}"
