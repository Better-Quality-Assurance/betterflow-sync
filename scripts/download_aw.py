"""Download ActivityWatch binaries from GitHub releases and rename for white-labeling."""

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

# Original AW binary names (what's in the zip) -> branded names
AW_TO_BF_NAMES = {
    "aw-server-rust": "bf-data-service",
    "aw-watcher-window": "bf-window-tracker",
    "aw-watcher-afk": "bf-idle-tracker",
}

# Branded names (what we check for on disk)
BF_BINARIES = list(AW_TO_BF_NAMES.values())

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
OUTPUT_BASE = os.path.join(PROJECT_ROOT, "resources", "trackers")


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
    """Check if all required binaries already exist (flat or bundled layout)."""
    for name in BF_BINARIES:
        if resolve_binary_path(output_dir, name, plat) is None:
            return False
    return True


def resolve_binary_path(output_dir: str, name: str, plat: str) -> str | None:
    """Resolve component launcher path (flat or bundled)."""
    ext = ".exe" if plat == "windows" else ""

    flat = os.path.join(output_dir, name + ext)
    if os.path.isfile(flat):
        if plat == "darwin" and name in {"bf-window-tracker", "bf-idle-tracker"}:
            if os.path.exists(os.path.join(output_dir, "Python")):
                return flat
            return None
        return flat

    bundled = os.path.join(output_dir, name, name + ext)
    if os.path.isfile(bundled):
        return bundled

    return None


def download_release(plat: str) -> str:
    """Download release zip to a temp file. Returns path to zip."""
    asset = RELEASE_ASSETS[plat]
    url = f"{RELEASE_BASE}/{asset}"
    print(f"Downloading {url} ...")

    tmp = tempfile.mktemp(suffix=".zip")
    urllib.request.urlretrieve(url, tmp)

    size_mb = os.path.getsize(tmp) / (1024 * 1024)
    print(f"Downloaded {size_mb:.1f} MB")
    return tmp


def extract_binaries(zip_path: str, output_dir: str, plat: str) -> None:
    """Extract component runtime directories and rename launchers to branded names."""
    ext = ".exe" if plat == "windows" else ""

    os.makedirs(output_dir, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        launchers: dict[str, str] = {}
        for info in zf.infolist():
            basename = os.path.basename(info.filename)
            original_stem = basename.replace(ext, "") if ext else basename
            if original_stem in AW_TO_BF_NAMES and not info.is_dir():
                launchers[original_stem] = info.filename

        missing = [name for name in AW_TO_BF_NAMES.keys() if name not in launchers]
        if missing:
            print(f"WARNING: Missing binaries in archive: {missing}")
            return

        for original_name, launcher_path in launchers.items():
            branded_name = AW_TO_BF_NAMES[original_name]
            component_root = os.path.dirname(launcher_path)
            prefix = (component_root + "/") if component_root else ""
            target_root = os.path.join(output_dir, branded_name)

            if os.path.isdir(target_root):
                shutil.rmtree(target_root)
            os.makedirs(target_root, exist_ok=True)

            print(f"  Extracting runtime {original_name} -> {branded_name}/")
            for member in zf.infolist():
                if member.is_dir():
                    continue
                if prefix and not member.filename.startswith(prefix):
                    continue
                if not prefix and member.filename != launcher_path:
                    continue

                rel_name = member.filename[len(prefix):] if prefix else os.path.basename(member.filename)
                if os.path.basename(member.filename) == os.path.basename(launcher_path):
                    rel_name = branded_name + ext

                target = os.path.join(target_root, rel_name)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)


def fix_permissions(output_dir: str, plat: str) -> None:
    """Make binaries executable on macOS and strip quarantine xattr."""
    if plat != "darwin":
        return

    for root, _, files in os.walk(output_dir):
        for file_name in files:
            path = os.path.join(root, file_name)
            if os.path.basename(path).startswith("bf-"):
                st = os.stat(path)
                os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
                print(f"  Fixed permissions: {path}")
            subprocess.run(
                ["xattr", "-d", "com.apple.quarantine", path],
                capture_output=True,
            )


def main() -> None:
    """Download tracker binaries for the current platform."""
    plat = get_platform()
    output_dir = get_output_dir(plat)

    print(f"BetterFlow Tracker Components {AW_VERSION} — platform: {plat}")
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
