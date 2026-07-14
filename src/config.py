"""Configuration management for BetterFlow."""

import json
import logging
import math
import os
import re
import sys
import threading
import uuid
from dataclasses import asdict, dataclass, field
from dataclasses import fields as dc_fields
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

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
    "WorkingHoursConfig",
    "setup_logging",
    "get_machine_uuid",
    "DEFAULT_API_URL",
    "DEFAULT_WEB_BASE_URL",
    "PRIVACY_POLICY_URL",
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

# Public privacy policy. Surfaced at the Input Monitoring gate and the
# tray Diagnostics submenu so the legal disclosure is one click away
# from any session. Update here, not in UI modules.
PRIVACY_POLICY_URL = "https://betterqa.co/privacy-policy-terms-of-service/"

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
    track_browser_urls: bool = False  # read active-tab URL without an extension (macOS: AppleScript / Windows: UI Automation)
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
    # Generate the AFK/active stream in-process from the OS idle clock + input
    # watcher instead of the external bf-idle-tracker bucket. Kill-switch: set
    # False to fall back to the external bucket + stale-synthesis path.
    in_process_afk: bool = True
    # Stage 2 of tracker-convergence: when in_process_afk is the source, STOP the
    # external bf-idle-tracker process (not just ignore its bucket), eliminating
    # the dual-source surface that produced Bug A. Default OFF — opt-in, and
    # independently reversible: with the tracker stopped, recovery can't fall back
    # to it without flipping a flag, so this ships dark until validated.
    stop_external_afk_tracker: bool = False
    # Generate the per-app active-window stream in-process from the OS
    # frontmost-window probe (+ psutil process name) instead of the external
    # bf-window-tracker bucket. Same convergence move as in_process_afk, for
    # machines where the bundled aw-watcher-window launches but its Win32 capture
    # returns zero events for hours. Default OFF — ships dormant/opt-in; when on
    # AND the probe is usable, the external window bucket is skipped so the two
    # sources never double-count.
    in_process_window: bool = False
    # Count keystrokes/clicks/scrolls in-process (Windows ctypes low-level hooks
    # / macOS CGEventTap) instead of relying on the external aw-watcher-input
    # tracker. Same convergence move as in_process_window, for machines where
    # aw-watcher-input's low-level hook is blocked (UIPI / AV) and reports ZERO
    # keystrokes/clicks for hours (Fraud Risk 75). Default OFF — ships
    # dormant/opt-in; when on AND an in-process backend is usable, the external
    # input bucket is skipped so the two sources never double-count.
    in_process_input: bool = False


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
class ForegroundActivitySettings:
    """Foreground-CPU activity detection — credits engaged-but-no-input work
    (e.g. watching an active Claude Code / build / render in the focused window)
    that the AFK timeout would otherwise mark idle.

    Guardrails are server-tunable: only the frontmost process is sampled, credit
    is anchored to real input (extends at most ``max_credit_minutes`` past the
    last keyboard/mouse input), and only sessions of at least
    ``min_session_seconds`` are uploaded for server validation."""

    # Default OFF: this changes tracked/billed time (it injects AFK credit and
    # dev-session spans) and its backend support (the dev-session bucket type) is
    # still an unfinished follow-up. Ship inert so a fleet update can't flip
    # billing for everyone; the server enables it via update_from_server once the
    # backend lands and it's been validated.
    enabled: bool = False
    cpu_threshold_percent: float = 15.0  # Frontmost-app CPU (single-core basis)
    max_credit_minutes: int = 20  # Max credit past the last real input
    min_session_seconds: int = 30  # Skip accidental blips


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
    # Hard safety cap: auto-end Private Time after this many continuous hours so a
    # forgotten toggle can't silently zero a whole day's billable time (Raluca,
    # 2026-06-25, ~11h private). 0 disables the cap. v1.5.79 already ends private
    # on sleep; this covers the awake-but-forgotten case.
    private_auto_end_hours: float = 4.0


def _normalize_hhmm(value) -> str:
    """Coerce a server-sent time to zero-padded HH:MM, or RAISE.

    allows() compares times as STRINGS, which is only correct when both sides are
    zero-padded ("7:30" > "22:00" lexically, so an unpadded start would let every
    hour of the night through).

    This must FAIL CLOSED, and an earlier version of this function did not: it
    substituted the widest possible default on bad input ("00:00" / "23:59") and
    the caller then set known=True anyway. A single backend typo — "7.30" instead
    of "07:30" — would silently widen a restricted user's window to start at
    midnight and record them all night. Raising instead lets update_from_server
    keep the schedule it already trusted (or none at all).
    """
    text = str(value).strip() if value is not None else ""
    # Seconds are tolerated but ignored: agent_work_schedules.work_start is a
    # string(5) today, so the wire carries "07:30" — but if that column is ever
    # migrated to TIME, Eloquent starts emitting "07:30:00", this would raise, known
    # would stay False, and EVERY restricted user would go dark. Cheap insurance.
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", text)
    if not match:
        raise ValueError(f"working-hours time is not HH:MM: {value!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"working-hours time out of range: {value!r}")
    return f"{hour:02d}:{minute:02d}"


# Fixing the /config envelope unwrap (bf_client.get_config) means server settings reach
# the agent for the FIRST TIME EVER — none of them have ever applied. Two of those move
# real-world behaviour and have nothing to do with working-hours enforcement, so they
# must land as their own deliberate change rather than as a side effect of a privacy fix:
#
#   tracking.afk_timeout_minutes  — 37 of 44 prod devices are set to 20 while every agent
#                                   has run the client default of 10. Applying it lengthens
#                                   the idle grace, i.e. it CHANGES BILLED HOURS.
#   privacy.hash_window_titles    — 41 of 44 are set to ON. Applying it turns window titles
#                                   into hashes, so admins lose readable titles.
#
# Both are what the DB has always said and what someone intended; they have simply never
# taken effect. Each needs its own release, with the affected people told first. Flip this
# to False (one setting at a time) to roll them out.
#
# working_hours is deliberately NOT gated by this: it is the whole point of the release.
DEFER_UNAPPLIED_SERVER_SETTINGS = True


@lru_cache(maxsize=8)
def _resolve_schedule_tz(name: str):
    """Resolve a schedule timezone, logging an unresolvable one ONCE.

    allows() runs per event, so logging inside it flooded the log on a single
    misconfigured timezone. lru_cache makes the body — and therefore the log line —
    run once per distinct name for the life of the process.
    """
    if not name:
        return None  # normal: production sends no timezone; use machine-local.
    try:
        return ZoneInfo(name)
    except Exception:
        # SET but unresolvable means the tz database is missing (Windows without
        # tzdata, which we now bundle). The machine-local fallback then evaluates the
        # window in a clock the employee can change, so say so — but keep the
        # fallback: failing closed here would silently stop tracking a whole platform.
        logger.error(
            "Working-hours timezone %r could not be resolved (missing tz database?) "
            "— falling back to machine-local time, which the user can change",
            name,
        )
        return None


def _normalize_working_days(value) -> list:
    """Coerce server-sent working days to a list of ISO weekdays, or RAISE.

    Fails closed for the same reason as _normalize_hhmm: allows() reads an EMPTY
    working_days as "no day restriction" (`if self.working_days and ...`), so a
    malformed [] would quietly turn a Mon-Fri schedule into a 7-day one.
    """
    if not isinstance(value, list) or not value:
        raise ValueError(f"working_days must be a non-empty list: {value!r}")
    days = [int(d) for d in value]  # ValueError/TypeError on junk — intended
    if any(d < 1 or d > 7 for d in days):
        raise ValueError(f"working_days out of ISO 1-7 range: {value!r}")
    return days


@dataclass
class WorkingHoursConfig:
    """Server-enforced working-hours window. When ``enforced``, the agent must
    NOT record a thing outside [work_start, work_end] on working_days, evaluated
    in ``timezone``. For B2E / Trainee-Intern this is 07:30-22:00 Mon-Fri; B2B
    and others are unrestricted (enforced=False).

    ``known`` is the fail-closed guard and the reason this class exists rather
    than a bare dict. Until the server has told us the schedule at least once, we
    do NOT know whether this person may be recorded, and "don't know" must mean
    "don't record" — not "record everything", which is what an ``enforced=False``
    default silently meant before. It is persisted, so an offline cold start
    reuses the last known schedule instead of falling back to permissive.
    """

    enforced: bool = False
    work_start: str = "00:00"
    work_end: str = "23:59"
    working_days: list = field(default_factory=lambda: [1, 2, 3, 4, 5, 6, 7])
    timezone: str = ""
    known: bool = False

    def allows(self, when: "datetime") -> bool:
        """True if this instant may be recorded at all — the single source of
        truth for both capture suppression (main) and the upload gate (sync).

        Fail-closed: an unknown schedule records nothing. Evaluated in the
        schedule's timezone, falling back to the machine's local zone (the
        employee's own clock is the right one for "don't watch my laptop at
        night") when the server sends none.
        """
        if not self.known:
            return False
        if not self.enforced:
            return True

        tz = _resolve_schedule_tz(self.timezone)
        local = when.astimezone(tz) if tz else when.astimezone()
        hhmm = local.strftime("%H:%M")

        # An empty working_days is treated as "no working day at all", not "every
        # day". _normalize_working_days already rejects [] from the server, so this
        # only bites a hand-edited config — and the safe reading of "no days
        # configured" is to record nothing.

        if self.work_start <= self.work_end:
            # Normal daytime window: the shift belongs to the day it falls on.
            if local.isoweekday() not in self.working_days:
                return False
            return self.work_start <= hhmm <= self.work_end

        # Overnight window (e.g. a 22:00-06:00 night shift). A plain
        # start <= hhmm <= end is EMPTY when start > end, which recorded such a user
        # for exactly zero seconds a day.
        #
        # The shift is named for the day it STARTS on, so the day-of-week test has to
        # be applied to that day — not to the instant's own local day. Testing the
        # instant's day gets it wrong in BOTH directions:
        #   - Sat 02:00 (the real tail of Friday's shift) would be denied, and
        #   - Mon 02:00 (the tail of a SUNDAY NIGHT the user does not work) would be
        #     ALLOWED and recorded. That second one is over-collection — precisely
        #     what this feature exists to prevent.
        if hhmm >= self.work_start:
            shift_day = local.isoweekday()               # evening half: today's shift
        elif hhmm <= self.work_end:
            shift_day = (local - timedelta(days=1)).isoweekday()  # morning half: yesterday's
        else:
            return False                                  # the daylight gap between shifts
        return shift_day in self.working_days

    def window_close_after(self, start: "datetime") -> "Optional[datetime]":
        """The instant the working-hours window containing ``start`` closes, in UTC.

        Used to CLAMP the end of a status span (idle/break/sleep/private) that began
        inside the window and ran past it. Gating those spans on their start alone was
        not enough: an idle span beginning 21:45 and ending 23:55 passed the gate and
        told the server the employee became active at 23:55 — the exact fact we are
        here not to collect. Clamping to 22:00 keeps the true in-window portion and
        leaks nothing about the evening.

        Returns None when there is no window to clamp to (unknown or unrestricted).
        """
        if not self.known or not self.enforced:
            return None

        tz = _resolve_schedule_tz(self.timezone)
        local = start.astimezone(tz) if tz else start.astimezone()
        end_h, end_m = (int(p) for p in self.work_end.split(":"))
        close = local.replace(hour=end_h, minute=end_m, second=0, microsecond=0)

        # For an overnight window the close belongs to the NEXT local day whenever
        # `start` is in the evening half.
        if self.work_start > self.work_end and local.strftime("%H:%M") >= self.work_start:
            close += timedelta(days=1)

        return close.astimezone(timezone.utc)


@dataclass
class Config:
    """Main configuration object."""

    api_url: str = DEFAULT_API_URL
    device_id: Optional[str] = None
    aw: AWSettings = field(default_factory=AWSettings)
    sync: SyncSettings = field(default_factory=SyncSettings)
    privacy: PrivacySettings = field(default_factory=PrivacySettings)
    reminders: ReminderSettings = field(default_factory=ReminderSettings)
    working_hours: WorkingHoursConfig = field(default_factory=WorkingHoursConfig)
    engagement: EngagementConfig = field(default_factory=EngagementConfig)
    fraud_detection: FraudDetectionConfig = field(default_factory=FraudDetectionConfig)
    call_detection: CallDetectionSettings = field(default_factory=CallDetectionSettings)
    foreground_activity: ForegroundActivitySettings = field(default_factory=ForegroundActivitySettings)
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
        foreground_activity_data = data.pop("foreground_activity", {})
        # working_hours was missing from this list until 2026-07-14, so it fell
        # through to the **data splat below and was rebuilt as a plain dict. A
        # dict has no attributes: update_from_server's `self.working_hours.
        # enforced = ...` then raised AttributeError (swallowed as "invalid
        # working_hours, ignoring") and _within_working_hours' `getattr(wh,
        # "enforced", False)` read False. Net effect: on EVERY device that had
        # ever written a config.json, working-hours enforcement was silently and
        # permanently off, and restricted users were recorded around the clock.
        working_hours_data = data.pop("working_hours", {})
        # Ignore any persisted foreground_activity.enabled — it's server-driven
        # and default-OFF. A device that ran a default-ON beta build already has
        # enabled=true on disk; honouring it would override the safe default on
        # update. Drop it on load so the code default wins; the server re-enables
        # per-session via update_from_server. (save() also stops writing it.) Log
        # when we drop a persisted True so a beta user's "dev-session credit
        # stopped after update" has a trail rather than a silent flip-off.
        if foreground_activity_data.pop("enabled", None) is True:
            logger.info(
                "Ignoring persisted foreground_activity.enabled=true on load; it "
                "is server-driven and default-OFF (server re-enables per-session)"
            )
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
            foreground_activity=_safe(ForegroundActivitySettings, foreground_activity_data) if foreground_activity_data else ForegroundActivitySettings(),
            working_hours=_safe(WorkingHoursConfig, working_hours_data) if working_hours_data else WorkingHoursConfig(),
            **{k: v for k, v in data.items() if k in cls.__dataclass_fields__},
        )

    def save(self) -> None:
        """Save config to file atomically (write tmp then rename)."""
        config_file = self.get_config_file()
        config_file.parent.mkdir(parents=True, exist_ok=True)

        data = asdict(self)
        # default_categories are built-in client defaults; fallback writes
        # accumulate in the app_categories DB table. Don't persist to avoid
        # unbounded growth and inability for the server to retract entries.
        data.get("privacy", {}).pop("default_categories", None)
        # Never persist foreground_activity.enabled. It is a SERVER-driven,
        # billing-affecting flag (default OFF) and update_from_server toggles it
        # per-session. Persisting it would let a build that shipped it default-ON
        # (v1.5.85-beta.*) pin enabled=true in config.json and override the safe
        # default on a later update — so the "ship inert" guarantee held only for
        # devices that never saved it. Drop it so the code default always wins on
        # load and only the server can switch it on.
        data.get("foreground_activity", {}).pop("enabled", None)
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
        # PRIVACY-EGRESS settings are deferred as a block behind
        # DEFER_UNAPPLIED_SERVER_SETTINGS. The /config envelope fix means server
        # config reaches the agent for the FIRST time on upgrade; without this
        # gate a device row carrying e.g. collect_full_urls=1 or
        # track_browser_domains=0 would silently start egressing full URLs the
        # moment this build lands — a privacy behaviour change in the opposite
        # direction from this release's intent. These stay off until the team
        # audits the device rows and flips the flag in a deliberate release.
        # (working_hours + operational sync tuning below are NOT gated — those
        # are the intended live behaviours.)
        if "privacy" in server_config and not DEFER_UNAPPLIED_SERVER_SETTINGS:
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

        if "collection" in server_config and not DEFER_UNAPPLIED_SERVER_SETTINGS:
            collection = server_config["collection"]
            if "collect_page_category" in collection:
                self.privacy.collect_page_category = self._to_bool(collection["collect_page_category"])
            if "auto_categorize" in collection:
                self.privacy.auto_categorize = self._to_bool(collection["auto_categorize"])
            if "track_display_info" in collection:
                self.privacy.track_display_info = self._to_bool(collection["track_display_info"])
            if "track_browser_urls" in collection:
                self.privacy.track_browser_urls = self._to_bool(collection["track_browser_urls"])
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

        if "tracking" in server_config and not DEFER_UNAPPLIED_SERVER_SETTINGS:
            tracking = server_config["tracking"]
            if "afk_timeout_minutes" in tracking:
                val = tracking["afk_timeout_minutes"]
                if val in (10, 20, 30):
                    self.aw.afk_timeout_minutes = val

        if "working_hours" in server_config:
            wh = server_config["working_hours"]
            # Build a fresh instance and swap it in only once it is fully parsed,
            # so a half-applied payload can never leave a live schedule in a state
            # that is neither the old one nor the new one. `known` is set LAST and
            # only on success: a schedule we failed to parse is a schedule we do
            # not know, and an unknown schedule records nothing (see .allows()).
            #
            # Do NOT widen this except to swallow AttributeError. That is what hid
            # the dict-vs-dataclass bug for the whole life of the feature — the
            # error fired on every sync, was logged as a shrug, and enforcement
            # stayed off. If the shape is wrong we want to know loudly.
            try:
                enforced = self._to_bool(wh.get("enforced", False))
                if enforced:
                    # Only a RESTRICTED schedule needs a window. Validators raise on
                    # anything malformed rather than widening the window to its
                    # defaults, so a backend typo can never reopen the night.
                    parsed = WorkingHoursConfig(
                        enforced=True,
                        work_start=_normalize_hhmm(wh.get("work_start")),
                        work_end=_normalize_hhmm(wh.get("work_end")),
                        working_days=_normalize_working_days(wh.get("working_days")),
                        timezone=str(wh.get("timezone", "") or ""),
                    )
                else:
                    # Unrestricted (B2B): 24/7, no window to validate.
                    parsed = WorkingHoursConfig(enforced=False)
                parsed.known = True
                self.working_hours = parsed
            except (TypeError, ValueError) as e:
                # Keep whatever we already knew (possibly a cached schedule from
                # disk). Never fall back to a permissive default.
                logger.error(
                    "Invalid working_hours from server (%s): %r — keeping previous "
                    "schedule (known=%s)",
                    e,
                    wh,
                    self.working_hours.known,
                )

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
            if "in_process_window" in sync:
                # Opt-in remote enable of the in-process window source (ships
                # dormant). Use _to_bool (not bool()) like every sibling flag: a
                # server payload of the STRING "false"/"0" must stay off —
                # bool("false") is True and would silently enable it fleet-wide.
                self.sync.in_process_window = self._to_bool(sync["in_process_window"])
                logger.info(
                    "Server config: in_process_window=%s", self.sync.in_process_window
                )
            if "in_process_input" in sync:
                # Opt-in remote enable of the in-process input source (ships
                # dormant). Use _to_bool (not bool()) like every sibling flag: a
                # server payload of the STRING "false"/"0" must stay off —
                # bool("false") is True and would silently enable it fleet-wide.
                self.sync.in_process_input = self._to_bool(sync["in_process_input"])
                logger.info(
                    "Server config: in_process_input=%s", self.sync.in_process_input
                )

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

        if "foreground_activity" in server_config:
            fa = server_config["foreground_activity"]
            try:
                if "enabled" in fa:
                    self.foreground_activity.enabled = self._to_bool(fa["enabled"])
                if "cpu_threshold_percent" in fa:
                    val = float(fa["cpu_threshold_percent"])
                    # Clamp to a sane single-core-basis range; 0 would credit any
                    # focused app, >100% is meaningless on a single-core basis.
                    if math.isfinite(val):
                        self.foreground_activity.cpu_threshold_percent = min(max(val, 1.0), 100.0)
                if "max_credit_minutes" in fa:
                    # Bound the farm window: never credit more than 2h past the
                    # last real input regardless of server value.
                    self.foreground_activity.max_credit_minutes = min(max(int(fa["max_credit_minutes"]), 1), 120)
                if "min_session_seconds" in fa:
                    self.foreground_activity.min_session_seconds = max(0, int(fa["min_session_seconds"]))
            except (TypeError, ValueError) as e:
                logger.warning(f"Invalid foreground_activity config from server: {e}")

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

    # encoding="utf-8" is REQUIRED, not cosmetic: without it the handler writes
    # in the platform locale encoding, which on Windows is cp1252. A cp1252 byte
    # like \x97 then breaks the remote log upload — the server's INSERT into the
    # utf8 agent_log_uploads.content column fails with MySQL 1366 "Incorrect
    # string value", so Windows logs silently never landed (Sachi, 2026-06-18).
    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    # Reduce noise from libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
