"""Privacy filtering for ActivityWatch events."""

import hashlib
import logging
from typing import Optional
from urllib.parse import urlparse

try:
    from ..config import PrivacySettings
except ImportError:
    from config import PrivacySettings

logger = logging.getLogger(__name__)


class PrivacyFilter:
    """Applies privacy rules to events before syncing."""

    def __init__(self, settings: PrivacySettings):
        """Initialize privacy filter.

        Args:
            settings: Privacy settings from config
        """
        self.settings = settings

    def should_exclude_app(self, app: Optional[str]) -> bool:
        """Check if app should be excluded from tracking.

        Args:
            app: Application name

        Returns:
            True if app should be excluded
        """
        if not app:
            return False
        return app in self.settings.exclude_apps

    def process_title(self, app: Optional[str], title: Optional[str]) -> Optional[str]:
        """Process window title according to privacy settings.

        Args:
            app: Application name (for allowlist check)
            title: Window title

        Returns:
            Processed title (hashed, original, or None)
        """
        if not title:
            return None

        # Check if app is in allowlist for raw titles
        if app and app in self.settings.title_allowlist:
            return title

        # Hash title if configured
        if self.settings.hash_titles:
            return self.hash_string(title)

        return title

    def process_url(self, url: Optional[str]) -> Optional[str]:
        """Process URL according to privacy settings.

        Args:
            url: Full URL

        Returns:
            Processed URL (domain only or full)
        """
        if not url:
            return None

        if self.settings.domain_only_urls:
            return self.extract_domain(url)

        return url

    def extract_domain(self, url: str) -> Optional[str]:
        """Extract domain from URL.

        Args:
            url: Full URL

        Returns:
            Domain name only, or None if parsing fails
        """
        try:
            parsed = urlparse(url)
            return parsed.netloc or None
        except Exception:
            return None

    def hash_string(self, value: str) -> str:
        """Hash a string with SHA-256.

        Returns first 16 characters of hex digest for readability
        while maintaining uniqueness for practical purposes.

        Args:
            value: String to hash

        Returns:
            First 16 chars of SHA-256 hex digest
        """
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    def is_app_allowlisted(self, app: Optional[str]) -> bool:
        """Check if app is in title allowlist.

        Args:
            app: Application name

        Returns:
            True if app allows raw titles
        """
        if not app:
            return False
        return app in self.settings.title_allowlist
