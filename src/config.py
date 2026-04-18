"""Configuration management for BetterFlow."""

import json
import logging
import math
import os
import re
import sys
import threading
import uuid
from dataclasses import dataclass, field, fields as dc_fields, asdict
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
    "EngagementConfig",
    "FraudDetectionConfig",
    "CallDetectionSettings",
    "setup_logging",
    "get_machine_uuid",
    "DEFAULT_API_URL",
    "DEFAULT_WEB_BASE_URL",
    "MAX_QUEUE_SIZE",
]

logger = logging.getLogger(__name__)

APP_NAME = "BetterFlow"
APP_AUTHOR = "BetterQA"


def _load_dotenv() -> None:
    """Load environment variables from a local .env file (if present)."""
    candidates: list[Path] = []
    if os.getenv("BETTERFLOW_ENV_FILE"):
        candidates.append(Path(os.environ["BETTERFLOW_ENV_FILE"]).expanduser())

    # Installed app runtime config location.
    candidates.append(Path(user_config_dir(APP_NAME, APP_AUTHOR)) / ".env")

    # In development (non-frozen) builds, also check cwd and project root.
    # Skipped in production PyInstaller bundles to prevent credential redirect
    # via a planted .env file in an attacker-controlled working directory.
    if not getattr(sys, "frozen", False):
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
DEFAULT_SYNC_INTERVAL = 30  # seconds
DEFAULT_BATCH_SIZE = 100
MAX_BATCH_SIZE = 1000
MAX_QUEUE_SIZE = 100000  # ~1 week of events

# Persistent machine UUID: cached in memory after first read.
_machine_uuid_cache: Optional[str] = None
_machine_uuid_lock = threading.Lock()
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def get_machine_uuid() -> str:
    """Return a persistent UUID for this machine.

    On first call, reads from (or generates and writes to) a `.machine_id`
    file in the config directory. Subsequent calls return the in-memory
    cache. The UUID survives app updates and hostname changes; it is only
    lost on full uninstall.

    Thread-safe: uses double-checked locking so the hot path (cache hit)
    is lock-free while the one-time generation is serialized.
    """
    global _machine_uuid_cache

    # Fast path: already cached, no lock needed.
    if _machine_uuid_cache is not None:
        return _machine_uuid_cache

    with _machine_uuid_lock:
        # Re-check under lock; another thread may have populated it.
        if _machine_uuid_cache is not None:
            return _machine_uuid_cache

        config_dir = Path(user_config_dir(APP_NAME, APP_AUTHOR))
        id_file = config_dir / ".machine_id"

        try:
            value = id_file.read_text(encoding="utf-8").strip()
            if value and _UUID_RE.match(value):
                _machine_uuid_cache = value
                return value
            if value:
                logger.warning("machine_id file contains invalid UUID, regenerating")
        except (OSError, UnicodeDecodeError) as e:
            logger.debug(f"No existing machine ID file: {e}")

        # Generate and persist a new UUID (atomic write: tmp + rename).
        new_id = str(uuid.uuid4())
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            tmp_file = id_file.with_suffix(".tmp")
            tmp_file.write_text(new_id, encoding="utf-8")
            os.replace(tmp_file, id_file)
        except OSError as e:
            logger.warning(f"Failed to write machine ID file: {e}")
            try:
                tmp_file.unlink(missing_ok=True)
            except OSError as cleanup_err:
                logger.debug("Cleanup of tmp machine-id file failed: %s", cleanup_err)

        _machine_uuid_cache = new_id
        return new_id


@dataclass
class PrivacySettings:
    """Privacy configuration."""

    hash_titles: bool = False  # Send actual window titles for categorization
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
    auto_categorize: bool = True  # Enrich events with app_category from server mappings
    track_display_info: bool = False  # Track monitor name and virtual desktop
    exclude_apps: list[str] = field(
        default_factory=lambda: [
            "1Password",
            "Keychain Access",
            "System Preferences",
            "System Settings",
            # macOS system agents
            "SecurityAgent",
            "UserNotificationCenter",
            "loginwindow",
            "ScreenSaverEngine",
            "CoreServicesUIAgent",
            "AirPlayUIAgent",
            "SystemUIServer",
            # Auto-updaters
            "Microsoft AutoUpdate",
            "Software Update",
        ]
    )
    default_categories: dict[str, str] = field(
        default_factory=lambda: {
            # Development
            "Claude": "development",
            "ChatGPT": "development",
            "Copilot": "development",
            "Cursor": "development",
            "Warp": "development",
            "iTerm2": "development",
            "Docker Desktop": "development",
            "Postman": "development",
            # Productivity
            "Calendar": "productivity",
            "Reminders": "productivity",
            "Notes": "productivity",
            "Notion": "productivity",
            "Microsoft Word": "productivity",
            "Microsoft Excel": "productivity",
            "Preview": "productivity",
            # Communication
            "WhatsApp": "communication",
            "Telegram": "communication",
            "Messages": "communication",
            "FaceTime": "communication",
            "Discord": "communication",
            # Social
            "Twitter": "social",
            "LinkedIn": "social",
            # Browsing
            "Brave Browser": "browsing",
            "Arc": "browsing",
            "Comet": "browsing",
            # Uncategorized (known but not classifiable)
            "Finder": "uncategorized",
            "Unknown": "uncategorized",
            "Activity Monitor": "uncategorized",
        }
    )


@dataclass
class SyncSettings:
    """Sync configuration."""

    interval_seconds: int = DEFAULT_SYNC_INTERVAL
    batch_size: int = DEFAULT_BATCH_SIZE
    compress: bool = True  # Use gzip compression
    idle_pause_minutes: int = 20  # Pause sync after this many minutes AFK
    min_window_event_seconds: float = 5.0  # Drop window/web events shorter than this


@dataclass
class EngagementConfig:
    """Engagement detection settings (server-configurable).

    These thresholds determine what counts as "engaged" work vs "idle-active"
    (mouse-only activity that may indicate fake activity).
    """

    sustained_typing_presses: int = 50  # Presses in window = engaged
    window_changes_min: int = 2  # Task switching = engaged
    scroll_threshold: int = 10  # Reading behavior = engaged
    combined_presses_min: int = 10  # For combined signal checks
    combined_scrolls_min: int = 5  # For combined signal checks
    window_minutes: int = 5  # Rolling window size in minutes


@dataclass
class FraudDetectionConfig:
    """Fraud detection signal thresholds (server-configurable).

    These thresholds control the sensitivity of client-side fraud detection
    signals. Each signal produces a sub-score; the total is capped at 100.
    """

    keystroke_cv_threshold: float = 0.1  # Below this = suspiciously uniform
    min_windows_for_variance: int = 6  # Need this many windows before checking
    mouse_only_streak_threshold: int = 3  # Consecutive mouse-only windows
    min_app_diversity: int = 2  # Fewer unique apps = suspicious
    app_diversity_min_minutes: int = 60  # Only check after this much active time
    click_keystroke_ratio_threshold: float = 10.0  # Above this = suspicious
    input_regularity_cv_threshold: float = 0.1  # Below this = suspiciously regular
    min_input_events_for_regularity: int = 10  # Need this many events for regularity check


@dataclass
class CallDetectionSettings:
    """Call/meeting detection settings."""

    enabled: bool = True
    min_call_duration: int = 30  # Seconds; skip accidental opens


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
    break_duration_minutes: int = 15  # Auto-break pause duration
    private_reminders_enabled: bool = True
    private_interval_minutes: int = 20  # 10, 20, or 30


@dataclass
class Config:
    """Main configuration object."""

    api_url: str = DEFAULT_API_URL
    device_id: Optional[str] = None
    aw: AWSettings = field(default_factory=AWSettings)
    sync: SyncSettings = field(default_factory=SyncSettings)
    privacy: PrivacySettings = field(default_factory=PrivacySettings)
    reminders: ReminderSettings = field(default_factory=ReminderSettings)
    engagement: EngagementConfig = field(default_factory=EngagementConfig)
    fraud_detection: FraudDetectionConfig = field(default_factory=FraudDetectionConfig)
    call_detection: CallDetectionSettings = field(default_factory=CallDetectionSettings)
    setup_complete: bool = False
    auto_start: bool = False
    check_updates: bool = True
    auto_install_updates: bool = True
    update_channel: str = "stable"
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
        data = dict(data)  # Shallow copy to avoid mutating caller's dict
        # Explicit runtime override (e.g. local backend for installed app).
        env_api_url = os.getenv("BETTERFLOW_API_URL")
        if env_api_url:
            data["api_url"] = env_api_url.rstrip("/")

        aw_data = data.pop("aw", {})
        sync_data = data.pop("sync", {})
        privacy_data = data.pop("privacy", {})
        reminders_data = data.pop("reminders", {})
        engagement_data = data.pop("engagement", {})
        fraud_detection_data = data.pop("fraud_detection", {})
        call_detection_data = data.pop("call_detection", {})
        data.pop("screenshots", None)

        # Migrate legacy localhost:8000 URLs to production endpoint.
        # Note: 8001 is intentionally excluded — it is the standard local dev port.
        api_url = data.get("api_url")
        if api_url in {
            "http://localhost:8000/api/agent",
            "http://127.0.0.1:8000/api/agent",
        }:
            data["api_url"] = DEFAULT_API_URL

        def _safe(dc_cls, d):
            """Strip unknown keys before constructing a dataclass."""
            valid = {f.name for f in dc_fields(dc_cls)}
            return dc_cls(**{k: v for k, v in d.items() if k in valid})

        return cls(
            aw=_safe(AWSettings, aw_data) if aw_data else AWSettings(),
            sync=_safe(SyncSettings, sync_data) if sync_data else SyncSettings(),
            privacy=_safe(PrivacySettings, privacy_data) if privacy_data else PrivacySettings(),
            reminders=_safe(ReminderSettings, reminders_data) if reminders_data else ReminderSettings(),
            engagement=_safe(EngagementConfig, engagement_data) if engagement_data else EngagementConfig(),
            fraud_detection=_safe(FraudDetectionConfig, fraud_detection_data) if fraud_detection_data else FraudDetectionConfig(),
            call_detection=_safe(CallDetectionSettings, call_detection_data) if call_detection_data else CallDetectionSettings(),
            **{k: v for k, v in data.items() if k in cls.__dataclass_fields__},
        )

    def save(self) -> None:
        """Save config to file atomically (write tmp then rename)."""
        config_file = self.get_config_file()
        config_file.parent.mkdir(parents=True, exist_ok=True)

        data = asdict(self)
        # default_categories are built-in client defaults; server overrides
        # live in the DB via sync_categories.  Don't persist to avoid unbounded
        # growth and inability for the server to retract entries.
        data.get("privacy", {}).pop("default_categories", None)
        tmp_file = config_file.with_suffix(".tmp")
        try:
            with open(tmp_file, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_file, config_file)
        except (OSError, ValueError):
            try:
                tmp_file.unlink(missing_ok=True)
            except OSError as cleanup_err:
                logger.debug("Cleanup of tmp config file failed: %s", cleanup_err)
            raise
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
            if "auto_categorize" in collection:
                self.privacy.auto_categorize = self._to_bool(collection["auto_categorize"])
            if "track_display_info" in collection:
                self.privacy.track_display_info = self._to_bool(collection["track_display_info"])
            if "default_categories" in collection:
                cats = collection["default_categories"]
                if isinstance(cats, dict):
                    valid = {
                        k: v for k, v in cats.items()
                        if isinstance(k, str) and isinstance(v, str) and k and v
                    }
                    # Merge: server entries override matching keys but
                    # built-in defaults survive for apps the server doesn't mention.
                    merged = dict(self.privacy.default_categories)
                    merged.update(valid)
                    self.privacy.default_categories = merged

        if "tracking" in server_config:
            tracking = server_config["tracking"]
            if "afk_timeout_minutes" in tracking:
                val = tracking["afk_timeout_minutes"]
                if val in (10, 20, 30):
                    self.aw.afk_timeout_minutes = val

        if "sync" in server_config:
            sync = server_config["sync"]
            if "sync_interval_seconds" in sync:
                try:
                    self.sync.interval_seconds = max(30, int(sync["sync_interval_seconds"]))
                except (TypeError, ValueError):
                    logger.warning("Invalid sync_interval_seconds from server, ignoring")
            if "batch_size" in sync:
                try:
                    self.sync.batch_size = max(1, min(int(sync["batch_size"]), MAX_BATCH_SIZE))
                except (TypeError, ValueError):
                    logger.warning("Invalid batch_size from server, ignoring")
            if "idle_pause_minutes" in sync:
                try:
                    val = int(sync["idle_pause_minutes"])
                    if 5 <= val <= 120:
                        self.sync.idle_pause_minutes = val
                except (TypeError, ValueError):
                    logger.warning("Invalid idle_pause_minutes from server, ignoring")
            if "min_window_event_seconds" in sync:
                try:
                    val = float(sync["min_window_event_seconds"])
                    if 0 <= val <= 30:
                        self.sync.min_window_event_seconds = val
                except (TypeError, ValueError):
                    logger.warning("Invalid min_window_event_seconds from server, ignoring")

        if "engagement" in server_config:
            eng = server_config["engagement"]
            try:
                if "sustained_typing_presses" in eng:
                    self.engagement.sustained_typing_presses = max(1, int(eng["sustained_typing_presses"]))
                if "window_changes_min" in eng:
                    self.engagement.window_changes_min = max(1, int(eng["window_changes_min"]))
                if "scroll_threshold" in eng:
                    self.engagement.scroll_threshold = max(1, int(eng["scroll_threshold"]))
                if "combined_presses_min" in eng:
                    self.engagement.combined_presses_min = max(1, int(eng["combined_presses_min"]))
                if "combined_scrolls_min" in eng:
                    self.engagement.combined_scrolls_min = max(1, int(eng["combined_scrolls_min"]))
                if "window_minutes" in eng:
                    self.engagement.window_minutes = max(1, int(eng["window_minutes"]))
            except (TypeError, ValueError) as e:
                logger.warning(f"Invalid engagement config from server: {e}")

        if "fraud_detection" in server_config:
            fd = server_config["fraud_detection"]
            try:
                if "keystroke_cv_threshold" in fd:
                    val = float(fd["keystroke_cv_threshold"])
                    if math.isfinite(val) and val > 0:
                        self.fraud_detection.keystroke_cv_threshold = val
                if "min_windows_for_variance" in fd:
                    self.fraud_detection.min_windows_for_variance = max(2, int(fd["min_windows_for_variance"]))
                if "mouse_only_streak_threshold" in fd:
                    self.fraud_detection.mouse_only_streak_threshold = max(1, int(fd["mouse_only_streak_threshold"]))
                if "min_app_diversity" in fd:
                    self.fraud_detection.min_app_diversity = max(1, int(fd["min_app_diversity"]))
                if "app_diversity_min_minutes" in fd:
                    self.fraud_detection.app_diversity_min_minutes = max(1, int(fd["app_diversity_min_minutes"]))
                if "click_keystroke_ratio_threshold" in fd:
                    val = float(fd["click_keystroke_ratio_threshold"])
                    if math.isfinite(val) and val > 0:
                        self.fraud_detection.click_keystroke_ratio_threshold = val
                if "input_regularity_cv_threshold" in fd:
                    val = float(fd["input_regularity_cv_threshold"])
                    if math.isfinite(val) and val > 0:
                        self.fraud_detection.input_regularity_cv_threshold = val
                if "min_input_events_for_regularity" in fd:
                    self.fraud_detection.min_input_events_for_regularity = max(2, int(fd["min_input_events_for_regularity"]))
            except (TypeError, ValueError) as e:
                logger.warning(f"Invalid fraud_detection config from server: {e}")

        if "call_detection" in server_config:
            cd = server_config["call_detection"]
            if "enabled" in cd:
                self.call_detection.enabled = self._to_bool(cd["enabled"])
            if "min_call_duration" in cd:
                try:
                    self.call_detection.min_call_duration = max(0, int(cd["min_call_duration"]))
                except (TypeError, ValueError):
                    logger.warning("Invalid min_call_duration from server, ignoring")

        try:
            self.save()
        except OSError as e:
            logger.warning("Failed to persist server config to disk: %s", e)


def setup_logging(debug: bool = False) -> None:
    """Configure logging.

    Safe to call multiple times (e.g. when toggling debug mode at runtime).
    """
    log_dir = Config.get_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "betterflow.log"

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
