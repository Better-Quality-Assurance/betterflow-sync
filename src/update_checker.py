"""Check for new releases via GitHub Releases API."""

import logging
import threading
from typing import Optional

import requests

logger = logging.getLogger(__name__)

GITHUB_REPO = "betterqa/betterflow-sync"
RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse 'v1.2.3' or '1.2.3' into a comparable tuple."""
    return tuple(int(x) for x in version.lstrip("v").split(".")[:3])


def check_for_update(
    current_version: str,
    callback: Optional[callable] = None,
) -> None:
    """Check GitHub for a newer release (runs in background thread).

    Args:
        current_version: Current app version (e.g. '1.0.0')
        callback: Optional fn(latest_version, download_url) called if update available
    """

    def _check():
        try:
            resp = requests.get(
                RELEASES_URL,
                headers={"Accept": "application/vnd.github+json"},
                timeout=10,
            )
            if resp.status_code != 200:
                return

            data = resp.json()
            latest_tag = data.get("tag_name", "")
            if not latest_tag:
                return

            try:
                if _version_tuple(latest_tag) <= _version_tuple(current_version):
                    return
            except (ValueError, TypeError):
                return

            html_url = data.get("html_url", "")
            logger.info(
                f"Update available: {current_version} -> {latest_tag} — {html_url}"
            )

            if callback:
                callback(latest_tag.lstrip("v"), html_url)

        except Exception as e:
            logger.debug(f"Update check failed: {e}")

    thread = threading.Thread(target=_check, name="update-checker", daemon=True)
    thread.start()
