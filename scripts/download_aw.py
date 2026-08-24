"""Download ActivityWatch binaries from GitHub releases and rename for white-labeling."""

import os
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The pin lives in src/aw_release.py and NOWHERE else. This script used to
# carry its own AW_VERSION and RELEASE_ASSETS, which nothing compared against
# the runtime copy — and this script is what build.yml uses to fetch the
# binaries we ship, so a drift meant the build downloading one version while
# the agent verified the digests of another, with the nightly pin guard green.
sys.path.insert(0, PROJECT_ROOT)
from src.machine_arch import common_arches  # noqa: E402
from src.aw_release import (  # noqa: E402
    AW_TO_BF_NAMES,
    AW_VERSION,
    RELEASE_ASSETS,
    RELEASE_BASE,
    asset_arch,
    asset_key,
    digest_mismatch,
    platform_key,
)

# Branded names (what we check for on disk)
BF_BINARIES = list(AW_TO_BF_NAMES.values())

# Output directory relative to project root
OUTPUT_BASE = os.path.join(PROJECT_ROOT, "resources", "trackers")


def get_platform() -> str:
    """The on-disk tracker directory key. Delegates to the one definition."""
    return platform_key()


def get_asset_key() -> str:
    """Which archive THIS BUILD needs.

    Honours `TARGET_ARCH`, which `.github/workflows/build.yml` already sets per
    matrix leg, because a cross-architecture build must fetch the trackers of
    the architecture it is BUILDING FOR, not of the runner executing it. The
    macOS x86_64 leg runs on `macos-14-large`; without this it would bundle
    whatever the runner happens to be and ship a DMG whose trackers do not match
    its own binaries.

    Falls back to this host's architecture for local builds, where the two are
    the same thing.
    """
    target = os.environ.get("TARGET_ARCH", "").strip()
    return asset_key(machine=target or None)


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


def arch_mismatch(output_dir: str, plat: str, key: str) -> bool:
    """True when the trackers on disk are not the architecture we are building.

    macOS only: it is the sole platform where we publish two architectures into
    one per-OS directory, so it is the only one where a leftover tree can be
    complete, correctly named, and still wrong. Returns False whenever the
    headers cannot be read, which re-downloads nothing on the strength of a
    failed probe.

    `asset_arch` decodes the key and `common_arches` decides what the tree is —
    the same two rules the agent applies at start time. Spelling either one out
    again here (a `key.endswith(...)`, a first-readable-wins loop) is a second
    implementation free to drift from the one that ships.
    """
    if plat != "darwin":
        return False
    wanted = asset_arch(key)
    if wanted is None:
        return False
    paths = [resolve_binary_path(output_dir, name, plat) for name in BF_BINARIES]
    arches = common_arches([p for p in paths if p])
    if arches is None or wanted in arches:
        return False
    have = "/".join(sorted(arches)) or "a mix with no common architecture"
    print(f"  trackers on disk are {have}, need {wanted} — re-downloading")
    return True


def build_ssl_context() -> ssl.SSLContext:
    """Build a TLS context with a CA bundle that actually exists.

    Several Pythons on macOS (notably the python.org framework builds) ship
    with `openssl_cafile` pointing at an `etc/openssl/cert.pem` that was never
    installed, so *every* HTTPS request fails with
    CERTIFICATE_VERIFY_FAILED / "unable to get local issuer certificate".
    The error names TLS and says nothing about the missing bundle, which is
    why this used to be worked around by exporting SSL_CERT_FILE by hand.

    Resolution order:
      1. SSL_CERT_FILE / REQUESTS_CA_BUNDLE, if the caller set one that exists.
      2. certifi's bundle, if certifi is importable.
      3. The interpreter's own default (may or may not work).
    """
    for env_var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = os.environ.get(env_var)
        if path and os.path.isfile(path):
            print(f"  TLS: using {env_var}={path}")
            return ssl.create_default_context(cafile=path)

    try:
        import certifi

        cafile = certifi.where()
        if os.path.isfile(cafile):
            print(f"  TLS: using certifi bundle {cafile}")
            return ssl.create_default_context(cafile=cafile)
        print(f"  TLS: certifi reported a missing bundle at {cafile}")
    except ImportError:
        print("  TLS: certifi not installed, falling back to system defaults")

    return ssl.create_default_context()


def verify_digest(zip_path: str, key: str) -> None:
    """Fail CLOSED if the archive does not match its pinned SHA-256.

    The runtime download path in src/aw_manager.py has always done this; this
    script — which fetches the binaries we actually SHIP — did not, so the
    build was the unverified half of a pair whose whole point is that upstream
    release assets are mutable under a pinned tag.

    The rule itself lives with the pins (`aw_release.digest_mismatch`) and is
    called by both halves. Single-sourcing the pin while hand-writing its
    enforcement twice is the same defect one layer up: a future change to the
    rule would have to land in both copies or the build and the agent would
    accept different archives.
    """
    problem = digest_mismatch(zip_path, key)
    if problem:
        print(f"ERROR: {problem}")
        sys.exit(1)
    print("  digest OK")


def download_release(key: str) -> str:
    """Download release zip to a temp file. Returns path to zip."""
    asset = RELEASE_ASSETS[key]
    url = f"{RELEASE_BASE}/{asset}"
    print(f"Downloading {url} ...")

    context = build_ssl_context()
    tmp = tempfile.mktemp(suffix=".zip")
    try:
        with urllib.request.urlopen(url, context=context) as response, open(
            tmp, "wb"
        ) as out:
            shutil.copyfileobj(response, out)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            print()
            print("ERROR: TLS certificate verification failed.")
            print(f"  interpreter: {sys.executable}")
            print(f"  default CA file: {ssl.get_default_verify_paths().openssl_cafile}")
            print()
            print("  This is a missing CA bundle on this machine, not a network or")
            print("  GitHub problem. Fix it with ONE of:")
            print(f"    {sys.executable} -m pip install --upgrade certifi")
            print("    /Applications/Python 3.x/Install Certificates.command")
            print("    SSL_CERT_FILE=/path/to/cacert.pem make download-aw")
            print()
            print(f"  Underlying error: {exc.reason}")
            sys.exit(1)
        raise

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
    """Download tracker binaries for the platform/architecture being built."""
    plat = get_platform()
    key = get_asset_key()
    output_dir = get_output_dir(plat)

    print(f"BetterFlow Tracker Components {AW_VERSION} — platform: {plat} ({key})")
    print(f"Output: {output_dir}")
    print()

    # "Already present" must also mean "present for the RIGHT architecture".
    # The output directory is per-OS, so a tree left by a previous build of the
    # other macOS architecture satisfies a name-only check and would be bundled
    # unchanged — shipping x86_64 trackers inside an arm64 DMG, which is the
    # #216 bug reintroduced at build time.
    if binaries_exist(output_dir, plat) and not arch_mismatch(output_dir, plat, key):
        print("All binaries already present, skipping download.")
        return

    zip_path = download_release(key)

    try:
        print("Verifying archive digest...")
        verify_digest(zip_path, key)
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
