"""Download ActivityWatch binaries from GitHub releases."""

import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

AW_VERSION = "v0.13.2"

# Binaries we need (headless components only, no aw-qt)
AW_BINARIES = [
    "aw-server-rust",
    "aw-watcher-window",
    "aw-watcher-afk",
]

# GitHub release URLs
RELEASE_BASE = (
    f"https://github.com/ActivityWatch/activitywatch/releases/download/{AW_VERSION}"
)
RELEASE_ASSETS = {
    "darwin": f"activitywatch-{AW_VERSION}-macos-x86_64.zip",
    "windows": f"activitywatch-{AW_VERSION}-windows.zip",
}

# Output directory relative to project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_BASE = os.path.join(PROJECT_ROOT, "resources", "activitywatch")


def get_platform() -> str:
    """Get current platform key."""
    system = platform.system()
    if system == "Darwin":
        return "darwin"
    elif system == "Windows":
        return "windows"
    else:
        print(f"Unsupported platform: {system}")
        sys.exit(1)


def get_output_dir(plat: str) -> str:
    """Get output directory for platform binaries."""
    return os.path.join(OUTPUT_BASE, plat)


def binaries_exist(output_dir: str, plat: str) -> bool:
    """Check if all required binaries already exist."""
    ext = ".exe" if plat == "windows" else ""
    for name in AW_BINARIES:
        path = os.path.join(output_dir, name + ext)
        if not os.path.exists(path):
            return False
    return True


def download_release(plat: str) -> str:
    """Download AW release zip to a temp file. Returns path to zip."""
    asset = RELEASE_ASSETS[plat]
    url = f"{RELEASE_BASE}/{asset}"
    print(f"Downloading {url} ...")

    tmp = tempfile.mktemp(suffix=".zip")
    urllib.request.urlretrieve(url, tmp)

    size_mb = os.path.getsize(tmp) / (1024 * 1024)
    print(f"Downloaded {size_mb:.1f} MB")
    return tmp


def extract_binaries(zip_path: str, output_dir: str, plat: str) -> None:
    """Extract only the needed binaries from the release zip."""
    ext = ".exe" if plat == "windows" else ""
    needed = {name + ext for name in AW_BINARIES}

    os.makedirs(output_dir, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            basename = os.path.basename(info.filename)
            if basename in needed:
                print(f"  Extracting {basename}")
                # Extract to flat directory (no nested folders)
                target = os.path.join(output_dir, basename)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                needed.discard(basename)

    if needed:
        print(f"WARNING: Missing binaries in archive: {needed}")


def fix_permissions(output_dir: str, plat: str) -> None:
    """Make binaries executable on macOS and strip quarantine xattr."""
    if plat != "darwin":
        return

    for name in AW_BINARIES:
        path = os.path.join(output_dir, name)
        if os.path.exists(path):
            # Make executable
            st = os.stat(path)
            os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

            # Strip quarantine attribute (prevents Gatekeeper blocks)
            subprocess.run(
                ["xattr", "-d", "com.apple.quarantine", path],
                capture_output=True,
            )
            print(f"  Fixed permissions: {name}")


def main() -> None:
    """Download AW binaries for the current platform."""
    plat = get_platform()
    output_dir = get_output_dir(plat)

    print(f"ActivityWatch {AW_VERSION} — platform: {plat}")
    print(f"Output: {output_dir}")
    print()

    if binaries_exist(output_dir, plat):
        print("All binaries already present, skipping download.")
        return

    zip_path = download_release(plat)

    try:
        print("Extracting binaries...")
        extract_binaries(zip_path, output_dir, plat)
        fix_permissions(output_dir, plat)
    finally:
        os.unlink(zip_path)

    # Verify
    if binaries_exist(output_dir, plat):
        print()
        print("Done! All binaries downloaded successfully.")
    else:
        print()
        print("ERROR: Some binaries are missing after extraction.")
        sys.exit(1)


if __name__ == "__main__":
    main()
