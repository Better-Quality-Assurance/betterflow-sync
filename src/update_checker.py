"""Check for new releases via GitHub Releases API."""

import logging
import platform
import threading
from typing import Callable, Optional

import requests

try:
    from .machine_arch import true_machine_arch
    from .sync.http_client import resolve_ca_bundle
    from .url_safety import assert_safe_final_url
except ImportError:  # PyInstaller bundle (src/ is import root)
    from machine_arch import true_machine_arch
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
            if pattern in name and arch in name and name.endswith(".dmg") and url:
                return url
        # Fallback: any macOS DMG
        for asset in assets:
            name = asset.get("name", "")
            url = asset.get("browser_download_url")
            if pattern in name and name.endswith(".dmg") and url:
                logger.debug(f"No arch-specific DMG for {arch}, using generic DMG")
                return url

    # Windows primary, macOS ZIP as last-resort fallback for non-standard releases
    for asset in assets:
        name = asset.get("name", "")
        url = asset.get("browser_download_url")
        if pattern in name and name.endswith(".zip") and url:
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
