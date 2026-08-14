"""Check for new releases via GitHub Releases API."""

import logging
import platform
import threading
from typing import Callable, Optional

import requests

try:
    from .machine_arch import ARM64, X86_64, true_machine_arch
    from .sync.http_client import resolve_ca_bundle
    from .url_safety import assert_safe_final_url
except ImportError:  # PyInstaller bundle (src/ is import root)
    from machine_arch import ARM64, X86_64, true_machine_arch
    from sync.http_client import resolve_ca_bundle
    from url_safety import assert_safe_final_url

logger = logging.getLogger(__name__)

GITHUB_REPO = "Better-Quality-Assurance/betterflow-sync"
RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases"

# Valid update channels in order of stability
UPDATE_CHANNELS = ("stable", "beta", "canary")


def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse 'v1.2.3' or '1.2.3-beta.1' into a comparable tuple."""
    return tuple(int(x) for x in version.lstrip("v").split("-")[0].split(".")[:3])


def _matches_channel(release: dict, channel: str) -> bool:
    """Check if a GitHub release matches the requested update channel.

    - stable: only non-prerelease, non-draft releases
    - beta: prereleases with 'beta' or 'rc' in the tag, plus stable
    - canary: any non-draft release (prereleases + stable)
    """
    if release.get("draft", False):
        return False
    is_prerelease = release.get("prerelease", False)
    tag = release.get("tag_name", "").lower()

    if channel == "stable":
        return not is_prerelease
    elif channel == "beta":
        if not is_prerelease:
            return True
        return "beta" in tag or "rc" in tag
    elif channel == "canary":
        return True
    return not is_prerelease


_ASSET_PATTERNS = {
    "Darwin": "BetterFlow-macOS",
    "Windows": "BetterFlow-Windows",
    "Linux": "BetterFlow-linux",
}

# The arch token that must NOT appear in a macOS asset we offer this machine.
#
# Two different reasons, depending on which Mac is asking, and they are worth
# keeping apart because only one of them is a compatibility fact:
#
#   * NATIVE arm64 (proc_translated == "0"): Rosetta 2 may not be installed, and
#     without it an x86_64 binary dies EBADARCH/ENOEXEC and capture stops dead —
#     the production fault on record for Ardiel Plata's device (internal-tool2
#     #2298, an Apple Silicon machine with no Rosetta).
#   * TRANSLATED (proc_translated == "1"): Rosetta demonstrably IS installed,
#     because we are running under it right now, so the Intel DMG would execute.
#     We refuse it anyway — offering it re-pins the machine to Rosetta for
#     another release cycle, which is precisely the self-perpetuating loop #185
#     exists to break. That is a product choice, not an inability.
#
# In the Intel direction there is no reverse Rosetta, so an arm64 build on a
# genuine Intel Mac has no recovery path under any circumstances.
#
# Refusal is not a dead end for the user: update_handler still raises the
# "Version X is available" notice with the release page, so a manual download
# remains one click away while the release is repaired.
_INCOMPATIBLE_ARCH = {ARM64: X86_64, X86_64: ARM64}


def _is_wrong_arch(name: str, arch: str, system: str) -> bool:
    """True when this asset names an architecture this machine cannot run.

    Scoped to macOS. The map's keys ("arm64", "x86_64") collide with a Linux
    host's own ``platform.machine()``, and the unknown-arch branch below is
    platform-blind by construction — so without this gate a Windows or Linux
    host that could not determine its architecture would silently refuse assets
    it has always accepted. Linux happens to return before ever reaching here,
    but that is the caller's control flow rather than a property of this rule.

    Unknown ``arch`` (``platform.machine()`` is documented to return "" when it
    cannot tell) rejects EVERY arch-suffixed asset rather than guessing: one of
    them is wrong and we cannot tell which, so the only safe answer is neither.
    """
    if system != "Darwin":
        return False

    if not arch:
        return any(token in name for token in (ARM64, X86_64))

    other = _INCOMPATIBLE_ARCH.get(arch)

    return other is not None and other in name


def _find_platform_asset(
    release: dict,
    system: Optional[str] = None,
    arch: Optional[str] = None,
) -> Optional[str]:
    """Find the platform-specific download URL from a GitHub release.

    On macOS: prefers architecture-specific DMG (arm64/x86_64), falls back to ZIP.
    On Windows: matches ZIP assets.
    On Linux: matches the .AppImage asset.

    Args:
        release: GitHub release dict with "assets" list.
        system: Override platform.system() for testing (e.g. "Darwin", "Windows").
        arch: Override the detected architecture for testing (e.g. "arm64", "x86_64").
    """
    system = system or platform.system()
    # NOT platform.machine(): that reports the running PROCESS's architecture,
    # so an Intel build under Rosetta 2 on Apple Silicon answers "x86_64" and
    # re-selects the Intel DMG on every update — the wrong build could never
    # correct itself. true_machine_arch() reports the HARDWARE. See machine_arch.
    arch = arch or true_machine_arch(system=system)
    pattern = _ASSET_PATTERNS.get(system)
    if not pattern:
        return None

    assets = release.get("assets", [])
    if system == "Linux":
        for asset in assets:
            name = asset.get("name", "")
            url = asset.get("browser_download_url")
            if pattern in name and name.endswith(".AppImage") and url:
                return url
        return None

    if system == "Darwin":
        # Prefer arch-specific DMG
        for asset in assets:
            name = asset.get("name", "")
            url = asset.get("browser_download_url")
            if pattern in name and arch and arch in name and name.endswith(".dmg") and url:
                return url
        # Fallback: any macOS DMG that is not a build we know this Mac cannot
        # run. An unsuffixed (possibly universal) DMG still qualifies; the
        # wrong-arch one does not, and no update beats a dead install.
        for asset in assets:
            name = asset.get("name", "")
            url = asset.get("browser_download_url")
            if pattern in name and name.endswith(".dmg") and url:
                if _is_wrong_arch(name, arch, system):
                    logger.warning(
                        f"Skipping {name}: wrong architecture for this machine "
                        f"({arch or 'undetermined'})"
                    )
                    continue
                logger.debug(f"No arch-specific DMG for {arch}, using generic DMG")
                return url

    # Windows primary, macOS ZIP as last-resort fallback for non-standard releases
    for asset in assets:
        name = asset.get("name", "")
        url = asset.get("browser_download_url")
        if pattern in name and name.endswith(".zip") and url:
            # Same rule as the DMG fallback: this loop is reached for macOS too,
            # so a BetterFlow-macOS-x86_64.zip is the identical hazard one door
            # along. _is_wrong_arch returns False outright off Darwin, so Windows
            # keeps its existing behaviour whatever its assets are named.
            if _is_wrong_arch(name, arch, system):
                logger.warning(
                    f"Skipping {name}: wrong architecture for this machine "
                    f"({arch or 'undetermined'})"
                )
                continue
            return url
    return None


def check_for_update(
    current_version: str,
    channel: str = "stable",
    callback: Optional[Callable[[str, str, Optional[str]], None]] = None,
) -> None:
    """Check GitHub for a newer release (runs in background thread).

    Args:
        current_version: Current app version (e.g. '1.0.0')
        channel: Update channel - 'stable', 'beta', or 'canary'
        callback: Optional fn(latest_version, download_url) called if update available
    """

    def _check():
        try:
            effective_channel = channel
            if effective_channel not in UPDATE_CHANNELS:
                logger.warning(f"Unknown update channel '{effective_channel}', defaulting to 'stable'")
                effective_channel = "stable"

            if effective_channel == "stable":
                # Fast path: use /releases/latest for stable channel
                resp = requests.get(
                    f"{RELEASES_URL}/latest",
                    headers={"Accept": "application/vnd.github+json"},
                    timeout=10,
                    verify=resolve_ca_bundle(),
                )
                # requests follows redirects transparently; this response is what
                # produces the download_url the updater's allowlist later guards,
                # so re-check the host we actually landed on before trusting it.
                assert_safe_final_url(resp.url, "Update check")
                if resp.status_code != 200:
                    logger.debug(f"Update check: GitHub API returned {resp.status_code}")
                    return
                try:
                    releases = [resp.json()]
                except (ValueError, RuntimeError):
                    logger.debug("Update check: failed to parse GitHub API response")
                    return
            else:
                # Fetch recent releases and filter by channel
                resp = requests.get(
                    RELEASES_URL,
                    headers={"Accept": "application/vnd.github+json"},
                    params={"per_page": 20},
                    timeout=10,
                    verify=resolve_ca_bundle(),
                )
                assert_safe_final_url(resp.url, "Update check")
                if resp.status_code != 200:
                    logger.debug(f"Update check: GitHub API returned {resp.status_code}")
                    return
                try:
                    releases = resp.json()
                except (ValueError, RuntimeError):
                    logger.debug("Update check: failed to parse GitHub API response")
                    return
                if not isinstance(releases, list):
                    return

            # Find the newest release matching the channel
            best = None
            best_tuple = None
            for rel in releases:
                if not _matches_channel(rel, effective_channel):
                    continue
                tag = rel.get("tag_name", "")
                if not tag:
                    continue
                try:
                    vt = _version_tuple(tag)
                except (ValueError, TypeError):
                    continue
                if best_tuple is None or vt > best_tuple:
                    best = rel
                    best_tuple = vt

            if best is None:
                return

            try:
                if best_tuple <= _version_tuple(current_version):
                    return
            except (ValueError, TypeError):
                return

            latest_tag = best["tag_name"]
            html_url = best.get("html_url", "")

            # Find platform-specific asset URL for self-update (DMG or ZIP)
            asset_url = _find_platform_asset(best)

            logger.info(
                f"Update available ({effective_channel}): {current_version} -> {latest_tag} | {html_url}"
            )

            if callback:
                callback(latest_tag.lstrip("v"), html_url, asset_url)

        except ValueError as e:
            # The only ValueError that reaches here is the off-allowlist redirect
            # guard (json/version parsing is caught locally), so log it loudly
            # rather than burying a hijacked update check at debug level.
            logger.warning(f"Update check aborted: {e}")
        except Exception as e:
            logger.debug(f"Update check failed: {e}")

    thread = threading.Thread(target=_check, name="update-checker", daemon=True)
    thread.start()
