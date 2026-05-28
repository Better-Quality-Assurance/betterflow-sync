"""Download ActivityWatch binaries from GitHub releases and rename for white-labeling."""

import hashlib
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

# Expected SHA-256 of each release asset, pinned per AW version. These are the
# canonical GitHub release artifacts for AW_VERSION (immutable once published).
# A download whose hash does not match one of these is rejected — this is the
# supply-chain integrity check for the bundled tracker binaries.
#
# To bump AW_VERSION: download the three zips from the release, run
# `shasum -a 256 <file>` on each, and replace the digests below.
EXPECTED_SHA256 = {
    "v0.13.2": {
        "activitywatch-v0.13.2-macos-x86_64.zip": "e62a76c0ec3c0e69d58ba207bb8da6d8d47d0c7ad1bc871ddf702168f291cf5b",
        "activitywatch-v0.13.2-windows-x86_64.zip": "a067fa765678a411991826c4da811fd2d8ca260c2db9d6d897957565b61c369f",
        "activitywatch-v0.13.2-linux-x86_64.zip": "8f62b10babf8a8f108cbdf7267c02fbc1ce2a970fa9535f230b3416b803e3360",
    },
}

# Original AW binary names (what's in the zip) -> branded names
AW_TO_BF_NAMES = {
    "aw-server-rust": "bf-data-service",
    "aw-watcher-window": "bf-window-tracker",
    "aw-watcher-afk": "bf-idle-tracker",
}

# Branded names (what we check for on disk)
BF_BINARIES = list(AW_TO_BF_NAMES.values())

# GitHub release URLs
RELEASE_BASE = f"https://github.com/ActivityWatch/activitywatch/releases/download/{AW_VERSION}"
RELEASE_ASSETS = {
    "darwin": f"activitywatch-{AW_VERSION}-macos-x86_64.zip",
    "windows": f"activitywatch-{AW_VERSION}-windows-x86_64.zip",
    "linux": f"activitywatch-{AW_VERSION}-linux-x86_64.zip",
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
    elif system == "Linux":
        return "linux"
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


def _sha256_file(path: str) -> str:
    """Compute the SHA-256 hex digest of a file, streaming to bound memory."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def expected_digest(asset: str) -> str:
    """Look up the pinned SHA-256 for an asset, or abort if none is recorded."""
    version_map = EXPECTED_SHA256.get(AW_VERSION)
    if not version_map or asset not in version_map:
        print(
            f"ERROR: No pinned SHA-256 for {asset} (version {AW_VERSION}). "
            "Refusing to download an unverified binary. Update EXPECTED_SHA256."
        )
        sys.exit(1)
    return version_map[asset].lower()


def download_release(plat: str) -> str:
    """Download release zip to a temp file and verify its SHA-256. Returns path to zip."""
    asset = RELEASE_ASSETS[plat]
    expected = expected_digest(asset)
    url = f"{RELEASE_BASE}/{asset}"
    print(f"Downloading {url} ...")

    # mkstemp() atomically creates the file with O_EXCL (no mktemp race / symlink
    # attack window). We own the fd; close it before urlretrieve writes the path.
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        urllib.request.urlretrieve(url, tmp)

        actual = _sha256_file(tmp)
        if actual != expected:
            print(
                "ERROR: SHA-256 mismatch — refusing to use this download.\n"
                f"  asset:    {asset}\n"
                f"  expected: {expected}\n"
                f"  actual:   {actual}"
            )
            sys.exit(1)

        size_mb = os.path.getsize(tmp) / (1024 * 1024)
        print(f"Downloaded {size_mb:.1f} MB — SHA-256 verified")
        return tmp
    except BaseException:
        # On any failure (verification, download, interrupt) don't leave the
        # temp file behind.
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


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

        missing = [name for name in AW_TO_BF_NAMES if name not in launchers]
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

                rel_name = (
                    member.filename[len(prefix) :] if prefix else os.path.basename(member.filename)
                )
                if os.path.basename(member.filename) == os.path.basename(launcher_path):
                    rel_name = branded_name + ext

                target = os.path.join(target_root, rel_name)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)


def fix_permissions(output_dir: str, plat: str) -> None:
    """Make launchers executable on POSIX; strip quarantine xattr on macOS."""
    if plat not in ("darwin", "linux"):
        return

    for root, _, files in os.walk(output_dir):
        for file_name in files:
            path = os.path.join(root, file_name)
            if os.path.basename(path).startswith("bf-"):
                st = os.stat(path)
                os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
                print(f"  Fixed permissions: {path}")
            if plat == "darwin":
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
