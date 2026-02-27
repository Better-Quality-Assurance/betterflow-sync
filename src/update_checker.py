"""Check for new releases via GitHub Releases API."""

import logging
import threading
from typing import Optional

import requests

logger = logging.getLogger(__name__)

GITHUB_REPO = "betterqa/betterflow-sync"
RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases"

# Valid update channels in order of stability
UPDATE_CHANNELS = ("stable", "beta", "canary")


def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse 'v1.2.3' or '1.2.3' into a comparable tuple."""
    return tuple(int(x) for x in version.lstrip("v").split(".")[:3])


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


def check_for_update(
    current_version: str,
    channel: str = "stable",
    callback: Optional[callable] = None,
) -> None:
    """Check GitHub for a newer release (runs in background thread).

    Args:
        current_version: Current app version (e.g. '1.0.0')
        channel: Update channel — 'stable', 'beta', or 'canary'
        callback: Optional fn(latest_version, download_url) called if update available
    """

    def _check():
        try:
            if channel == "stable":
                # Fast path: use /releases/latest for stable channel
                resp = requests.get(
                    f"{RELEASES_URL}/latest",
                    headers={"Accept": "application/vnd.github+json"},
                    timeout=10,
                )
                if resp.status_code != 200:
                    return
                releases = [resp.json()]
            else:
                # Fetch recent releases and filter by channel
                resp = requests.get(
                    RELEASES_URL,
                    headers={"Accept": "application/vnd.github+json"},
                    params={"per_page": 20},
                    timeout=10,
                )
                if resp.status_code != 200:
                    return
                releases = resp.json()
                if not isinstance(releases, list):
                    return

            # Find the newest release matching the channel
            best = None
            best_tuple = None
            for rel in releases:
                if not _matches_channel(rel, channel):
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
            logger.info(
                f"Update available ({channel}): {current_version} -> {latest_tag} — {html_url}"
            )

            if callback:
                callback(latest_tag.lstrip("v"), html_url)

        except Exception as e:
            logger.debug(f"Update check failed: {e}")

    thread = threading.Thread(target=_check, name="update-checker", daemon=True)
    thread.start()
