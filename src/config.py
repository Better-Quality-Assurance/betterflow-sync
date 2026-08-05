"""Configuration management for BetterFlow."""

import json
import logging
import math
import os
import re
import sys
import threading
import time
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

# Both spellings, as everywhere else in src/: relative under `python -m src.main`,
# absolute inside the PyInstaller bundle. hardware_serial imports only stdlib, so
# this cannot cycle back into config.
try:
    from .hardware_serial import get_hardware_serial
except ImportError:
    from hardware_serial import get_hardware_serial

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


# Namespace for deriving a machine UUID from a hardware serial. A fixed,
# project-specific namespace means the derivation is reproducible on this
# machine forever and cannot collide with a UUID5 minted by anything else.
_MACHINE_ID_NAMESPACE = uuid.UUID("6f9f4a1e-6a1f-5f2d-9c1b-3a0e7b5d8c42")


def get_machine_uuid() -> str:
    """Return a persistent UUID for this machine.

    On first call, reads from (or generates and writes to) a `.machine_id`
    file in the config directory. Subsequent calls return the in-memory
    cache. The UUID survives app updates and hostname changes.

    When the file is absent, the id is DERIVED from the hardware serial rather
    than randomly generated, so losing the file does not silently create a
    second identity for one laptop. The server keys a device as
    sha256(machine_id . platform) (AgentDevice::generateDeviceId), so a fresh
    random id there means a fresh device row: verified on 2026-08-04, two
    macOS users ended up with two ACTIVE, concurrently-syncing device rows
    each, which is the input shape behind cross-device hour double-counting.

    A machine with no readable serial (a VM, a container, a hardened Linux
    box) falls back to a random UUID — the previous behaviour, and the only
    honest one when the machine offers nothing stable to derive from.

    An existing file ALWAYS wins, including over the derived value. Every
    machine in the fleet already holds a random id, and preferring the derived
    one would re-register the entire fleet as new devices.

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

        # Derive from the hardware serial where possible so a lost file is
        # recoverable, else fall back to random (atomic write: tmp + rename).
        new_id = _derive_machine_uuid()
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


def _derive_machine_uuid() -> str:
    """A UUID for this machine that survives losing the config directory.

    Derived from the hardware serial the agent already reports for MDM asset
    correlation. Random when no serial is available — a shared constant would
    merge unrelated machines onto one device row, which is worse than the
    duplicate this exists to prevent.

    Never raises: this is on the launch path, and get_hardware_serial's promise
    not to raise belongs to another module.
    """
    try:
        serial = get_hardware_serial()
    except Exception as e:  # noqa: BLE001 — a serial is never worth a failed launch
        logger.warning("Hardware serial probe failed, using a random machine id: %s", e)
        serial = None

    if not serial:
        logger.info("No hardware serial available — machine id will be random")
        return str(uuid.uuid4())

    derived = str(uuid.uuid5(_MACHINE_ID_NAMESPACE, serial))
    # The serial itself is never logged: it is a device identifier, and this
    # log is uploaded on logs_requested.
    logger.info("Derived machine id from the hardware serial")
    return derived


@dataclass
class PrivacySettings:
    """Privacy configuration."""

    # `hash_titles` / `title_allowlist` were REMOVED (2026-07-23). They were declared
    # here and populated from the server, but no capture or transform code in src/ ever
    # read them — the agent has never hashed a window title. Keeping a dead field named
    # "hash_titles" misrepresented how titles are handled, in a settings surface offered
    # to employees. Title handling is a SERVER-side control (AgentDevice::
    # shouldStoreRawTitle in internal-tool2, driven by the device row); the agent sends
    # titles raw and has no say. Do not re-add these without a real client-side consumer.
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
    # keystrokes/clicks for hours (Fraud Risk 75). Windows defaults ON because
    # the external tracker is the known-bad path there; macOS stays opt-in under
    # its Input Monitoring grant. A server config value can still explicitly
    # switch it either way, and when on AND an in-process backend is usable, the
    # external input bucket is skipped so the two sources never double-count.
    in_process_input: bool = field(default_factory=lambda: sys.platform == "win32")


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
    # Hard ceiling on the AFK credit a single call can inject into the uploaded
    # AFK stream (a stuck call-matching window title must not keep the stream
    # not-afk forever). Generous by design: real all-day meetings exist, and the
    # cap only bites when there is ALSO zero keyboard/mouse input for its whole
    # length — any real input keeps the stream not-afk on its own.
    max_credit_minutes: int = 240
    # Microphone-in-use meeting detection (mic_activity.py): the system-level
    # signal that catches a meeting even when the call window isn't frontmost
    # (a background Slack huddle while reading docs). Conferencing-gated and
    # capped by max_credit_minutes; sessions upload as auditable call events.
    mic_signal: bool = True


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
# the agent for the FIRST TIME EVER — none of them have ever applied. One of those moves
# real-world behaviour and has nothing to do with working-hours enforcement, so it must
# land as its own deliberate change rather than as a side effect of a privacy fix:
#
#   tracking.afk_timeout_minutes  — 37 of 44 prod devices are set to 20 while every agent
#                                   has run the client default of 10. Applying it lengthens
#                                   the idle grace, i.e. it CHANGES BILLED HOURS.
#
# That is what the DB has always said and what someone intended; it has simply never
# taken effect. It needs its own release, with the affected people told first. Flip this
# to False to roll it out.
#
# An earlier revision of this comment also listed privacy.hash_window_titles here and
# claimed that applying it would "turn window titles into hashes". That was never true:
# no client-side title hashing has ever existed in src/, so the setting was inert whether
# deferred or not. Its local mirror was removed 2026-07-23 — see PrivacySettings and the
# ignore note in update_from_server.
#
# Because the /config envelope fix makes server config reach agents for the FIRST time
# ever (the whole fleet has run on local defaults), this gate now covers EVERY block that
# changes capture / billing / privacy behaviour, so this first delivery is behaviour-neutral
# except for the working-hours schedule: privacy, collection, engagement, fraud_detection,
# call_detection, and foreground_activity are all deferred. Only working_hours (the
# feature), benign sync tuning (interval, batch_size, idle_pause,
# min_window_event_seconds, in_process_window), and sync.in_process_input (un-deferred
# 2026-07-17: it is the shipped remediation for Windows' zero-input fraud false
# positives, stays opt-in per device, and counts carry no content — see its handler)
# go live. Roll the rest out deliberately, one block at a time, after confirming the
# device rows and telling the affected people.
#
# working_hours is deliberately NOT gated by this: it is the whole point of the release.
DEFER_UNAPPLIED_SERVER_SETTINGS = True


@lru_cache(maxsize=8)
def _resolve_zone(name: str):
    """Resolve an IANA timezone name to a ZoneInfo, or None — QUIET (no logging).

    The one place a name becomes a zone. Callers decide whether a miss is
    noteworthy: a bad SCHEDULE anchor is (``_resolve_schedule_tz`` logs it); a
    machine that only has a "+03:00" offset string is expected on a tz-database-less
    host and must NOT log a schedule error (``_localize`` uses this directly).
    lru_cache keeps ZoneInfo construction off the per-event/per-minute path.
    """
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — unresolvable/offset-string/missing tzdata
        return None


@lru_cache(maxsize=8)
def _resolve_schedule_tz(name: str):
    """Resolve a SCHEDULE anchor timezone, logging an unresolvable one ONCE.

    A set-but-unresolvable anchor is a real config problem (missing tz database,
    backend typo), so it is worth a one-time ERROR — unlike the machine's own tz,
    which falls back silently. lru_cache makes the log line run once per name.
    """
    zone = _resolve_zone(name)
    if name and zone is None:
        logger.error(
            "Working-hours anchor timezone %r could not be resolved (missing tz "
            "database?) — the window is judged in machine-local time instead",
            name,
        )
    return zone


def _detect_machine_timezone_uncached() -> str:
    """Actual OS timezone detection — see detect_machine_timezone for the cache."""
    # macOS/Linux: /etc/localtime is a symlink into the zoneinfo database.
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/")[1]
    except (OSError, IndexError):
        pass

    # Windows: tzlocal maps the registry zone to an IANA name.
    try:
        from tzlocal import get_localzone

        return str(get_localzone())
    except Exception:  # noqa: BLE001 — ImportError, or a tzlocal resolution failure
        # Not fatal: the offset fallback below still yields a usable value; a debug
        # line keeps this from being a fully silent swallow without flooding logs.
        logger.debug("tzlocal unavailable/failed; using UTC-offset fallback for tz")

    # Last resort: a fixed offset like "+03:00". Not an IANA name (so _resolve_zone
    # returns None and _localize falls back to the raw machine clock), but it is what
    # the heartbeat reports upstream.
    offset = datetime.now(timezone.utc).astimezone().strftime("%z")  # "+0300"
    return f"{offset[:3]}:{offset[3:]}" if offset else "UTC"


_MACHINE_TZ_CACHE_SECONDS = 60.0


@lru_cache(maxsize=2)
def _detect_machine_timezone_bucketed(_bucket: int) -> str:
    """Cached by a coarse monotonic time bucket so detect_machine_timezone serves a
    single readlink/tzlocal result for the whole bucket. maxsize=2 keeps the current
    and previous bucket; lru_cache is internally locked, so this is thread-safe."""
    return _detect_machine_timezone_uncached()


def detect_machine_timezone() -> str:
    """The device's own local IANA timezone name, falling back to a UTC-offset
    string. The SINGLE source of truth for "what timezone is this machine in" for
    WINDOW EVALUATION and the HEARTBEAT report — so ``timezone_mismatch`` compares
    the schedule anchor against the exact zone the agent reports, with no second
    implementation to drift from it (BetterFlowClient._detect_timezone delegates
    here). NB: other machine-local reasoning (date bucketing in sync_engine /
    activity_analyzer) still uses raw ``.astimezone()``; this is not a global
    machine-tz authority, only for the window+heartbeat.

    Cached for ~``_MACHINE_TZ_CACHE_SECONDS`` because ``_localize`` calls it on every
    ``allows()`` — i.e. once per event in the upload gate and up to thousands of
    times in ``next_boundary_after``'s minute walk. A live readlink (and, on Windows,
    a tzlocal registry lookup) on each of those was a needless syscall storm. The
    staleness ceiling is harmless: enforcement re-evaluates every 60s anyway, so a
    corrected OS clock still self-heals within a tick.
    """
    return _detect_machine_timezone_bucketed(int(time.monotonic() // _MACHINE_TZ_CACHE_SECONDS))


@lru_cache(maxsize=8)
def _warn_timezone_drift_once(anchor: str, machine: str) -> None:
    """One WARNING per distinct (anchor, machine) pair — mirrors the once-logging of
    _resolve_schedule_tz, since timezone_mismatch is polled every heartbeat."""
    logger.warning(
        "Working-hours anchor timezone %r differs from this device's timezone %r — "
        "evaluating the window in the device's local time (self-healing) and "
        "flagging the drift on the heartbeat so the schedule can be re-anchored.",
        anchor,
        machine,
    )


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

    def _localize(self, when: "datetime") -> "datetime":
        """Convert a UTC instant to the wall clock the window is evaluated on — the
        ONE place every consumer (allows / window_close_after / next_boundary_after)
        gets its local time, so they can never disagree.

        The device's own local timezone is AUTHORITATIVE. It is resolved live on
        every call (``detect_machine_timezone``) so that correcting a machine whose
        zone was wrong takes effect immediately — the failure that silently zeroed
        tracking was a schedule anchored to a stale/wrong reported zone that then
        stuck. ``self.timezone`` (the server anchor) is deliberately NOT consulted
        here; it survives only for drift DETECTION (``timezone_mismatch``).

        Accepted trade-off (2026-07-22): the window now follows a clock the employee
        can change, so someone could set their OS timezone to sit permanently
        outside the window and avoid recording. That divergence is reported on the
        heartbeat (``timezone_mismatch``) so the fleet can catch it — a signal the
        previous silent-anchor behaviour never emitted, while it mis-tracked honest
        misconfigurations far more often than it stopped abuse.

        A detected value that is not a resolvable IANA name (the "+03:00" offset
        fallback on a machine without a tz database) yields ``None`` here, and
        ``when.astimezone()`` falls back to the raw system clock — the same last
        resort as before. Resolved QUIETLY (via ``_resolve_zone``, not
        ``_resolve_schedule_tz``): an offset-only machine is expected on a
        tz-database-less host and must not log a schedule-anchor error.
        """
        zone = _resolve_zone(detect_machine_timezone())
        return when.astimezone(zone) if zone else when.astimezone()

    def timezone_mismatch(self) -> "Optional[str]":
        """The schedule's anchored timezone when it currently evaluates the window at
        a DIFFERENT UTC offset than this machine's own timezone — i.e. the anchor has
        drifted from reality and the fleet should re-anchor it. Returns ``None`` when
        there is nothing to report: schedule unknown or unrestricted, no anchor set
        (machine-local is already the only clock), an unresolvable anchor (already
        falls back to machine-local), or the two offsets agree right now.

        Compares live UTC OFFSETS, not zone names, so equal-offset aliases
        (``UTC``/``Etc/UTC``) and two zones that happen to share an offset today do
        not false-positive, while a real gap (Los_Angeles vs Bucharest) does. Offsets
        are DST-dependent, hence evaluated at the current instant.

        Deliberately not a pure predicate: on a genuine drift it also emits a
        once-per-(anchor, machine) WARNING via ``_warn_timezone_drift_once``. This is
        the one place that both KNOWS a drift just occurred and holds the once-guard,
        so "detect and announce the drift" is treated as one responsibility rather
        than duplicating the guard in the sole caller (``_build_health_telemetry``).
        All detection here is cache-served (see ``detect_machine_timezone``).
        """
        if not (self.known and self.enforced) or not self.timezone:
            return None
        anchor = _resolve_schedule_tz(self.timezone)
        if anchor is None:
            return None
        now = datetime.now(timezone.utc)
        # Machine offset via the SAME path _localize uses (detect_machine_timezone),
        # NOT the raw system clock — otherwise a mocked/overridden machine zone would
        # be compared against the real OS clock and disagree with what allows() does.
        if now.astimezone(anchor).utcoffset() == self._localize(now).utcoffset():
            return None
        _warn_timezone_drift_once(self.timezone, detect_machine_timezone())
        return self.timezone

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

        local = self._localize(when)
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

        local = self._localize(start)
        end_h, end_m = (int(p) for p in self.work_end.split(":"))
        # NB: this clamps to work_end:00 exactly, which is ONE MINUTE EARLIER than
        # the instant allows() stops permitting recording. allows() compares HH:MM
        # strings inclusively (`hhmm <= work_end`), so it stays True through
        # work_end:59 and only flips False at work_end+1 min — and
        # next_boundary_after() mirrors that inclusive edge. window_close_after()
        # deliberately does NOT mirror it: clamping a span slightly earlier can only
        # discard in-window data, never leak the minute after close, so the
        # one-minute disagreement is the safe direction and intentional.
        close = local.replace(hour=end_h, minute=end_m, second=0, microsecond=0)

        # For an overnight window the close belongs to the NEXT local day whenever
        # `start` is in the evening half.
        if self.work_start > self.work_end and local.strftime("%H:%M") >= self.work_start:
            close += timedelta(days=1)

        return close.astimezone(timezone.utc)

    def next_boundary_after(self, when: "datetime") -> "Optional[datetime]":
        """The next UTC instant strictly after ``when`` at which ``allows()`` flips.

        Used to arm a one-shot capture stop/start EXACTLY at the window edge rather
        than waiting up to a full 60s enforcement tick — sync() already stops
        uploading at the boundary via a live now() read, but local recording had a
        tail of up to a tick past e.g. 22:00.

        Returns None when the schedule has no boundaries — an unknown schedule
        (``allows`` is always False) or an unrestricted one (always True). In both
        cases there is nothing for a one-shot trigger to align to.

        Computed by walking allows() at minute resolution (its own granularity —
        work_start/work_end are HH:MM and seconds are ignored), so the boundary is
        by construction consistent with the suppression and upload gates that read
        the same allows(). Capped at 8 days so a degenerate schedule (e.g. a
        hand-edited empty working_days, where allows() is always False) yields None
        instead of looping.
        """
        if not self.known or not self.enforced:
            return None

        current = self.allows(when)
        # Boundaries land on HH:MM:00 in the schedule's zone; stepping in UTC by
        # whole minutes from the next minute still lands on every local flip
        # (including across a DST change, which only shifts the wall-clock offset).
        probe = when.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = probe + timedelta(days=8)
        while probe <= limit:
            if self.allows(probe) != current:
                return probe
            probe += timedelta(minutes=1)
        return None


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
    # Record of the one-time privacy notice (src/privacy_notice.py). The version
    # is a hash of the notice text, so a device holding an OLDER version is
    # re-shown the notice — that comparison is the whole point, and it is why
    # this stores the version rather than a bare `privacy_notice_seen: bool`.
    # Purely local + reported on the heartbeat; the server never writes it back
    # (update_from_server touches neither field), so a config push cannot forge
    # an acknowledgement.
    privacy_notice_ack_version: Optional[str] = None
    privacy_notice_ack_at: Optional[str] = None  # UTC ISO 8601
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

        def _block(key: str) -> dict:
            """Pop a nested settings block as a defensive COPY.

            Two traps, closed for every block rather than the one that happened
            to need it:

            (a) `data = dict(data)` above is a TOP-LEVEL copy only, so popping a
            key out of a nested block reaches through and mutates the CALLER's
            dict — which the drops below (foreground_activity.enabled,
            sync.in_process_input) do exactly.

            (b) A hand-edited or corrupt block that is not a mapping (null, a
            list, a string) used to reach `_safe()` as a non-mapping and raise.
            `_from_dict` runs inside load()'s `except`, so ONE bad block silently
            discarded the WHOLE config — api_url, working_hours, engagement, the
            lot — and fell back to full defaults. Anything that is not a mapping
            now degrades to {}, i.e. that block's defaults, and leaves the rest
            of the file intact.
            """
            value = data.pop(key, None)
            if isinstance(value, dict):
                return dict(value)
            if value is not None:
                # Log it: degrading to defaults silently is how a partially
                # reverted config looks identical to a healthy one.
                logger.warning(
                    "Config block %r is %s, not an object — using that block's "
                    "defaults and keeping the rest of the file",
                    key, type(value).__name__,
                )
            return {}

        aw_data = _block("aw")
        sync_data = _block("sync")
        privacy_data = _block("privacy")
        reminders_data = _block("reminders")
        engagement_data = _block("engagement")
        fraud_detection_data = _block("fraud_detection")
        call_detection_data = _block("call_detection")
        foreground_activity_data = _block("foreground_activity")
        # working_hours was missing from this list until 2026-07-14, so it fell
        # through to the **data splat below and was rebuilt as a plain dict. A
        # dict has no attributes: update_from_server's `self.working_hours.
        # enforced = ...` then raised AttributeError (swallowed as "invalid
        # working_hours, ignoring") and _within_working_hours' `getattr(wh,
        # "enforced", False)` read False. Net effect: on EVERY device that had
        # ever written a config.json, working-hours enforcement was silently and
        # permanently off, and restricted users were recorded around the clock.
        working_hours_data = _block("working_hours")
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
        # Same trap, opposite direction: in_process_input is PLATFORM-defaulted
        # (True on Windows, where there is no external input tracker at all) and
        # server-overridable. update_from_server ends with save(), so every agent
        # that ever fetched config has "in_process_input": false on disk from the
        # builds where it shipped dormant fleet-wide — and honouring that would
        # pin the Windows default OFF on upgrade, leaving exactly the devices
        # this exists for reporting zero keystrokes forever. Only a FRESH install
        # would have gotten the fix. Drop it so the platform default wins on
        # load; the server still switches it either way per session.
        persisted_inproc_input = sync_data.pop("in_process_input", None)
        if persisted_inproc_input is not None:
            logger.info(
                "Ignoring persisted sync.in_process_input=%s on load; it is "
                "platform-defaulted and server-driven",
                persisted_inproc_input,
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
        # Never persist sync.in_process_input either — same reason, and this is
        # the write side of the _from_dict drop above. It is platform-defaulted
        # (Windows True: no external input tracker ships there) and server-
        # overridable, so a value on disk can only ever outrank both and pin a
        # Windows device at zero keystrokes across an upgrade.
        data.get("sync", {}).pop("in_process_input", None)
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
            privacy.exclude_apps -> EXTENDS local exclude_apps (union, never replace)
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
            # DELIBERATELY IGNORED: privacy.hash_window_titles and
            # privacy.title_allowlist. The server still sends both (they are real
            # columns on the agent_devices row and AgentConfigController emits
            # them), but the agent has no client-side title hashing and never had
            # any — the values were stored in Config and read by nothing. They are
            # dropped here rather than mirrored, so that no future reader mistakes
            # a populated field for an enforced control. Title handling is enforced
            # SERVER-side (AgentDevice::shouldStoreRawTitle). Do not "restore" these.
            if "track_browser_domains" in privacy:
                # Server tracks domains = we extract domain only
                self.privacy.domain_only_urls = self._to_bool(privacy["track_browser_domains"])
            if "collect_full_urls" in privacy:
                self.privacy.collect_full_urls = self._to_bool(privacy["collect_full_urls"])
            if "exclude_apps" in privacy:
                # ADDITIVE ONLY — union with what this build ships, never a
                # replacement. The Regulament Intern states the excluded-app list
                # is not limitative and may be extended; this is the mechanism.
                # Making it a replacement would let one server row REMOVE
                # 1Password/Keychain from the list, i.e. turn a signed privacy
                # guarantee off remotely with no release and nobody informed —
                # exactly the failure mode the deferral gate above exists for.
                # Union can only ever send LESS data, so the worst a bad payload
                # can do is stop tracking an app, which is visible and harmless.
                extra = privacy["exclude_apps"]
                if isinstance(extra, list):
                    added = [
                        a.strip() for a in extra
                        if isinstance(a, str) and a.strip()
                        and a.strip() not in self.privacy.exclude_apps
                    ]
                    if added:
                        self.privacy.exclude_apps = self.privacy.exclude_apps + added
                        logger.info(
                            "Server config: extended exclude_apps with %s", added
                        )
                else:
                    logger.warning(
                        "Invalid privacy.exclude_apps from server (expected a "
                        "list, got %s) — ignoring", type(extra).__name__
                    )

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
                # dormant). UN-DEFERRED deliberately (2026-07-17): Windows
                # devices have NO working external input watcher (the bundle
                # launches only window+idle trackers, and where aw-watcher-input
                # does run its low-level hook gets blocked by UIPI/AV), so every
                # Windows agent reports zero keystrokes/clicks all month and the
                # fraud engine flags every worked day as suspicious (Sachi 23,
                # Claudia 26 false suspicious days, Fraud Risk 95). This flag IS
                # the shipped remediation, and it stays opt-in per device: the
                # server must still send in_process_input=true explicitly, so
                # the deliberate one-device-at-a-time rollout is preserved — the
                # deferral gate only made the remediation unreachable. The
                # original deferral concern (a stale device row silently
                # activating input capture) is acceptable here: input COUNTS
                # (presses/clicks/scrolls per window) carry no content, unlike
                # the still-deferred privacy/title flags. Use _to_bool (not
                # bool()) like every sibling flag: a server payload of the
                # STRING "false"/"0" must stay off — bool("false") is True and
                # would silently enable it.
                self.sync.in_process_input = self._to_bool(sync["in_process_input"])
                logger.info(
                    "Server config: in_process_input=%s", self.sync.in_process_input
                )

        if "engagement" in server_config and not DEFER_UNAPPLIED_SERVER_SETTINGS:
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

        if "fraud_detection" in server_config and not DEFER_UNAPPLIED_SERVER_SETTINGS:
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

        if "call_detection" in server_config and DEFER_UNAPPLIED_SERVER_SETTINGS:
            cd = server_config["call_detection"]
            # Disable-only exemption from the deferral gate: mic_signal=false
            # is the PRIVACY KILL SWITCH for the mic probe, and a remote
            # off-switch must work without waiting for the staged rollout (or
            # an app release). Enabling stays deferred like everything else —
            # only "off" passes through.
            if "mic_signal" in cd and not self._to_bool(cd["mic_signal"]):
                self.call_detection.mic_signal = False
                logger.info("Server disabled mic_signal (deferral-exempt kill switch)")

        if "call_detection" in server_config and not DEFER_UNAPPLIED_SERVER_SETTINGS:
            cd = server_config["call_detection"]
            if "enabled" in cd:
                self.call_detection.enabled = self._to_bool(cd["enabled"])
            if "min_call_duration" in cd:
                try:
                    # Upper clamp: a huge server value would suppress every
                    # call/mic EVENT while short-session AFK credit still
                    # flows — credited time with no auditable span. 10 min is
                    # far above any sane "skip accidental opens" threshold.
                    self.call_detection.min_call_duration = min(
                        max(0, int(cd["min_call_duration"])), 600
                    )
                except (TypeError, ValueError):
                    logger.warning("Invalid min_call_duration from server, ignoring")
            if "max_credit_minutes" in cd:
                try:
                    # Bound the farm window: a call injects AFK credit into the
                    # billed stream, so never let a server value push the cap
                    # past 8h (nor below 1min) regardless of what it sends.
                    self.call_detection.max_credit_minutes = min(
                        max(int(cd["max_credit_minutes"]), 1), 480
                    )
                except (TypeError, ValueError):
                    logger.warning("Invalid call max_credit_minutes from server, ignoring")
            if "mic_signal" in cd:
                self.call_detection.mic_signal = self._to_bool(cd["mic_signal"])

        if "foreground_activity" in server_config and not DEFER_UNAPPLIED_SERVER_SETTINGS:
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


# Agent log files are disclosed to employees as retained for 30 days
# (Regulament Intern art. 68^1 alin. 8 lit. f). The logs carry app names, the
# machine hostname, and OS usernames inside stack-trace paths, so this is a
# privacy CEILING, not a floor: nothing older than the window may remain.
# Changing this number changes what a signed document promises — tell
# dpo@betterqa.co. Pinned by tests/test_log_retention.py.
LOG_RETENTION_DAYS = 30


def prune_old_logs(
    log_dir: "Path", *, now: Optional[float] = None, max_age_days: int = LOG_RETENTION_DAYS
) -> "list[Path]":
    """Delete agent log files whose last write predates the retention window.

    Size-based rotation bounds disk use but gives no time guarantee — a quiet
    machine keeps a rotated file for months. This is the time bound: on every
    startup, remove any ``betterflow.log`` or ``betterflow.log.N`` whose mtime
    is older than ``max_age_days``. mtime is the last write, so an active file
    older than the window has ALL its lines older than the window and is safe to
    drop; it is recreated fresh by the handler.

    Scoped to our own files by name, so an unrelated ``.log`` in the same
    directory is never touched. Never raises: retention must not break startup,
    and one unreadable file must not stop the rest being pruned (a sweep that
    aborts on the first error silently keeps everything behind it). Returns the
    files it removed.
    """
    from pathlib import Path

    now = time.time() if now is None else now
    cutoff = now - max_age_days * 86400
    removed: list[Path] = []

    try:
        candidates = list(Path(log_dir).glob("betterflow.log*"))
    except OSError:
        return removed

    for path in candidates:
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            removed.append(path)
        except OSError:
            # Locked, vanished, or permission-denied: skip it, keep sweeping.
            logger.debug("Log retention could not remove %s", path, exc_info=True)

    return removed


def setup_logging(debug: bool = False) -> None:
    """Configure logging.

    Safe to call multiple times (e.g. when toggling debug mode at runtime).
    """
    log_dir = Config.get_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    # Enforce the disclosed 30-day retention ceiling before opening the handler.
    # Done here rather than on a timer so it runs on every launch regardless of
    # how long the machine was off — the case size-based rotation misses.
    prune_old_logs(log_dir)

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
