"""Configuration management for BetterFlow Sync."""

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from platformdirs import user_config_dir, user_data_dir, user_log_dir

__all__ = [
    "Config",
    "PrivacySettings",
    "SyncSettings",
    "AWSettings",
    "setup_logging",
    "DEFAULT_API_URL",
    "MAX_QUEUE_SIZE",
]

logger = logging.getLogger(__name__)

APP_NAME = "BetterFlow Sync"
APP_AUTHOR = "BetterQA"

# API endpoints
DEFAULT_API_URL = "http://127.0.0.1:8001/api/agent"
STAGING_API_URL = "https://staging.betterflow.eu/api/agent"

# ActivityWatch defaults
DEFAULT_AW_HOST = "localhost"
DEFAULT_AW_PORT = 5600

# Sync settings
DEFAULT_SYNC_INTERVAL = 60  # seconds
DEFAULT_BATCH_SIZE = 100
MAX_BATCH_SIZE = 1000
MAX_QUEUE_SIZE = 100000  # ~1 week of events


@dataclass
class PrivacySettings:
    """Privacy configuration."""

    hash_titles: bool = True  # Hash window titles by default
    title_allowlist: list[str] = field(
        default_factory=lambda: [
            "Visual Studio Code",
            "PyCharm",
            "IntelliJ IDEA",
            "WebStorm",
            "Terminal",
            "iTerm2",
            "Windows Terminal",
            "Cursor",
        ]
    )
    domain_only_urls: bool = True  # Strip URLs to domain only
    collect_full_urls: bool = False  # Collect full URLs (sensitive, opt-in)
    collect_page_category: bool = True  # Include coarse page category classification
    exclude_apps: list[str] = field(
        default_factory=lambda: [
            "1Password",
            "Keychain Access",
            "System Preferences",
            "System Settings",
        ]
    )


@dataclass
class SyncSettings:
    """Sync configuration."""

    interval_seconds: int = DEFAULT_SYNC_INTERVAL
    batch_size: int = DEFAULT_BATCH_SIZE
    compress: bool = True  # Use gzip compression


@dataclass
class AWSettings:
    """ActivityWatch connection settings."""

    host: str = DEFAULT_AW_HOST
    port: int = DEFAULT_AW_PORT

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass
class Config:
    """Main configuration object."""

    api_url: str = DEFAULT_API_URL
    device_id: Optional[str] = None
    aw: AWSettings = field(default_factory=AWSettings)
    sync: SyncSettings = field(default_factory=SyncSettings)
    privacy: PrivacySettings = field(default_factory=PrivacySettings)
    setup_complete: bool = False
    auto_start: bool = False
    check_updates: bool = True
    debug_mode: bool = False

    @classmethod
    def get_config_dir(cls) -> Path:
        """Get the configuration directory path."""
        return Path(user_config_dir(APP_NAME, APP_AUTHOR))

    @classmethod
    def get_data_dir(cls) -> Path:
        """Get the data directory path (for SQLite queue, etc.)."""
        return Path(user_data_dir(APP_NAME, APP_AUTHOR))

    @classmethod
    def get_log_dir(cls) -> Path:
        """Get the log directory path."""
        return Path(user_log_dir(APP_NAME, APP_AUTHOR))

    @classmethod
    def get_config_file(cls) -> Path:
        """Get the config file path."""
        return cls.get_config_dir() / "config.json"

    @classmethod
    def load(cls) -> "Config":
        """Load config from file, or return defaults."""
        config_file = cls.get_config_file()
        if config_file.exists():
            try:
                with open(config_file, "r") as f:
                    data = json.load(f)
                return cls._from_dict(data)
            except Exception as e:
                logger.warning(f"Failed to load config: {e}, using defaults")
        return cls()

    @classmethod
    def _from_dict(cls, data: dict) -> "Config":
        """Create Config from dictionary."""
        aw_data = data.pop("aw", {})
        sync_data = data.pop("sync", {})
        privacy_data = data.pop("privacy", {})

        # Normalize legacy localhost URLs to current local backend default.
        # Keeps old installs from sticking to port 8000 after local backend moved.
        api_url = data.get("api_url")
        if api_url in {
            "http://localhost:8000/api/agent",
            "http://127.0.0.1:8000/api/agent",
            "http://localhost:8001/api/agent",
        }:
            data["api_url"] = DEFAULT_API_URL

        return cls(
            aw=AWSettings(**aw_data) if aw_data else AWSettings(),
            sync=SyncSettings(**sync_data) if sync_data else SyncSettings(),
            privacy=PrivacySettings(**privacy_data) if privacy_data else PrivacySettings(),
            **{k: v for k, v in data.items() if k in cls.__dataclass_fields__},
        )

    def save(self) -> None:
        """Save config to file."""
        config_file = self.get_config_file()
        config_file.parent.mkdir(parents=True, exist_ok=True)

        data = asdict(self)
        with open(config_file, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"Config saved to {config_file}")

    def update_from_server(self, server_config: dict) -> None:
        """Update local config from server response.

        Server returns:
            privacy.hash_window_titles -> local hash_titles
            privacy.title_allowlist -> local title_allowlist
            privacy.track_browser_domains -> local domain_only_urls (inverted)
            sync.sync_interval_seconds -> local interval_seconds
            sync.batch_size -> local batch_size
        """
        if "privacy" in server_config:
            privacy = server_config["privacy"]
            if "hash_window_titles" in privacy:
                self.privacy.hash_titles = privacy["hash_window_titles"]
            if "title_allowlist" in privacy:
                self.privacy.title_allowlist = privacy["title_allowlist"]
            if "track_browser_domains" in privacy:
                # Server tracks domains = we extract domain only
                self.privacy.domain_only_urls = privacy["track_browser_domains"]
            if "collect_full_urls" in privacy:
                self.privacy.collect_full_urls = bool(privacy["collect_full_urls"])

        if "collection" in server_config:
            collection = server_config["collection"]
            if "collect_page_category" in collection:
                self.privacy.collect_page_category = bool(collection["collect_page_category"])

        if "sync" in server_config:
            sync = server_config["sync"]
            if "sync_interval_seconds" in sync:
                self.sync.interval_seconds = max(30, sync["sync_interval_seconds"])
            if "batch_size" in sync:
                self.sync.batch_size = min(sync["batch_size"], MAX_BATCH_SIZE)

        self.save()


def setup_logging(debug: bool = False) -> None:
    """Configure logging."""
    log_dir = Config.get_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "betterflow-sync.log"

    level = logging.DEBUG if debug else logging.INFO
    format_str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    logging.basicConfig(
        level=level,
        format=format_str,
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
    )

    # Reduce noise from libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
