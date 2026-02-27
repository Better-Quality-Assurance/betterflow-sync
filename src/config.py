"""Configuration management for BetterFlow Sync."""

import json
import logging
import os
import sys
from dataclasses import dataclass, field, asdict
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from platformdirs import user_config_dir, user_data_dir, user_log_dir

__all__ = [
    "Config",
    "PrivacySettings",
    "SyncSettings",
    "AWSettings",
    "ReminderSettings",
    "ScreenshotSettings",
    "setup_logging",
    "DEFAULT_API_URL",
    "DEFAULT_WEB_BASE_URL",
    "MAX_QUEUE_SIZE",
]

logger = logging.getLogger(__name__)

APP_NAME = "BetterFlow Sync"
APP_AUTHOR = "BetterQA"


def _load_dotenv() -> None:
    """Load environment variables from a local .env file (if present)."""
    candidates: list[Path] = []
    if os.getenv("BETTERFLOW_SYNC_ENV_FILE"):
        candidates.append(Path(os.environ["BETTERFLOW_SYNC_ENV_FILE"]).expanduser())

    # Installed app runtime config location.
    candidates.append(Path(user_config_dir(APP_NAME, APP_AUTHOR)) / ".env")

    # Prefer a .env in current working directory, then project root in source runs.
    candidates.append(Path.cwd() / ".env")
    candidates.append(Path(__file__).resolve().parents[1] / ".env")

    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if not resolved.exists() or not resolved.is_file():
            continue

        try:
            with open(resolved, "r", encoding="utf-8") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    if not key:
                        continue
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                        value = value[1:-1]
                    os.environ.setdefault(key, value)
            return
        except Exception as e:
            logger.warning(f"Failed loading .env file at {resolved}: {e}")


if "pytest" not in sys.modules:
    _load_dotenv()

# API endpoints
DEFAULT_API_URL = os.getenv("BETTERFLOW_API_URL", "https://app.betterflow.eu/api/agent").rstrip("/")
DEFAULT_WEB_BASE_URL = os.getenv("BETTERFLOW_WEB_BASE_URL")
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
            # IDEs and code editors
            "Visual Studio Code",
            "Code",
            "Cursor",
            "PyCharm",
            "IntelliJ IDEA",
            "WebStorm",
            "PhpStorm",
            "GoLand",
            "CLion",
            "Rider",
            "RubyMine",
            "DataGrip",
            "RustRover",
            "Fleet",
            "Android Studio",
            "Xcode",
            "Visual Studio",
            "Sublime Text",
            "Nova",
            "BBEdit",
            "Zed",
            "Vim",
            "Neovim",
            "nvim",
            "Eclipse",
            # Terminals
            "Terminal",
            "iTerm2",
            "iTerm",
            "Windows Terminal",
            "PowerShell",
            "Command Prompt",
            "Warp",
            "Alacritty",
            "Kitty",
            "WezTerm",
            "Hyper",
            # API and database tools
            "Postman",
            "Insomnia",
            "DBeaver",
            "TablePlus",
            "pgAdmin",
            "MongoDB Compass",
            "Redis Insight",
            # Design tools
            "Figma",
            "Sketch",
            "Adobe XD",
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
    afk_timeout_minutes: int = 10

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass
class ReminderSettings:
    """Reminder notification settings."""

    break_reminders_enabled: bool = True
    break_interval_hours: int = 2  # 1, 2, 3, or 4
    private_reminders_enabled: bool = True
    private_interval_minutes: int = 20  # 10, 20, or 30


@dataclass
class ScreenshotSettings:
    """Screenshot capture settings."""

    enabled: bool = False
    interval_seconds: int = 300  # 5 min default
    quality: int = 80  # JPEG quality (1-100)


@dataclass
class Config:
    """Main configuration object."""

    api_url: str = DEFAULT_API_URL
    device_id: Optional[str] = None
    aw: AWSettings = field(default_factory=AWSettings)
    sync: SyncSettings = field(default_factory=SyncSettings)
    privacy: PrivacySettings = field(default_factory=PrivacySettings)
    reminders: ReminderSettings = field(default_factory=ReminderSettings)
    screenshots: ScreenshotSettings = field(default_factory=ScreenshotSettings)
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
                original_api_url = data.get("api_url")
                config = cls._from_dict(data)

                # Persist API URL migrations so subsequent runs use the normalized value.
                if original_api_url != config.api_url:
                    try:
                        config.save()
                    except Exception as e:
                        logger.warning(f"Failed to persist migrated config: {e}")

                return config
            except Exception as e:
                logger.warning(f"Failed to load config: {e}, using defaults")
        return cls()

    @classmethod
    def _from_dict(cls, data: dict) -> "Config":
        """Create Config from dictionary."""
        # Explicit runtime override (e.g. local backend for installed app).
        env_api_url = os.getenv("BETTERFLOW_API_URL")
        if env_api_url:
            data["api_url"] = env_api_url.rstrip("/")

        aw_data = data.pop("aw", {})
        sync_data = data.pop("sync", {})
        privacy_data = data.pop("privacy", {})
        reminders_data = data.pop("reminders", {})
        screenshots_data = data.pop("screenshots", {})

        # Migrate legacy localhost URLs to production endpoint.
        api_url = data.get("api_url")
        if api_url in {
            "http://localhost:8000/api/agent",
            "http://127.0.0.1:8000/api/agent",
            "http://localhost:8001/api/agent",
            "http://127.0.0.1:8001/api/agent",
        }:
            data["api_url"] = DEFAULT_API_URL

        return cls(
            aw=AWSettings(**aw_data) if aw_data else AWSettings(),
            sync=SyncSettings(**sync_data) if sync_data else SyncSettings(),
            privacy=PrivacySettings(**privacy_data) if privacy_data else PrivacySettings(),
            reminders=ReminderSettings(**reminders_data) if reminders_data else ReminderSettings(),
            screenshots=ScreenshotSettings(**screenshots_data) if screenshots_data else ScreenshotSettings(),
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

    @staticmethod
    def _to_bool(value) -> bool:
        """Coerce a server config value to bool safely.

        Handles strings like "false", "0", "no" that bool() gets wrong.
        """
        if isinstance(value, str):
            return value.lower() not in ("false", "0", "no", "")
        return bool(value)

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
                self.privacy.hash_titles = self._to_bool(privacy["hash_window_titles"])
            if "title_allowlist" in privacy:
                self.privacy.title_allowlist = privacy["title_allowlist"]
            if "track_browser_domains" in privacy:
                # Server tracks domains = we extract domain only
                self.privacy.domain_only_urls = self._to_bool(privacy["track_browser_domains"])
            if "collect_full_urls" in privacy:
                self.privacy.collect_full_urls = self._to_bool(privacy["collect_full_urls"])

        if "collection" in server_config:
            collection = server_config["collection"]
            if "collect_page_category" in collection:
                self.privacy.collect_page_category = self._to_bool(collection["collect_page_category"])

        if "tracking" in server_config:
            tracking = server_config["tracking"]
            if "afk_timeout_minutes" in tracking:
                val = tracking["afk_timeout_minutes"]
                if val in (10, 20, 30):
                    self.aw.afk_timeout_minutes = val

        if "sync" in server_config:
            sync = server_config["sync"]
            if "sync_interval_seconds" in sync:
                self.sync.interval_seconds = max(30, sync["sync_interval_seconds"])
            if "batch_size" in sync:
                self.sync.batch_size = min(sync["batch_size"], MAX_BATCH_SIZE)

        if "screenshots" in server_config:
            ss = server_config["screenshots"]
            if "enabled" in ss:
                self.screenshots.enabled = self._to_bool(ss["enabled"])
            if "interval_seconds" in ss:
                self.screenshots.interval_seconds = max(60, int(ss["interval_seconds"]))
            if "quality" in ss:
                self.screenshots.quality = max(10, min(100, int(ss["quality"])))

        self.save()


def setup_logging(debug: bool = False) -> None:
    """Configure logging.

    Safe to call multiple times (e.g. when toggling debug mode at runtime).
    """
    log_dir = Config.get_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "betterflow-sync.log"

    level = logging.DEBUG if debug else logging.INFO
    format_str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    formatter = logging.Formatter(format_str)

    root = logging.getLogger()
    root.setLevel(level)

    # Remove existing handlers to support runtime re-configuration
    for h in root.handlers[:]:
        root.removeHandler(h)
        h.close()

    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3,
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    # Reduce noise from libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
