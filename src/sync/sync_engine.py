"""Sync engine - orchestrates data flow from ActivityWatch to BetterFlow."""

import logging
import math
import re
import socket
import threading
import time
from collections import OrderedDict
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import urlparse

try:
    from ..__init__ import __version__ as AGENT_VERSION
except ImportError:
    try:
        from __init__ import __version__ as AGENT_VERSION
    except ImportError:
        AGENT_VERSION = "0.0.0"

try:
    from ..browser_tracker import is_browser_app
    from ..config import Config
    from .aw_client import AWClientError, AWEvent, BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT, BUCKET_TYPE_AFK, BUCKET_TYPE_AFK_ALT, BUCKET_TYPE_WEB, BUCKET_TYPE_INPUT, BUCKET_TYPE_CALL, CALL_STATUS_ONGOING, CALL_STATUS_COMPLETED
    from .bf_client import BetterFlowClientError, BetterFlowAuthError
    from .protocols import AWClientProtocol, BFClientProtocol, OfflineQueueProtocol
    from .activity_analyzer import ActivityAnalyzer, EngagementThresholds
    from .daily_time_tracker import DailyTimeTracker
    from .call_detector import CallDetector, CallEvent
    from .foreground_activity import ForegroundActivityDetector, create_detector
    from .mic_activity import MicActivityDetector, create_mic_detector
    from .os_idle import get_system_idle_seconds
    from .queue import EVENT_RETENTION_DAYS, is_event_storable, normalized_project_id
except ImportError:
    from browser_tracker import is_browser_app
    from config import Config
    from sync.aw_client import AWClientError, AWEvent, BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT, BUCKET_TYPE_AFK, BUCKET_TYPE_AFK_ALT, BUCKET_TYPE_WEB, BUCKET_TYPE_INPUT, BUCKET_TYPE_CALL, CALL_STATUS_ONGOING, CALL_STATUS_COMPLETED
    from sync.bf_client import BetterFlowClientError, BetterFlowAuthError
    from sync.protocols import AWClientProtocol, BFClientProtocol, OfflineQueueProtocol
    from sync.activity_analyzer import ActivityAnalyzer, EngagementThresholds
    from sync.daily_time_tracker import DailyTimeTracker
    from sync.call_detector import CallDetector, CallEvent
    from sync.foreground_activity import ForegroundActivityDetector, create_detector
    from sync.mic_activity import MicActivityDetector, create_mic_detector
    from sync.os_idle import get_system_idle_seconds
    from sync.queue import EVENT_RETENTION_DAYS, is_event_storable, normalized_project_id

logger = logging.getLogger(__name__)

# A definitive rejection is formatted by http_client as ``API error (NNN): <body>``.
# Anchoring on that PREFIX rather than searching for any 3-digit run is what makes
# this a PROVENANCE test instead of a shape test: a free-text search reports a digit
# run from a rejected filename ("Bug 404 fix.txt" -> "server status 404") or from our
# OWN reason strings ("shed after 137 transient failures" -> "server status 137", on
# a string that says it is not a rejection). Both were live before this anchor.
_SERVER_STATUS_RE = re.compile(r"^API error \((\d{3})\)")


def server_status_summary(reasons) -> str:
    """Reduce server rejection text to bare HTTP status codes for the OPS ingest.

    ONE implementation, shared by SyncEngine's drop report and
    BetterFlowApp._note_sync_failure, because they answer the same question about
    the same strings and two spellings of that rule would drift.

    Only digits matched by ``_SERVER_STATUS_RE`` are ever emitted, so no
    server-authored text can leave the device by this path. That matters because
    the ops ingest is CROSS-TENANT and a validation error routinely echoes the
    value it rejected — and our payloads carry window titles. The full text stays
    in betterflow.log, which is uploaded only on explicit admin request.
    """
    if isinstance(reasons, str):
        reasons = [reasons]
    items = [str(r) for r in (reasons or []) if r]
    if not items:
        return ""
    codes = sorted({m.group(1) for m in
                    (_SERVER_STATUS_RE.match(r) for r in items) if m})
    if codes and len(codes) == len(items):
        return f"; server status {','.join(codes)}, full reason in local dead-letter"
    if codes:
        return (f"; server status {','.join(codes)} plus "
                f"{len(items) - len(codes)} local reason(s), "
                "full detail in local dead-letter")
    return f"; {len(items)} local reason(s) recorded in local dead-letter"


# Sentinel for "no project id has been rejected yet" — distinct from None,
# which is itself a rejectable value (a project dict with no "id").
_NO_REJECTED_PROJECT_ID = object()


#: Reasons that mark the START of an excluded window. Only these flush the
#: pre-window tail before advancing checkpoints — on the LEAVE side `now` is the
#: end of the window, so flushing there would upload the span being excluded.
_FLUSH_TAIL_ON_ENTER = frozenset({"pause", "private_time"})


def _is_window_like(bucket_type: str) -> bool:
    """Window/web buckets whose events carry per-event activity + time tracking.

    Single source of truth so a new window-class bucket type is a one-line edit
    here rather than a scattered tuple-membership change. NOTE: this includes
    WEB; the activity-analyzer's window-change feed deliberately excludes WEB and
    keeps its own (WINDOW, WINDOW_ALT) check.
    """
    return bucket_type in (BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT, BUCKET_TYPE_WEB)


def _is_afk_like(bucket_type: str) -> bool:
    """AFK/idle buckets that drive the active-vs-idle decision."""
    return bucket_type in (BUCKET_TYPE_AFK, BUCKET_TYPE_AFK_ALT)


def _is_input_like(bucket_type: str) -> bool:
    """Input buckets (keystroke/click/scroll counts, fraud detection). Single
    source of truth so suppressing the external aw-watcher-input bucket when the
    in-process source is active is a one-line membership check."""
    return bucket_type == BUCKET_TYPE_INPUT


# Seconds the latest AFK event may lag "now" before the tracker is presumed
# frozen. A healthy bf-idle-tracker heartbeats its current event continuously,
# so its end-time tracks now; a larger gap means it hung/went blind and its last
# event must not be trusted as the live idle state. Mirrors IdleManager's
# _afk_staleness_grace and aw_manager's STALE_THRESHOLD (all 120s).
_AFK_STALENESS_GRACE = 120.0


@dataclass
class _SyncCycleContext:
    """Per-cycle activity context, threaded explicitly through the transform
    path instead of living as SyncEngine instance state.

    ``has_input_data`` is set once per cycle (in ``_prepare_input_analysis``,
    which returns the context); ``afk_events`` is refreshed per window bucket
    by ``_sync_window_buckets``. ``_reconcile_backlog`` builds its own fresh
    instance. Because it is a stack-local confined to the ``sync()`` /
    reconcile call chains (never an instance field), there is no cross-cycle
    leakage and no shared-state locking concern.
    """

    has_input_data: bool = False
    afk_events: list = field(default_factory=list)
    # Most recent real keyboard/mouse input observed in the input buckets this
    # cycle (end-time of the latest input event). The cross-platform
    # human-presence anchor for foreground-activity credit — works on Linux,
    # where the OS idle clock is unreadable.
    last_input_at: Optional[datetime] = None


@dataclass
class SyncStats:
    """Statistics from a sync cycle."""

    events_fetched: int = 0
    events_filtered: int = 0
    events_sent: int = 0
    events_queued: int = 0
    buckets_synced: int = 0
    gaps_filled: int = 0
    calls_detected: int = 0
    dev_sessions_detected: int = 0
    # Window-filter diagnostics: separate "the watcher produced nothing"
    # (v1.5.83 logs that watcher-side) from "the watcher produced window events
    # but the privacy filter dropped them all" (this side). window_seen = window
    # events read from AW this cycle; window_sent = those that survived filtering;
    # the drop sets/counter name WHY (which excluded app, or how many sub-minimum
    # flickers) so the next stall classifies itself (Cristian Dragota, 2026-06-25).
    window_seen: int = 0
    window_sent: int = 0
    window_drop_excluded_apps: set = field(default_factory=set)
    window_drop_short: int = 0
    errors: list[str] = field(default_factory=list)
    queued_bucket_ids: set = field(default_factory=set)
    _should_heartbeat: bool = False
    # True when AW answered is_running() (/info) but the bucket fetch (/buckets/)
    # failed — a half-hung bf-data-service. The coordinator escalates this to a
    # force_restart, which is_running() alone can't trigger.
    aw_bucket_fetch_failed: bool = False
    # True when this cycle read nothing because capture is suppressed (outside the
    # user's working hours, or their schedule isn't known yet). Distinguishes "we
    # are deliberately not recording" from "the tracker broke" — without it the
    # nightly silence looks identical to an outage.
    capture_suppressed: bool = False

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


MAX_APP_LENGTH = 256
MAX_TITLE_LENGTH = 1024
MAX_URL_LENGTH = 2048


class BoundedLRU:
    """Ordered dict with hard-capped size.

    Writes move the key to the end (most-recent). When the size exceeds
    ``maxsize`` the oldest entry is evicted. This replaces three nearly
    identical hand-rolled caches (_sent_cache, _gap_filled_originals,
    _time_cache) that each duplicated the same eviction logic.

    Not thread-safe on its own — callers must wrap operations in a lock
    when shared across threads, exactly as the original caches did.

    Does NOT inherit from dict — callers must use the explicit API below.
    """

    def __init__(self, maxsize: int) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self._maxsize = maxsize
        self._od: OrderedDict = OrderedDict()

    def get(self, key, default=None):
        if key in self._od:
            self._od.move_to_end(key)
            return self._od[key]
        return default

    def __contains__(self, key) -> bool:
        return key in self._od

    def __getitem__(self, key):
        self._od.move_to_end(key)
        return self._od[key]

    def __setitem__(self, key, value) -> None:
        self._od[key] = value
        self._od.move_to_end(key)
        if len(self._od) > self._maxsize:
            self._od.popitem(last=False)

    def __len__(self) -> int:
        return len(self._od)

    def clear(self) -> None:
        self._od.clear()


class SyncEngine:
    """Core sync engine that orchestrates AW -> BetterFlow data flow."""

    # Backlog paging: fetch a bounded forward time-window per cycle and take the
    # OLDEST batch, so a reconcile can walk through a stranded mid-day gap
    # instead of the newest-first fetch snapping the checkpoint back to now.
    # 2h holds well under _BACKLOG_FETCH_LIMIT events at realistic rates
    # (~300 input/h), so the slice keeps the true oldest events.
    _BACKLOG_WINDOW = timedelta(hours=2)
    _BACKLOG_FETCH_LIMIT = 1000
    # How many consecutive EMPTY backlog windows one cycle may skip. Each probe
    # is a cheap local AW query, so a weekend/overnight gap is crossed inside a
    # single cycle (48 x ~2h ~= 4 days) instead of one window per 60s cycle —
    # which left the dashboard graph empty for ~30 min every Monday morning
    # (device 14, 2026-08-03). Bounded so a months-parked device probes at most
    # ~4 days of history per cycle; the checkpoint persists each skip, so the
    # walk resumes where it left off.
    _BACKLOG_MAX_EMPTY_WINDOWS_PER_CYCLE = 48
    # Wall-clock budget for that walk, per bucket. The iteration cap alone does
    # not bound TIME: a degraded-but-answering local AW server can take up to
    # the client's 10s timeout per probe without raising, and 48 probes x 4
    # backlogged buckets would blow the 150s sync watchdog deadline (false
    # "Sync hung" ERROR) and even the 420s wedge ceiling (concurrent cycles).
    # Healthy probes run ~5ms, so this never triggers in the normal case; when
    # it does, progress is already persisted and the next cycle resumes.
    _BACKLOG_WALK_BUDGET_SECONDS = 5.0

    def __init__(
        self,
        aw: AWClientProtocol,
        bf: BFClientProtocol,
        queue: OfflineQueueProtocol,
        config: Config,
        on_config_updated: Optional[Callable] = None,
        display_tracker=None,
        activity_analyzer: Optional[ActivityAnalyzer] = None,
        time_tracker: Optional[DailyTimeTracker] = None,
        browser_tracker=None,
    ):
        self.aw = aw
        self.bf = bf
        self.queue = queue
        self.config = config
        self._on_config_updated = on_config_updated
        self._display_tracker = display_tracker
        self._browser_tracker = browser_tracker
        self._paused = False
        # "cannot upload right now", NOT "must not record". Distinct from
        # _paused on purpose: _paused discards the window, this one keeps it.
        # See suspend_upload().
        self._upload_suspended = False
        self._private_mode = False
        self._private_start: Optional[datetime] = None
        self._current_project: Optional[dict] = None
        # Last project id normalization rejected, so the drop is logged once per
        # distinct value instead of once per stamped event (or — worse — never).
        self._rejected_project_id: object = _NO_REJECTED_PROJECT_ID
        self._session_active = False
        self._config_fetched = False
        self._last_config_fetch_monotonic: float = 0.0
        self._heartbeat_count = 0
        # Send heartbeat every 5 sync cycles (5 * 60s = 5 min default)
        self._heartbeat_interval = 5
        # Monotonic time of the last heartbeat ATTEMPT (any path — the
        # sync-cadence heartbeat and the 60s-tick floor both funnel through
        # _send_heartbeat). The main loop's heartbeat floor reads its age to
        # reach idle/paused devices whose sync-cadence heartbeat has gone
        # dormant. None until the first heartbeat this process.
        self._last_heartbeat_monotonic: Optional[float] = None
        # Non-blocking guard so the send + command-processing body of
        # _send_heartbeat runs on only one thread at a time. The sync-cadence
        # path (_do_sync) and the 60s-tick heartbeat floor can both reach it,
        # and on an idle device both are live with harmonic periods — see
        # _send_heartbeat for why a second concurrent caller must no-op.
        self._heartbeat_inflight = threading.Lock()
        # Optional callable returning agent-health telemetry to ride along with
        # each heartbeat (set by the SyncCoordinator, which owns the AwManager
        # and the sync-failure counter). Returning None / raising is tolerated.
        self.health_provider: Optional[Callable[[], dict]] = None
        # Optional error reporter (set by the SyncCoordinator) so a FAILED
        # logs_requested upload is visible remotely. The whole point of the
        # remote log fetch is to diagnose a sick agent — but if the upload
        # itself fails (unreadable log, POST error) the only record is the local
        # log we can't fetch. Reporting the failure here (a separate ops-ingest
        # channel) breaks that circular blindness. This is exactly why Windows
        # wedges were undiagnosable. None / errors tolerated.
        self.error_reporter = None
        # Optional callback (set by BetterFlowApp → UpdateHandler) invoked with
        # the server's advertised minimum version when this agent is below it.
        # It stages the latest build and applies on next idle, so an urgent fix
        # reaches the fleet in minutes instead of waiting for the 6h periodic
        # check. None / errors tolerated — never let it break the heartbeat.
        self.on_update_required: Optional[Callable[[str], None]] = None
        # Optional in-process AFK source (set by the SyncCoordinator). When
        # present, enabled by config, and the OS idle clock is readable, the
        # agent uploads its own AFK stream and ignores the external bf-idle-tracker
        # bucket. The checkpoint is the last instant covered by an upload; it is
        # initialized to `now` on the first cycle (account only while running) and
        # is NEVER touched by the backlog reconcile (no AW bucket to re-fetch; the
        # sample log only retains ~2h, so a day-start rewind would re-emit afk over
        # a morning already billed correctly).
        self.afk_source = None
        # Optional callback (set by the SyncCoordinator to aw_manager's
        # set_inproc_afk_active) invoked every cycle with the in-process-AFK
        # decision. The flag it sets gates the idle-tracker watchdog + AFK health
        # telemetry. Publishing it from HERE — the path where the decision is
        # actually made — makes the engine the single source of truth: the flag
        # tracks the engine every active cycle independent of the 60s reconcile
        # timer, which used to be the only writer and silently died in Bug A
        # (#76/#78), leaving the flag stale for a whole release. None / errors
        # tolerated — telemetry wiring must never break a sync cycle.
        self.inproc_afk_flag_sink: Optional[Callable[[bool], None]] = None
        # Mutated only on the sync thread (record_sample / _build_inproc_afk /
        # _commit_inproc_afk_checkpoint all run inside SyncEngine.sync()).
        self._afk_inproc_checkpoint: Optional[datetime] = None
        # Proposed next checkpoint for the span built this cycle; committed by
        # _commit_inproc_afk_checkpoint only after a confirmed send (finding B).
        self._afk_inproc_pending: Optional[datetime] = None
        # Blind-clock escalation latch + threshold (finding E).
        self._inproc_blind_reported = False
        self._INPROC_BLIND_THRESHOLD = 3
        # Same escalation latch for the in-process WINDOW probe: when its OS
        # frontmost-window read goes blind (e.g. psutil access-denied on win32),
        # it silently uploads zero per-app attribution while ALSO suppressing the
        # external tracker — the exact blind-capture failure the feature exists to
        # cure. Surface it to ops AND fall back to the external source while blind.
        self._window_inproc_blind_reported = False

        # Optional in-process WINDOW source (set by the SyncCoordinator), the
        # per-app analogue of afk_source. When present, enabled by config, and
        # the OS frontmost-window probe is usable, the agent uploads its own
        # per-app active-window stream and the external bf-window-tracker bucket
        # is skipped so the two never double-count. Ships dormant
        # (in_process_window defaults False). Same checkpoint discipline as AFK:
        # the checkpoint is the last instant covered by an upload, seeded to
        # `now` on the first cycle, committed only after a confirmed send.
        # Mutated only on the sync thread.
        self.window_source = None
        self._window_inproc_checkpoint: Optional[datetime] = None
        # NB: the per-cycle "pending" checkpoint is a LOCAL in sync() (threaded
        # build -> commit), not an instance field — see _build_inproc_window for
        # why (wedge re-arm can run two sync()s concurrently).
        # Optional in-process INPUT source (set by the SyncCoordinator), the
        # keystroke/click/scroll-count analogue of window_source. When present,
        # enabled by config, and an in-process counting backend is usable
        # (Windows ctypes hooks / macOS CGEventTap; off on Linux), the agent
        # uploads its own input-count stream and the external aw-watcher-input
        # bucket is skipped so the two never double-count. Counts accrue
        # continuously on the backend listener thread; the sync thread only
        # DRAINS them into an event each cycle. Checkpoint committed only after a
        # confirmed send, mirroring AFK/window. Mutated only on the sync thread.
        self.input_source = None
        self._input_inproc_checkpoint: Optional[datetime] = None

        # Monotonic timestamp of the current sync() cycle's start, stamped at the
        # top of sync(). Gates the per-bucket send loop's in-cycle network budget
        # (_SEND_SKIP_IF_CYCLE_ELAPSED) so N buckets can't stack N retry chains
        # past the _do_sync watchdog. None outside a sync() cycle (e.g. a direct
        # _send_events call in a unit test) -> the budget guard is inert.
        self._cycle_start_monotonic: Optional[float] = None

        # Starvation floor for the in-cycle network budget. `_cycle_delivered`
        # records whether THIS cycle put anything on the wire at all;
        # `_consecutive_undelivered_cycles` counts cycles in a row where the
        # budget gate refused the drain and nothing else had delivered either.
        # Both are touched only on the sync thread (a wedge-recovery cycle can
        # overlap, in which case the worst case is an extra forced drain — the
        # same failure the floor is there to cause on purpose).
        self._cycle_delivered = False
        self._consecutive_undelivered_cycles = 0

        # Queue retry backoff
        self._queue_consecutive_failures = 0
        self._queue_backoff_until = datetime.min.replace(tzinfo=timezone.utc)

        # Clock skew: track server-vs-local time offset (seconds, positive = server ahead)
        self._server_time_offset: Optional[float] = None
        self._hostname = socket.gethostname()

        # One-time upgrade migration: legacy status spans (idle/break/private/
        # sleep) queued by <=1.5.95 carry no bucket_id, so the queue's
        # storability classifier would evict them on the first cycle even though
        # the server accepts them — a lost billing carve-out. Give them the same
        # bf-status_<host> id current spans use, BEFORE the sync loop starts.
        # Idempotent and best-effort: a migration hiccup must never block startup.
        # Each migration is guarded on its own: they are independent, and a
        # hiccup in one must not silently skip the other (a shared try would
        # let a backfill error swallow the project_id sanitize, re-exposing the
        # rejected-span loss this release fixes).
        for name, args in (
            ("backfill_status_bucket_ids", (self._hostname,)),
            ("sanitize_project_ids", ()),
        ):
            try:
                migrate = getattr(self.queue, name, None)
                if callable(migrate):
                    migrate(*args)
            except Exception as e:  # pragma: no cover - defensive; never fatal
                logger.warning("Queued-event startup migration %s skipped: %s", name, e)

        # Dedup: track (bucket_id, event_id) pairs already sent this session.
        # The lookback window re-fetches recent events for duration updates —
        # we only re-send if the duration actually changed.
        self._sent_cache: BoundedLRU = BoundedLRU(maxsize=5_000)
        # Track original AW durations for gap-filled events to prevent re-send.
        self._gap_filled_originals: BoundedLRU = BoundedLRU(maxsize=5_000)
        # Time-tracking dedup: track last-counted duration per event to avoid
        # double-counting when the lookback window re-fetches events with grown
        # durations. Only the delta (new - old) is added to the time tracker.
        self._time_cache: BoundedLRU = BoundedLRU(maxsize=5_000)

        # Thread safety: protects cross-thread mutable state
        self._state_lock = threading.Lock()
        # Dedicated lock for all BoundedLRU caches (ops are multi-step and
        # touched from the sync thread plus heartbeat thread).
        self._cache_lock = threading.Lock()

        # Window-filter streak: consecutive sync cycles where the watcher produced
        # window events but the privacy filter dropped them ALL (window_seen>0,
        # window_sent==0). Distinguishes "produced-but-filtered" from the
        # watcher-quiet case (v1.5.83 logs that watcher-side). Single-threaded
        # (only _do_sync touches it via sync()), so no lock needed.
        self._window_filter_streak: int = 0
        self._window_filter_warned: bool = False

        # App category cache — avoids SQLite lookups on every event.
        # Populated lazily; invalidated when categories are refreshed.
        self._category_cache: Optional[dict[str, str]] = None
        self._category_cache_lock = threading.Lock()
        # Track which fallback categories have been persisted to DB this session.
        # Prevents repeated writes after cache invalidation.
        self._persisted_fallbacks: set[str] = set()

        # Activity analysis for fraud detection (DIP)
        self._activity_analyzer = activity_analyzer or ActivityAnalyzer(
            thresholds=self._create_engagement_thresholds(),
            fraud_config=self.config.fraud_detection,
        )
        self._time_tracker = time_tracker or DailyTimeTracker()
        self._afk_watcher_available = False  # True when AFK buckets exist this cycle
        # One-time-per-process flag: on first sync we rewind checkpoints to the
        # start of the local day so any locally-stored events the server never
        # received (sync outage / dropped by the old client filter) get re-sent.
        self._backlog_reconciled = False

        # Call/meeting detection
        self._call_detector: Optional[CallDetector] = (
            CallDetector(
                min_duration=config.call_detection.min_call_duration,
                max_credit_seconds=config.call_detection.max_credit_minutes * 60,
            )
            if config.call_detection.enabled
            else None
        )

        # Microphone-in-use meeting detection: the system-level companion to
        # the window-title CallDetector above — it keeps seeing a meeting when
        # the call window is NOT frontmost (background huddle while reading
        # docs). None when disabled (call_detection.enabled or mic_signal off)
        # or unsupported (Linux). Wired into AfkSource as an activity source by
        # main.py; sessions upload as auditable call events (call_type "mic").
        self._mic_detector: Optional[MicActivityDetector] = create_mic_detector(
            config, self._hostname
        )

        # Foreground-CPU activity detection: credits engaged-but-no-input work
        # (an active Claude Code / build / render in the focused window) that the
        # AFK timeout would otherwise mark idle. None when disabled or
        # unsupportable here (no frontmost-pid getter, or psutil missing). Wired
        # into AfkSource as an activity source by main.py so the uploaded AFK
        # stream stays not-afk on macOS/Windows; the uploaded dev-session span is
        # the server-validated path (and the only one on Linux, where in-process
        # AFK is inert).
        self._foreground_detector: Optional[ForegroundActivityDetector] = create_detector(
            config.foreground_activity, self._hostname
        )

        # The local day the counted-time cache is scoped to; a change across a
        # sync cycle triggers daily housekeeping (prune) without a restart.
        self._counted_cache_day = self._local_day_iso()

        # Restore per-event counted-time from the previous process so a restart
        # (or the start-of-day backlog reconcile that re-fetches the whole day)
        # does not re-count already-counted events into the local daily total.
        self._load_counted_time_cache()

    def _create_engagement_thresholds(self) -> EngagementThresholds:
        """Create EngagementThresholds from config."""
        eng = self.config.engagement
        return EngagementThresholds(
            sustained_typing_presses=eng.sustained_typing_presses,
            window_changes_min=eng.window_changes_min,
            scroll_threshold=eng.scroll_threshold,
            combined_presses_min=eng.combined_presses_min,
            combined_scrolls_min=eng.combined_scrolls_min,
            window_minutes=eng.window_minutes,
        )

    def pause(self) -> None:
        """Pause syncing and drop buffered events until resume."""
        with self._state_lock:
            need_advance = not self._paused
            self._paused = True
            need_end_session = self._session_active
            self._session_active = False
        if need_advance:
            self._advance_checkpoints_to_now("pause")
            # Close any open call/mic session: paused time is not recorded, so
            # a session left open would bridge the pause into one uploaded span.
            self.flush_engagement_detectors("pause")
        if need_end_session:
            try:
                self.bf.end_session("app_quit")
            except BetterFlowClientError as e:
                logger.warning("end_session(app_quit) failed: %s", e)
        # Free fraud detector accumulators while paused
        self._activity_analyzer.clear()

    def resume(self) -> None:
        """Resume syncing."""
        with self._state_lock:
            was_paused = self._paused
            self._paused = False
        # Skip the window AW recorded while paused — otherwise the next sync
        # re-fetches it (the fetch is "since checkpoint", so the enter-time
        # advance to pause-START doesn't cover it). See Lucian, 2026-06-22.
        if was_paused:
            self._advance_checkpoints_to_now("resume")

    def suspend_upload(self, reason: str) -> None:
        """Record that uploads can't land right now. NOT the same as pause().

        This is deliberately an observable marker, NOT an egress gate: it is
        written here and read only by is_upload_suspended (tray/diagnostics).
        Upload is already gated by `bf.is_reachable()` at every send site, with
        the offline queue as the durability layer, so the network outage stops
        the sends on its own — this flag adds nothing to that and must not
        become a second gate. If it ever gates a send, an outage the OS network
        monitor never reports as recovered (see
        BetterFlowApp._set_sync_failure_state: Wi-Fi "connected" with no route
        is exactly that case) latches it True for the rest of the process and
        the device stops uploading with a healthy network.

        pause() means "this window must never be recorded" — private time, a
        manual break, a working-hours close — and it enforces that by advancing
        every checkpoint past the window so the events can never be fetched.

        A network outage is the opposite: the work happened, it is billable, we
        simply cannot upload it yet. Routing it through pause() deleted it. The
        offline queue never saw those events because they were never fetched —
        which is why "0 queued" was reported all the way through outages that
        lost real time. Reproduced against this engine: 8 min offline lost 6 min,
        20 min lost 18, 95 min lost 93 (duration minus the 2-minute lookback),
        with send_events never called once.

        Livia Cimpeanu described it on 2026-06-16, five weeks before it was
        found in the code: "I was tracked before my break. It isn't anymore."
        """
        with self._state_lock:
            already = self._upload_suspended
            self._upload_suspended = True
        if not already:
            logger.info("Upload suspended (%s) — still capturing and queueing", reason)

    def resume_upload(self, reason: str) -> None:
        """Clear the marker. Deliberately does NOT touch checkpoints.

        That is the whole point: nothing is skipped on the way back, so the
        next normal "since checkpoint" fetch picks up everything recorded
        during the outage and the queue drains on the usual sync path.

        It DOES drop the session flag, though. pause() used to do that on the
        way into an outage (end_session + _session_active = False), so the
        first cycle back always re-established the session. Nothing heartbeats
        during an outage, so the server's 30-min cleanup marks the session
        'crashed' — and without this the agent would never call
        sessions/start again and tracking would not resume on return. Only the
        local flag is cleared: end_session is pointless mid-outage and the next
        cycle's start_session is the same call the pre-outage path made.
        """
        with self._state_lock:
            was = self._upload_suspended
            self._upload_suspended = False
            if was:
                self._session_active = False
        if was:
            logger.info("Upload resumed (%s) — draining the queue", reason)

    @property
    def is_upload_suspended(self) -> bool:
        with self._state_lock:
            return self._upload_suspended

    @property
    def is_paused(self) -> bool:
        with self._state_lock:
            return self._paused

    def set_enrichment_trackers(self, *, browser_tracker=None, display_tracker=None) -> None:
        """Attach/detach the browser-URL and display trackers.

        These used to be constructed in BetterFlowApp.__init__ and handed to this
        ctor once. They are now created and destroyed by the working-hours capture
        policy, so the engine needs a way to be told about the new objects.

        An explicit setter, not a bare attribute assignment: the engine reads
        `self._browser_tracker` (underscore), so `engine.browser_tracker = x` from
        outside silently created a NEW, never-read attribute while `_browser_tracker`
        stayed None for the whole process. Effect: browser events shipped with no
        URL, collapsed to generic "browsing", and were counted productive — the
        browser_domain-empty incident, reintroduced fleet-wide by the very fix that
        was supposed to protect people. A Mock-based test asserted the wrong
        attribute and passed.
        """
        with self._state_lock:
            self._browser_tracker = browser_tracker
            self._display_tracker = display_tracker

    def request_backlog_reconcile(self) -> None:
        """Re-arm the start-of-day backlog reconcile so the NEXT sync rewinds
        checkpoints and re-sends any locally-stored events the server never
        received. The backend upserts by AW event id, so replaying already-
        stored events is deduped — safe.

        The reconcile normally runs once per process (at startup), which made
        recovering a stuck day require a quit+restart. Manual "Sync Now" calls
        this first so a single click recovers the day, as users expect.
        """
        with self._state_lock:
            self._backlog_reconciled = False
        logger.info("Manual sync: re-armed start-of-day backlog reconcile")

    def set_private_mode(self, enabled: bool) -> None:
        """Enable/disable private time (no events recorded)."""
        with self._state_lock:
            entering_private = enabled and not self._private_mode
            leaving_private = not enabled and self._private_mode and self._private_start is not None
            private_start_snap = self._private_start
            self._private_mode = enabled
            if entering_private:
                self._private_start = datetime.now(timezone.utc)
            elif leaving_private:
                self._private_start = None
            need_end_session = enabled and self._session_active
            if need_end_session:
                self._session_active = False
        if entering_private:
            self._advance_checkpoints_to_now("private_time")
            # Close any open call/mic session at the boundary. Without this, a
            # call spanning the private hour ends AFTER it and uploads one
            # 'completed' span covering the whole private period — recording
            # exactly what Private Time contractually never records.
            self.flush_engagement_detectors("private_time")
        if leaving_private and private_start_snap:
            # Skip the private window AW recorded (active window + not-afk while
            # the user kept working). The enter-time advance set checkpoints to
            # private-START, and the fetch is "since checkpoint", so without this
            # leave-time advance the whole private hour re-syncs and bills as
            # ACTIVE — the exact bug Lucian hit on 2026-06-22.
            self._advance_checkpoints_to_now("private_time_end")
            self._send_private_time_event(private_start_snap)
        if need_end_session:
            try:
                self.bf.end_session("user_logout")
            except BetterFlowClientError as e:
                logger.warning("end_session(user_logout) failed: %s", e)

    @property
    def is_private(self) -> bool:
        with self._state_lock:
            return self._private_mode

    def is_in_call(self) -> bool:
        """True while the call/meeting detector reports an active call.

        Idle detection consults this so a meeting/call with no keyboard or
        mouse input is not mistaken for idle. False when call detection is
        disabled. The detector's state is advanced as window events are
        processed each sync cycle and persists across cycles until the call
        ends, so it stays True for the duration of a meeting.
        """
        detector = self._call_detector
        if not (detector and detector.is_in_call()):
            return False
        # Evidence freshness: with AW up but the window watcher hung mid-call,
        # no event will ever end the call — the raw state machine stays
        # IN_CALL forever. Don't suppress the idle pause on state nothing has
        # confirmed for minutes (the billed credit already froze via the same
        # staleness rule; local and uploaded must agree).
        try:
            return detector.has_fresh_evidence(datetime.now(timezone.utc))
        except Exception as e:
            logger.debug("is_in_call freshness check failed: %s", e)
            return True  # defensive: never break the idle guard on a helper error

    def is_active_dev_session(self) -> bool:
        """True while the foreground-CPU detector reports an active session.

        Consulted by idle detection alongside ``is_in_call`` so an engaged
        no-input session (an active Claude Code / build / render in the focused
        window) isn't mistaken for idle. False when the detector is disabled or
        unsupported on this platform.
        """
        detector = self._foreground_detector
        return bool(detector and detector.is_active())

    def is_mic_meeting_active(self) -> bool:
        """True while the mic-in-use detector reports an open meeting session.

        Consulted by idle detection alongside ``is_in_call`` — the mic keeps
        seeing a meeting the window-title detector loses the moment the call
        window stops being frontmost. False when disabled or unsupported.
        """
        detector = self._mic_detector
        return bool(detector and detector.is_active())

    def _observe_mic_activity(self, all_events: list, stats: "SyncStats") -> None:
        """Sample the microphone and append any mic-meeting span.

        Mirrors ``_observe_foreground_activity``: the session stays OPEN across
        cycles (so ``is_mic_meeting_active`` doesn't flap), a live snapshot is
        uploaded each cycle under a deterministic id (server upserts one row),
        and the final span is emitted when the mic goes cold past the grace.
        """
        detector = self._mic_detector
        if detector is None:
            return
        cd = self.config.call_detection
        if not (cd.enabled and cd.mic_signal):
            # Server kill switch without an app restart: the detector was
            # built at startup, but a privacy-sensitive probe must honour a
            # server-pushed mic_signal=false NOW. Close any open session (the
            # truthful span still ships) and stop sampling.
            try:
                ended = detector.flush()
            except Exception as e:
                logger.debug("mic detector kill-switch flush failed: %s", e)
                ended = None
            if ended:
                all_events.append(self._stamp_project(ended))
            return
        now = datetime.now(timezone.utc)
        try:
            ended = detector.observe(now)
            live = detector.snapshot() if ended is None else None
        except Exception as e:
            logger.debug("mic activity observe failed: %s", e)
            return
        span = ended or live
        if span:
            all_events.append(self._stamp_project(span))
            if ended is not None:
                stats.calls_detected += 1

    def _current_project_id(self) -> Optional[int]:
        """The active project's id, or None. The single locked read of
        _current_project that every event-stamping path shares, so the
        lock+read rule can't drift between call sites. Normalization is the
        queue's shared helper, so a live stamp and the queue migration always
        agree on which ids the backend accepts."""
        with self._state_lock:
            project = self._current_project
            if not project:
                return None
            raw = project.get("id")
            pid = normalized_project_id(raw)
            # Dropping the id silently would untag every event with no trace —
            # the failure mode is invisible in logs and only shows up as
            # unprojected rows in the backend. Warn once per distinct value.
            if pid is None and raw != self._rejected_project_id:
                self._rejected_project_id = raw
                logger.warning(
                    "Active project id %r is not a backend project id; "
                    "events will be sent untagged",
                    raw,
                )
            elif pid is not None:
                self._rejected_project_id = _NO_REJECTED_PROJECT_ID
        return pid

    def _stamp_project(self, event: dict) -> dict:
        """Stamp the active project onto a SyncEngine-built event dict (call,
        mic, window, status-span, synthetic-active-AFK). The single place the
        engine reads-and-writes the project, so these rows can't drift in how
        they are tagged (the same meeting must never upsert a projected row
        next to an unprojected one). AfkSource-built AFK events don't pass
        through here: they receive the id as a parameter (from
        _current_project_id()) and assign it in AfkSource._event() — so the
        project DECISION is still single-sourced (_current_project_id), even
        though the dict write happens in two modules."""
        pid = self._current_project_id()
        if pid is not None:
            event["project_id"] = pid
        return event

    def _enqueue_events_best_effort(self, events: list, context: str) -> None:
        """Queue events for later delivery when they can't be sent now (fully
        offline paths that bypass _send_events' own queue-on-failure). Best
        effort: a queue error is logged, never raised into the sync cycle."""
        try:
            self.queue.enqueue(events)
        except Exception as e:
            logger.warning(
                "Failed to queue %d event(s) (%s): %s", len(events), context, e
            )

    def _deliver_final_event(self, event: dict, context: str) -> None:
        """Deliver a final detector event at shutdown: one immediate direct
        send, queueing on any non-auth failure (a definitive rejection ages
        out via the queue's retry dead-lettering). Never raises — shutdown
        must complete regardless.

        Deliberately bypasses _send_events: its auth-error path enqueues the
        batch before raising, and an expired token is the LIKELIEST failure at
        logout — the queued row would then be delivered under whoever logs in
        next on this machine (the queue is account-agnostic). On auth failure
        the event is dropped with a log line instead, matching
        _send_status_span's policy for exactly this reason.
        """
        try:
            self._note_delivery_attempt()
            result = self.bf.send_events([event])
            if getattr(result, "success", False):
                return
            self._enqueue_events_best_effort([event], context)
        except BetterFlowAuthError as e:
            logger.warning(
                "final event dropped (%s): auth error at shutdown — not queued "
                "(would deliver under the next login): %s",
                context,
                e,
            )
        except Exception as e:
            logger.warning("final event send failed (%s): %s — queueing", context, e)
            self._enqueue_events_best_effort([event], context)

    def engagement_activity_sources(self) -> list:
        """Every engagement detector that feeds AFK credit — THE list main.py
        registers with AfkSource. Owned by the class that constructs the
        detectors so adding detector #4 can't silently miss the uploaded
        stream (a detector wired into the local idle guard but not into
        AfkSource reproduces this feature's founding bug: tray says tracking,
        server bills idle)."""
        return [
            d
            for d in (
                self._call_detector,
                self._mic_detector,
                self._foreground_detector,
            )
            if d is not None
        ]

    def flush_engagement_detectors(self, context: str) -> None:
        """Close any open call/mic session at a capture boundary — pause,
        private time, working-hours suppression.

        Detector state must NOT stay open across a not-recorded period: the
        detectors receive no events while capture is off, so the eventual
        close after resume would emit one span BRIDGING the boundary (a
        'completed' call covering a whole private hour, or a 4h-capped call
        spanning the suppressed night). The truthful pre-boundary span is
        queued for delivery; AFK credit survives via the detectors'
        ended-session memory, which freezes at the real pre-boundary end.
        """
        if self._call_detector is not None:
            try:
                remaining = self._call_detector.flush()
                if remaining:
                    self._enqueue_events_best_effort(
                        [self._make_call_bf_event(remaining)], context
                    )
            except Exception as e:
                logger.warning("call detector flush (%s) failed: %s", context, e)
        if self._mic_detector is not None:
            try:
                ended = self._mic_detector.flush()
                if ended:
                    self._enqueue_events_best_effort(
                        [self._stamp_project(ended)], context
                    )
            except Exception as e:
                logger.warning("mic detector flush (%s) failed: %s", context, e)
        # The foreground/dev-session detector too: its upserted span would
        # otherwise bridge the boundary the same way (session_start pre-pause,
        # first post-resume observe() extends it across the not-recorded
        # period) — and on Linux the dev-session span IS the billing path.
        if self._foreground_detector is not None:
            try:
                span = self._foreground_detector.flush()
                if span:
                    self._enqueue_events_best_effort([span], context)
            except Exception as e:
                logger.warning("foreground detector flush (%s) failed: %s", context, e)

    def _observe_foreground_activity(
        self, all_events: list, stats: "SyncStats", cycle: "_SyncCycleContext"
    ) -> None:
        """Sample the foreground process and append any dev-session span.

        Anchors credit to the most recent real input: the OS idle clock / input
        watcher (macOS/Windows, via the AFK source) OR the latest input-bucket
        event (all platforms, incl. Linux) — whichever is newer. The session is
        kept OPEN across cycles (so ``is_active_dev_session`` doesn't flap and
        wrongly trip the idle pause); a live snapshot is uploaded each cycle for
        server validation, and the final span is emitted when it naturally ends.
        Both carry a deterministic id, so the server upserts one record."""
        detector = self._foreground_detector
        if detector is None:
            return
        now = datetime.now(timezone.utc)
        last_real = cycle.last_input_at
        if self.afk_source is not None:
            base = self.afk_source.base_last_input_at(now)
            if base is not None and (last_real is None or base > last_real):
                last_real = base
        try:
            ended = detector.observe(now, last_real)
            live = detector.snapshot() if ended is None else None
        except Exception as e:
            logger.debug("foreground activity observe failed: %s", e)
            return
        span = ended or live
        if span:
            all_events.append(span)
            if ended is not None:
                stats.dev_sessions_detected += 1

    def set_current_project(self, project: Optional[dict]) -> None:
        """Set the current project for event tagging."""
        with self._state_lock:
            self._current_project = project

    def invalidate_category_cache(self) -> None:
        """Clear the in-memory category cache so next lookup re-reads from DB."""
        with self._category_cache_lock:
            self._category_cache = None
            self._persisted_fallbacks.clear()

    def _get_category(self, app_name: str) -> Optional[str]:
        """Look up category for an app from DB cache only (M3).

        Returns None if the app has no DB-sourced category.
        Does NOT consult the fallback map - callers handle that explicitly.
        """
        with self._category_cache_lock:
            if self._category_cache is None:
                self._category_cache = self.queue.get_all_categories()
            return self._category_cache.get(app_name)

    def _config_refetch_due(self, now: float) -> bool:
        """True if server config should be (re)fetched this cycle: never fetched
        yet, or the refetch interval has elapsed since the last successful fetch.

        Periodic refetch is how a mid-session schedule change reaches a RUNNING
        agent — config was otherwise fetched once per process, so a user marked
        restricted after startup kept being recorded until the app restarted.
        """
        if not self._config_fetched:
            return True
        # Fetched, but no real fetch timestamp (0.0 = never stamped, e.g. the
        # flag was set directly rather than via fetch_server_config): we can't
        # measure staleness, so hold — no spurious refetch.
        if self._last_config_fetch_monotonic <= 0:
            return False
        return (
            now - self._last_config_fetch_monotonic
        ) >= self._CONFIG_REFETCH_INTERVAL_SECONDS

    def fetch_server_config(self) -> None:
        """Fetch and apply server-side configuration."""
        try:
            server_config = self.bf.get_config()
            self.config.update_from_server(server_config)
            self._config_fetched = True
            self._last_config_fetch_monotonic = time.monotonic()
            self.invalidate_category_cache()
            logger.info("Server configuration applied")

            # Update activity analyzer thresholds from new config
            self._activity_analyzer.update_thresholds(
                self._create_engagement_thresholds()
            )
            self._activity_analyzer.update_fraud_config(self.config.fraud_detection)

            if self._on_config_updated:
                self._on_config_updated()
        except BetterFlowAuthError:
            raise
        except BetterFlowClientError as e:
            logger.warning(f"Failed to fetch server config: {e}")
            # Back off a FAILED refetch by the normal interval, else a /config
            # route that 500s while the rest of the API is healthy would leave
            # every already-configured agent "due" every cycle, burning the whole
            # per-cycle network budget in get_config()'s retry chain and starving
            # uploads (and brushing the "Sync hung" watchdog). Stamping here is
            # safe for the INITIAL fetch too: _config_refetch_due short-circuits
            # on `not _config_fetched`, so a brand-new agent still retries every
            # cycle until it first succeeds (it must, to learn its schedule).
            self._last_config_fetch_monotonic = time.monotonic()

    def sync(self) -> SyncStats:
        """Perform a sync cycle.

        1. Fetch server config (first time only)
        2. Check if ActivityWatch is running
        3. Get events since last checkpoint
        4. Send raw events to BetterFlow (server handles privacy)
        5. Update checkpoints
        6. Send heartbeat periodically
        """
        stats = SyncStats()
        cycle_start = time.monotonic()  # to keep the queue drain inside the watchdog budget
        # Publish the cycle start so _send_events can bound the per-bucket send
        # loop to the same in-cycle network budget as the queue drain.
        self._cycle_start_monotonic = cycle_start
        # Fresh cycle, nothing delivered yet. Feeds the drain gate's starvation
        # floor (_drain_gate_allows).
        self._cycle_delivered = False

        # Daily housekeeping before anything else, so it still runs while paused:
        # a long-running agent that never restarts across midnight would
        # otherwise never prune persisted counted-time (prune_counted_time only
        # ran at startup), leaking a day's rows every day it stays up.
        self._maybe_rollover_counted_day()

        with self._state_lock:
            paused = self._paused
            private_mode = self._private_mode
            private_start = self._private_start
        if paused or private_mode:
            # Re-flush the engagement detectors every paused/private cycle
            # (idempotent — no-op when nothing is open). The boundary flush in
            # pause()/set_private_mode() runs on the TRAY thread and can race
            # a sync cycle already in flight: that cycle passed the paused
            # check before the toggle and keeps feeding pre-boundary window
            # events into the call detector AFTER the flush, re-opening the
            # call. Without this, the reopened call stays open across the
            # whole private period and the first post-resume event closes it
            # with an end that BRIDGES it — exactly the privacy bug the
            # boundary flush exists to prevent. Mirrors the suppressed branch.
            self.flush_engagement_detectors("paused_or_private")
            # While Private Time is on, nothing is synced — so the backend
            # cannot tell an ongoing private session from a network gap, and
            # its tracked-hours trailing grace painted the window as counted
            # "Tracked time" until the leave-time private_time event finally
            # arrived (Ecaterina/Martin, 2026-07-07). Re-send the growing span
            # every cycle: same deterministic id (private_<startts>_<engine>),
            # so the server patches the duration in place and the timeline
            # shows Private within one sync cycle instead of only after the
            # session ends. Snapshots are NOT queued on failure — the next
            # cycle's longer span supersedes this one; only the final
            # leave-time send (set_private_mode) is queued for retry.
            if private_mode and private_start is not None:
                self._send_status_span(
                    kind="private", start=private_start, queue_on_failure=False
                )
            return stats

        # Fetch server config on first successful sync. Gated by the shared
        # per-cycle network budget: get_config() runs a full retry chain
        # (~94s against a hung server), so it must count toward the SAME 50s
        # budget the _do_sync watchdog was sized around. Otherwise it stacks
        # ahead of the session-start and send chains and the cycle overruns the
        # 150s deadline ("Sync hung"). The budget-start is stamped at the top of
        # sync() (self._cycle_start_monotonic), so every per-cycle network call
        # after it — this fetch, start_session, the send loop, the queue drain —
        # shares one budget.
        if (
            self._config_refetch_due(time.monotonic())
            and not self._cycle_network_budget_exceeded(self._CYCLE_NETWORK_BUDGET_SECONDS)
            and self.bf.is_reachable()
        ):
            self.fetch_server_config()

        # Outside the working-hours window the trackers are intentionally stopped
        # (AppController._apply_capture_policy). A stopped tracker is the correct
        # state here, not an outage: fall through to the AW check below and it
        # would report "ActivityWatch is not running" every cycle all night, and —
        # worse — return early, so in-hours work still sitting in the offline queue
        # would not upload until the window reopened.
        #
        # So: fetch nothing, but still drain the queue and keep the heartbeat
        # going, which is also what keeps the device visibly online rather than
        # looking dead every evening.
        if not self.config.working_hours.allows(datetime.now(timezone.utc)):
            stats.capture_suppressed = True
            # Close any open call/mic session ONCE at the suppression edge
            # (idempotent — later suppressed cycles find nothing open). A call
            # title still matching when capture stops at 22:00 would otherwise
            # stay open all night (trackers down, no events, no flush) and the
            # morning resume would upload a cap-length span covering hours
            # that were contractually not recorded.
            self.flush_engagement_detectors("capture_suppressed")
            if self.bf.is_reachable() and not self.queue.is_empty():
                if self._drain_gate_allows():
                    self._process_queue(stats)
            with self._state_lock:
                self._heartbeat_count += 1
                should_heartbeat = self._heartbeat_count >= self._heartbeat_interval
                if should_heartbeat:
                    self._heartbeat_count = 0
            stats._should_heartbeat = should_heartbeat
            return stats

        # Check ActivityWatch
        if not self.aw.is_running():
            stats.errors.append("ActivityWatch is not running")
            # Finalize any in-progress call before bailing. Without window
            # events the detector can never observe the call ending, so
            # is_in_call() would stay True for the whole outage and the idle
            # guard in IdleManager would keep suppressing idle long after the
            # call ended — painting the post-call AFK stretch as worked time.
            # (AFK credit is safe either way: get_last_active_at freezes at the
            # flushed call's END, never tracks `now` for an ended call.) The
            # observed call portion is still recorded if the backend is
            # reachable; if AW comes back mid-call a fresh call starts cleanly.
            #
            # That recovery can emit a SECOND event for the same meeting (the
            # continuation is picked up from the un-advanced checkpoint). Both
            # carry the deterministic id call_<app>_<start_ts> and the backend
            # upserts call events by id, so the overlapping recovered portion
            # supersedes this flushed one rather than double-billing.
            if self._call_detector:
                remaining = self._call_detector.flush()
                if remaining:
                    stats.calls_detected += 1
                    ev = self._make_call_bf_event(remaining)
                    if self.bf.is_reachable():
                        # _send_events may set stats.queued_bucket_ids on failure;
                        # we return immediately and intentionally don't act on it —
                        # call events go to the synthetic call bucket, not a
                        # checkpointed AW bucket, so there is no checkpoint to
                        # withhold.
                        self._send_events([ev], stats)
                    else:
                        # Fully offline (AW down AND backend unreachable, e.g. a
                        # resume off-network): the flush already reset the
                        # detector, so this event can never be re-emitted — queue
                        # it or the observed call span is silently lost.
                        self._enqueue_events_best_effort([ev], "AW-outage call flush")
            # The mic probe doesn't depend on AW — keep observing during the
            # outage so a meeting that ends mid-outage closes its session.
            # Unlike the window-title detector above, no flush is needed: the
            # mic going cold is observable without window events, and a still-
            # hot mic is a still-running meeting that SHOULD keep suppressing
            # idle. An ended span is recorded (or queued when offline). The
            # kill switch applies here exactly as on the normal path — an AW
            # outage must not keep a server-disabled mic probe sampling.
            if self._mic_detector:
                cd_cfg = self.config.call_detection
                try:
                    if not (cd_cfg.enabled and cd_cfg.mic_signal):
                        ended = self._mic_detector.flush()
                    else:
                        ended = self._mic_detector.observe(datetime.now(timezone.utc))
                except Exception as e:
                    logger.debug("mic activity observe (AW outage) failed: %s", e)
                    ended = None
                if ended:
                    stats.calls_detected += 1
                    ended = self._stamp_project(ended)
                    if self.bf.is_reachable():
                        self._send_events([ended], stats)
                    else:
                        self._enqueue_events_best_effort([ended], "AW-outage mic close")
            # The AFK sample log doesn't depend on AW either (OS idle clock +
            # in-process sources). Without this, an outage spanning a hands-off
            # meeting records NO samples, and after recovery the >timeout gap
            # in the sample log is reconstructed as afk — re-creating the exact
            # billed-idle-during-meeting failure this feature exists to fix,
            # any time AW hiccups mid-meeting.
            if self.afk_source is not None:
                try:
                    self.afk_source.record_sample(
                        datetime.now(timezone.utc),
                        protect_since=self._afk_inproc_checkpoint,
                    )
                except Exception as e:
                    logger.debug("afk_source.record_sample (AW outage) failed: %s", e)
            return stats

        # Start session if needed (attempt directly; no pre-check to avoid TOCTOU).
        # start_session() ALREADY retries internally (retry=True -> up to ~94s
        # against a hung server). An OUTER `for attempt in range(2)` loop doubled
        # that to ~188s — enough to blow the 150s _do_sync watchdog on its own
        # (the "Sync hung" reports). Call it ONCE per cycle: the durable
        # OfflineQueue plus the next sync cycle already provide cross-cycle retry,
        # so an outer multiplier buys nothing but watchdog overruns. Also gate it
        # on the shared per-cycle network budget so a slow config-fetch ahead of
        # it can't push the combined network time past the watchdog.
        with self._state_lock:
            need_session = not self._session_active
        if need_session and not self._cycle_network_budget_exceeded(
            self._CYCLE_NETWORK_BUDGET_SECONDS
        ):
            try:
                self.bf.start_session()
                with self._state_lock:
                    self._session_active = True
            except BetterFlowClientError as e:
                # BetterFlowAuthError is a BetterFlowClientError subclass; the
                # pre-existing behaviour swallowed both here (logged, not
                # re-raised) and let the next cycle retry. Preserved to keep the
                # session-start failure mode unchanged — only the retry MULTIPLIER
                # is removed.
                logger.warning(f"Failed to start session: {e}")

        # Record an activity sample for the in-process AFK timeline (no-op when
        # no source is wired or the OS idle clock is unavailable). Done every
        # cycle so the sample log stays dense regardless of bucket-fetch outcome.
        if self.afk_source is not None:
            try:
                # Protect samples back to the in-process checkpoint so the active
                # samples taken just before a long pause survive this prune and
                # can still be billed by the re-seed salvage below (and so a
                # held checkpoint can rebuild un-acked spans, finding B).
                # The checkpoint read is lock-free: it's a GIL-atomic reference
                # read, _advance_checkpoints_to_now only moves it FORWARD to now,
                # and _build_inproc_afk re-reads it fresh — so a race only ever
                # over-retains a few samples (bounded by the deque maxlen), never
                # mis-bills.
                self.afk_source.record_sample(
                    datetime.now(timezone.utc),
                    protect_since=self._afk_inproc_checkpoint,
                )
            except Exception as e:
                logger.debug("afk_source.record_sample failed: %s", e)

        # Record a frontmost-window sample for the in-process window timeline.
        # Done every cycle too (in addition to the dedicated fast sampler) so a
        # fresh sample always exists right before build_window_events runs.
        self.record_window_sample_if_active(datetime.now(timezone.utc))

        # Get buckets to sync
        try:
            window_buckets = self.aw.get_window_buckets()
            web_buckets = self.aw.get_web_buckets()
            afk_buckets = self.aw.get_afk_buckets()
            input_buckets = self.aw.get_input_buckets()
        except AWClientError as e:
            stats.errors.append(f"Failed to get buckets: {e}")
            # is_running() (/info) can still pass while /buckets/ 503s on a
            # half-hung bf-data-service. Flag it so the coordinator force_restarts
            # the hung server instead of looping this error forever (the 2 AM 503
            # storm that cost Liviu ~75 min, recovered only by a manual restart).
            stats.aw_bucket_fetch_failed = True
            return stats

        # Track whether AFK watcher is running so _transform_event can
        # distinguish "watcher down" (default active) from "genuinely idle".
        with self._state_lock:
            self._afk_watcher_available = bool(afk_buckets)

        # One-time backlog reconcile (this process): rewind checkpoints to the
        # start of the local day so events that never reached the server are
        # re-fetched and re-sent. The backend upserts by AW event id, so
        # replaying already-stored events is deduped — safe. This is what makes
        # a simple quit+restart recover a stuck day's data. It builds its own
        # default context (input analysis hasn't run yet).
        if not self._backlog_reconciled:
            self._reconcile_backlog(
                window_buckets + web_buckets + afk_buckets + input_buckets
            )
            self._backlog_reconciled = True

        # Fetch input events for activity analysis before processing window
        # events. The returned context is threaded explicitly through the
        # transform path — no per-cycle state lives on the instance.
        cycle = self._prepare_input_analysis(input_buckets)

        # When the agent is the sole per-app window source (in-process), drop the
        # external bf-window-tracker bucket(s) entirely — we upload our own stream
        # below instead, so its (possibly blind) events never reach the server and
        # can't double-count. No-op when the flag is off (default): the external
        # window bucket syncs exactly as today.
        # Escalate a blind in-process window probe once per episode (and, via
        # _should_skip_external_window below, fall back to the external tracker).
        self._check_window_source_health()
        skip_external_window = self._should_skip_external_window()
        window_buckets_to_sync = (
            [b for b in window_buckets if not _is_window_like(b.type)]
            if skip_external_window else window_buckets
        )

        # Sync window buckets with gap-filling
        all_events, call_events, pending_checkpoints = self._sync_window_buckets(
            window_buckets_to_sync, stats, cycle
        )

        # Foreground-CPU activity: sample the focused process and credit engaged
        # no-input work (an active Claude Code / build / render). Anchored to the
        # most recent real input so credit only ever extends near genuine human
        # presence. Updates is_active_dev_session() for the idle guard, advances
        # the AFK activity-source credit (folded by next cycle's record_sample),
        # and emits an auditable dev-session span when a session ends.
        self._observe_foreground_activity(all_events, stats, cycle)

        # Microphone-in-use meeting detection: the system-level companion to the
        # window-title call detector — sees a meeting even when the call window
        # isn't frontmost. Keeps is_mic_meeting_active() current for the idle
        # guard, advances the AFK activity-source credit, and uploads a live
        # snapshot / final span (call_type "mic") for server-side audit.
        self._observe_mic_activity(all_events, stats)

        # Clear window-specific AFK context before processing non-window buckets
        # so AFK events from one window bucket don't leak into unrelated buckets.
        cycle.afk_events = []

        # Sync non-window buckets normally. When the agent is the sole AFK source
        # (in-process), drop the external bf-idle-tracker bucket entirely — we
        # upload our own stream below instead, so its (possibly frozen/blind)
        # events never reach the server.
        skip_external_afk = self._should_skip_external_afk()
        # Publish the decision to the flag sink on the path where it's made, so
        # aw_manager's flag (idle-tracker watchdog + AFK telemetry) stays in step
        # with the engine every active cycle — one source of truth, not a cache a
        # separate timer keeps in sync (and silently failed to: Bug A, #76/#78).
        self._publish_inproc_afk_flag(skip_external_afk)
        # When the agent counts input in-process (the sole input source), drop the
        # external aw-watcher-input bucket(s) so its (possibly hook-blocked, zero)
        # events never reach the server and can't double-count. No-op when the
        # flag is off (default): the external input bucket syncs exactly as today.
        skip_external_input = self._should_skip_external_input()
        for bucket in web_buckets + afk_buckets + input_buckets:
            if skip_external_afk and _is_afk_like(bucket.type):
                continue
            if skip_external_input and _is_input_like(bucket.type):
                continue
            try:
                events, checkpoint = self._sync_bucket(bucket.id, bucket.type, stats, cycle)
                all_events.extend(events)
                if checkpoint:
                    pending_checkpoints.append(checkpoint)
                stats.buckets_synced += 1
            except AWClientError as e:
                stats.errors.append(f"Failed to sync bucket {bucket.id}: {e}")

        if skip_external_afk:
            # Sole-source path: upload the in-process AFK stream for the slice
            # since the last covered instant. Subsumes _synthesize_for_stale_afk.
            all_events.extend(self._build_inproc_afk(datetime.now(timezone.utc)))
        else:
            # External-bucket path: if bf-idle-tracker has frozen but the OS idle
            # clock shows the user kept working, upload a synthetic not-afk span so
            # the frozen tracker's worked span isn't billed idle ("Active time not
            # advancing" alert) — the gap #53/#56 left open on the upload side.
            synth_afk = self._synthesize_for_stale_afk(afk_buckets)
            if synth_afk:
                all_events.append(synth_afk)

        # Pending in-process window checkpoint for this cycle, kept as a LOCAL
        # (not an instance field) so a concurrent wedge-recovery cycle can't
        # clobber the value this cycle will commit after its own confirmed send.
        window_pending: Optional[datetime] = None
        if skip_external_window:
            # Sole-source path: upload the in-process per-app window stream for
            # the slice since the last covered instant.
            window_events, window_pending = self._build_inproc_window(
                datetime.now(timezone.utc)
            )
            all_events.extend(window_events)

        # Pending in-process input checkpoint for this cycle, kept as a LOCAL for
        # the same wedge-recovery reason window_pending is (see _build_inproc_window).
        input_pending: Optional[datetime] = None
        if skip_external_input:
            # Sole-source path: drain the accumulated keystroke/click/scroll
            # counts into one event for the slice since the last covered instant.
            input_event, input_pending = self._build_inproc_input(
                datetime.now(timezone.utc)
            )
            if input_event is not None:
                all_events.append(input_event)

        # Live snapshot of any ongoing call — WITHOUT ending it. The id derives
        # from (app, start), so the server upserts one growing row per meeting.
        # The old per-cycle flush() here ended the call at every sync boundary:
        # one meeting fragmented into per-cycle call rows, is_in_call() dropped
        # to False between cycles (IdleManager's in-call idle suppression raced
        # a sub-second window and mostly never engaged — Ecaterina's huddle
        # billed Idle, 2026-07-15), and the AFK activity source found flushed
        # state at record_sample time. The call now stays open until its real
        # end (grace expiry), an AW outage, or shutdown.
        if self._call_detector:
            snap = self._call_detector.snapshot()
            if snap:
                all_events.append(
                    self._make_call_bf_event(snap, status=CALL_STATUS_ONGOING)
                )
        if call_events:
            stats.calls_detected += len(call_events)
            all_events.extend(call_events)

        # Send events, then advance checkpoints per-bucket.
        self._send_and_advance_checkpoints(all_events, pending_checkpoints, stats)
        # Commit the in-process AFK checkpoint only if its bucket wasn't queued
        # (the send was confirmed) — otherwise rebuild it next cycle (finding B).
        self._commit_inproc_afk_checkpoint(stats)
        # Same discipline for the in-process window stream: advance its checkpoint
        # only after a confirmed send. Pass this cycle's own pending value.
        self._commit_inproc_window_checkpoint(stats, window_pending)
        # Same discipline for the in-process input stream.
        self._commit_inproc_input_checkpoint(stats, input_pending)

        # Process offline queue if we're online — but only if this cycle hasn't
        # already burned most of the watchdog budget on a slow/hung regular send
        # (else two ~94s chains stack past _DO_SYNC_DEADLINE; the queue drains
        # next cycle). _drain_gate_allows adds the floor that stops "next cycle"
        # from being the answer forever — see its docstring.
        if self.bf.is_reachable() and not self.queue.is_empty():
            if self._drain_gate_allows():
                self._process_queue(stats)
            else:
                # The regular send already burned most of the watchdog budget
                # (slow/hung server). Skip the drain this cycle so two ~94s chains
                # don't stack past the deadline; it drains next cycle. Log it so a
                # climbing queue_size traces to drain-skips, not ingest failure.
                logger.debug(
                    "Skipping queue drain this cycle: budget %ds already spent "
                    "(queue_size=%d)",
                    self._QUEUE_SKIP_IF_CYCLE_ELAPSED, self.queue.size(),
                )

        # Check heartbeat counter — actual HTTP call is deferred to
        # after sync() returns so _sync_lock is not held during the
        # blocking network request.
        with self._state_lock:
            self._heartbeat_count += 1
            should_heartbeat = self._heartbeat_count >= self._heartbeat_interval
            if should_heartbeat:
                self._heartbeat_count = 0
        stats._should_heartbeat = should_heartbeat

        self._assess_window_filter(stats)

        return stats

    # Warn after this many consecutive cycles where window events were produced
    # but the filter dropped them all. At a 5-min sync interval this is ~15 min,
    # matching the server's window-ingest-stall threshold; at 1-min, ~3 min.
    _WINDOW_FILTER_WARN_CYCLES = 3

    def _assess_window_filter(self, stats: SyncStats) -> None:
        """Classify a 'no window data on the server' gap as filter-side.

        When the watcher produced window events this cycle (window_seen>0) but
        the privacy filter dropped them all (window_sent==0), the server sees
        window/app data go stale even though the watcher is healthy — so the
        watcher-side warning (v1.5.83) stays silent. After a sustained streak,
        warn once and NAME the cause (which excluded app, or sub-minimum
        flickers) so the next occurrence classifies itself instead of being a
        third indistinguishable silence (Cristian Dragota, 2026-06-25).
        """
        produced_but_filtered = stats.window_seen > 0 and stats.window_sent == 0
        if not produced_but_filtered:
            if self._window_filter_warned:
                logger.info(
                    "Window/app events are reaching the server again "
                    "(filter no longer dropping them all)"
                )
            self._window_filter_streak = 0
            self._window_filter_warned = False
            return

        self._window_filter_streak += 1
        if self._window_filter_warned or self._window_filter_streak < self._WINDOW_FILTER_WARN_CYCLES:
            return
        self._window_filter_warned = True
        if stats.window_drop_excluded_apps:
            cause = "excluded app(s) frontmost: " + ", ".join(sorted(stats.window_drop_excluded_apps))
        elif stats.window_drop_short:
            cause = (
                f"{stats.window_drop_short} window event(s) under the "
                f"{self.config.sync.min_window_event_seconds:.0f}s minimum (flicker filter)"
            )
        else:
            cause = "all window events filtered (dedup / zero-duration)"
        logger.warning(
            "Window/app data has gone stale on the server across %d cycles — the "
            "watcher IS producing window events but the filter is dropping them all "
            "(%s). Billing is unaffected (AFK/input still upload); only per-app "
            "attribution is lost for this span.",
            self._window_filter_streak, cause,
        )

    def _prepare_input_analysis(self, input_buckets: list) -> _SyncCycleContext:
        """Fetch recent input events, feed the activity analyzer, and return a
        fresh cycle context recording whether input data exists.

        The lookback covers the engagement window and the AFK grace period so
        the analyzer sees enough history to classify engagement.
        """
        input_lookback_minutes = max(
            self.config.engagement.window_minutes * 2,
            self.config.aw.afk_timeout_minutes + 2,
        )
        input_events_for_analysis: list[AWEvent] = []
        for bucket in input_buckets:
            try:
                events = self.aw.get_events_since(
                    bucket.id,
                    datetime.now(timezone.utc) - timedelta(minutes=input_lookback_minutes),
                    limit=1000,
                )
                input_events_for_analysis.extend(events)
            except AWClientError as e:
                logger.debug("input bucket %s fetch failed: %s", bucket.id, e)
        self._activity_analyzer.add_input_events(input_events_for_analysis)
        # Latest real-input instant (end-time of the newest input event) as the
        # cross-platform human-presence anchor for foreground-activity credit.
        last_input_at: Optional[datetime] = None
        for ev in input_events_for_analysis:
            end = ev.timestamp + timedelta(seconds=ev.duration)
            if last_input_at is None or end > last_input_at:
                last_input_at = end
        return _SyncCycleContext(
            has_input_data=len(input_events_for_analysis) > 0,
            last_input_at=last_input_at,
        )

    def _sync_window_buckets(
        self, window_buckets: list, stats: "SyncStats", cycle: "_SyncCycleContext"
    ) -> tuple[list, list, list]:
        """Sync window buckets with gap-filling, call detection, and per-event
        transform. Returns (transformed_events, call_events, pending_checkpoints).

        ``cycle`` carries the per-cycle activity context; ``cycle.afk_events`` is
        refreshed per bucket below and read downstream in the transform path.
        """
        all_events: list[dict] = []
        call_events: list[dict] = []
        pending_checkpoints: list[tuple[str, datetime, Optional[int]]] = []
        for bucket in window_buckets:
            try:
                raw_events, _ = self._fetch_bucket_events(bucket.id, stats)
                if raw_events:
                    # Fetch AFK data covering the same time range
                    earliest = raw_events[0].timestamp
                    latest_ev = raw_events[-1]
                    latest_end = latest_ev.timestamp + timedelta(seconds=latest_ev.duration)
                    afk_events = self._get_afk_events_for_range(earliest, latest_end)

                    # Store AFK events so _transform_event can check idle status
                    cycle.afk_events = afk_events

                    filled = self._fill_window_gaps(raw_events, afk_events, bucket_id=bucket.id)
                    stats.gaps_filled += filled

                    # Feed raw events to call detector
                    if self._call_detector:
                        for ev in raw_events:
                            ce = self._call_detector.process_event(
                                app=ev.app or "",
                                title=ev.title or "",
                                url=ev.url,
                                timestamp=ev.timestamp,
                                duration=ev.duration,
                            )
                            if ce:
                                call_events.append(self._make_call_bf_event(ce))

                    transformed, checkpoint = self._transform_and_checkpoint(
                        raw_events, bucket.id, bucket.type, stats, cycle
                    )
                    all_events.extend(transformed)
                    if checkpoint:
                        pending_checkpoints.append(checkpoint)
                stats.buckets_synced += 1
            except AWClientError as e:
                stats.errors.append(f"Failed to sync bucket {bucket.id}: {e}")
        return all_events, call_events, pending_checkpoints

    def _send_and_advance_checkpoints(
        self, all_events: list, pending_checkpoints: list, stats: "SyncStats"
    ) -> None:
        """Send the cycle's events, then advance checkpoints per-bucket. Only
        hold back checkpoints for buckets that had events queued (partial
        failure). Previously this was all-or-nothing: if ANY event was queued,
        NO checkpoint advanced, causing indefinite re-fetch of already-sent
        events that slowly filled the dedup LRU cache. Extracted from sync()
        verbatim — behaviour unchanged.
        """
        if all_events:
            pre_queued = stats.events_queued
            self._send_events(all_events, stats)
            if stats.events_queued == pre_queued:
                # Full success — advance all checkpoints
                for bucket_id, ts, event_id in pending_checkpoints:
                    self.queue.set_checkpoint_forward(bucket_id, ts, event_id)
            else:
                # Partial failure — advance only buckets whose events
                # were all sent (none queued).
                for bucket_id, ts, event_id in pending_checkpoints:
                    if bucket_id not in stats.queued_bucket_ids:
                        self.queue.set_checkpoint_forward(bucket_id, ts, event_id)
        elif pending_checkpoints:
            # All events were dedup-filtered (already sent); safe to advance.
            for bucket_id, ts, event_id in pending_checkpoints:
                self.queue.set_checkpoint_forward(bucket_id, ts, event_id)

    @staticmethod
    def _local_day_iso() -> str:
        """Local calendar date as ISO ``YYYY-MM-DD`` — the key the daily time
        counter and counted-time persistence are scoped by."""
        return datetime.now().astimezone().date().isoformat()

    def _maybe_rollover_counted_day(self) -> None:
        """On a local-day rollover, reset the per-day dedup cache and reload
        (which prunes persisted counted-time for days we will never replay).

        Without this the prune only happened at process start, so an agent left
        running across midnight accumulated a day's counted-time rows in the
        persistent store every day it stayed up. Cheap when the day is unchanged
        (a single string compare), so safe to call every sync cycle.
        """
        today = self._local_day_iso()
        with self._cache_lock:
            if today == self._counted_cache_day:
                return
            self._counted_cache_day = today
            # Yesterday's (bucket_id, event_id) dedup entries are dead — the
            # daily total resets at midnight, so drop them before reloading.
            self._time_cache.clear()
        logger.info("Local day rolled over to %s — pruning counted-time cache", today)
        self._load_counted_time_cache(today)

    def _load_counted_time_cache(self, day: Optional[str] = None) -> None:
        """Repopulate ``_time_cache`` from persisted per-event counted-seconds
        for the current local day.

        Without this, after a restart the in-memory dedup cache is empty, so
        ``prev_counted`` reads as 0 and the next sync re-adds each replayed
        event's full duration to the daily total — double-counting the tray's
        active time (badly so when the start-of-day backlog reconcile replays
        the whole day). Persisted counts make the replay a no-op (delta == 0)
        while still re-sending events to the server (which dedups by event id).

        Tolerant of a mocked/partial queue: a non-dict result is ignored so
        existing unit tests with ``Mock`` queues are unaffected.
        """
        getter = getattr(self.queue, "get_counted_times", None)
        if not callable(getter):
            return
        try:
            today = day or self._local_day_iso()
            persisted = getter(today)
            if not isinstance(persisted, dict):
                return
            with self._cache_lock:
                for (bucket_id, event_id), seconds in persisted.items():
                    self._time_cache[(bucket_id, event_id)] = float(seconds)
            # Housekeeping: drop counts from days we will never replay.
            pruner = getattr(self.queue, "prune_counted_time", None)
            if callable(pruner):
                pruner(today)
            if persisted:
                logger.info(
                    "Restored counted-time for %d events (day %s)",
                    len(persisted),
                    today,
                )
        except Exception as e:  # never let cache restore block startup
            logger.warning("Counted-time cache restore failed: %s", e)

    def _persist_counted_time(
        self, bucket_id: str, event_id: str, counted_seconds: float, day: str
    ) -> None:
        """Write-through the cumulative counted-seconds for one event so the
        dedup survives a restart. Best-effort; never raises into the sync loop."""
        setter = getattr(self.queue, "set_counted_time", None)
        if not callable(setter):
            return
        try:
            setter(bucket_id, event_id, counted_seconds, day)
        except Exception as e:
            logger.debug("Counted-time persist failed for %s/%s: %s", bucket_id, event_id, e)

    def _reconcile_backlog(self, buckets: list) -> None:
        """Enqueue the whole current work day's AW events into the offline queue
        so they drain in the background.

        The old approach rewound the forward checkpoint and relied on the next
        fetch to re-send. But AW returns events NEWEST-first under a ``limit``,
        so the fetch grabbed the most-recent batch and the checkpoint snapped
        back to ~now — an older mid-day gap was never reached. And the offline
        queue only ever held send FAILURES, so "0 queued" could read clean while
        locally-captured events sat un-synced: there was no reconciliation
        between AW and prod (furdui.iancu, 2026-06-16: ~1000 events at
        05:00-07:00 UTC stranded while the queue showed 0).

        This walks start-of-work-day -> now OLDEST-first (paged, since the API is
        newest-first) and ENQUEUES every event. The backend upserts by AW event
        id, so re-enqueuing already-synced events is deduped — safe. The queue's
        size() now reflects the true backlog and ticks down to 0 as
        _process_queue drains it, so "0 queued" finally means "everything reached
        prod".

        Time tracking during replay: non-window buckets pass skip_time_tracking
        so the replay never touches the daily total. Window/web buckets go
        through _transform_window_event_with_timeout, which dedups per
        (bucket_id, event_id) against the persisted counted-time cache — so an
        already-counted event is a no-op, while a genuinely-stranded event (the
        whole reason reconcile exists) is counted once, here, correctly. There
        is no double-count: both the live sync and this replay share that cache.

        Context: this runs BEFORE _prepare_input_analysis, so it builds its own
        fresh _SyncCycleContext (has_input_data=False, no AFK). Replay window
        events are therefore classified "active" (we can't penalize activity
        without input data — and the server-side billing metric is
        tracked_seconds, not active_seconds). That is the intended fallback.
        """
        # Don't pile the whole day on top of a backlog that's already queued and
        # draining. Re-enqueuing would duplicate events (a batch carrying the same
        # id twice has the server report processed < len) and balloon the queue —
        # repeated Sync Now / restarts drove it from 3k to 11k on 2026-06-16. Let
        # the pending backlog drain first; the next reconcile covers anything new.
        try:
            pending = self.queue.size()
        except Exception:
            pending = 0
        if pending > self.config.sync.batch_size:
            logger.info(
                "Backlog reconcile skipped: %d events already queued and draining", pending
            )
            return

        day_start = (
            datetime.now()
            .astimezone()
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .astimezone(timezone.utc)
        )
        now = datetime.now(timezone.utc)
        # Fresh, empty context: replay runs before input analysis, so window
        # events are classified "active" (see docstring). Deterministic — never
        # inherits a prior cycle's state.
        cycle = _SyncCycleContext()
        enqueued = 0
        for bucket in buckets:
            cursor = day_start
            while cursor < now:
                window_end = min(now, cursor + self._BACKLOG_WINDOW)
                try:
                    events = self.aw.get_events(
                        bucket.id,
                        start=cursor,
                        end=window_end,
                        limit=self._BACKLOG_FETCH_LIMIT,
                    )
                except AWClientError as e:
                    logger.warning("Backlog reconcile fetch failed for %s: %s", bucket.id, e)
                    break  # move on to the next bucket
                cursor = window_end
                if not events:
                    continue
                events.sort(key=lambda e: e.timestamp)  # AW is newest-first
                batch: list[dict] = []
                for event in events:
                    if _is_window_like(bucket.type):
                        batch.extend(
                            self._transform_window_event_with_timeout(
                                event, bucket.id, bucket.type, cycle
                            )
                        )
                    else:
                        transformed = self._transform_event(
                            event, bucket.id, bucket.type, skip_time_tracking=True, cycle=cycle
                        )
                        if transformed:
                            batch.append(transformed)
                if batch:
                    enqueued += self.queue.enqueue(batch)
        if enqueued:
            logger.info(
                "Backlog reconcile: enqueued %d work-day events (since %s) to drain via the queue",
                enqueued,
                day_start.isoformat(),
            )

    def _within_working_hours(self, event: AWEvent) -> bool:
        """True if the event's start may be uploaded — i.e. it falls inside the
        working-hours window (or the schedule is unrestricted).

        This is now only the SECOND line of defence. Capture itself is suppressed
        outside the window (AppController._apply_capture_policy stops the
        trackers), so in the normal case no out-of-hours event exists to filter.
        This gate still runs to catch anything already sitting in the local store
        or the offline queue from before the window closed.

        Delegates to WorkingHoursConfig.allows() so capture and upload can never
        disagree about what "outside working hours" means. It deliberately does
        NOT use getattr-with-a-default: reading a missing field as "unrestricted"
        is precisely how this silently failed open.
        """
        return self.config.working_hours.allows(event.timestamp)

    def _fetch_bucket_events(
        self, bucket_id: str, stats: SyncStats
    ) -> tuple[list[AWEvent], datetime]:
        """Fetch events from a bucket with lookback window.

        Returns (events, lookback_start) — events sorted oldest-first.
        """
        now = datetime.now(timezone.utc)
        checkpoint = self.queue.get_checkpoint(bucket_id)
        if checkpoint is None:
            # First sync for this bucket — start from now so we don't
            # retroactively sync old AW events that accumulated before
            # BetterFlow was running.  Persist immediately so the next
            # cycle uses the 2-min lookback instead of resetting to "now".
            checkpoint = now
            self.queue.set_checkpoint(bucket_id, checkpoint)
            lookback_start = checkpoint
        else:
            lookback_start = checkpoint - timedelta(minutes=2)

        # Page OLDEST-first through a bounded forward window.
        #
        # AW returns events NEWEST-first, so fetching [checkpoint, now] with a
        # plain `limit` grabs the most RECENT batch and strands any older
        # un-synced events further back. That silently defeats the backlog
        # reconcile: it rewinds the checkpoint to recover a mid-day gap, but the
        # newest-first fetch snaps the checkpoint straight back to ~now via
        # max(events) below, so the gap is never reached. (furdui.iancu,
        # 2026-06-16: ~1000 events at 05:00-07:00 UTC were unrecoverable by
        # Sync Now / restart for exactly this reason.)
        #
        # Capping the fetch to a forward window and taking the OLDEST batch makes
        # successive cycles walk through the backlog and cover the gap. In steady
        # state the window is just [checkpoint-2m, now] (fetch_end == now) and
        # behaviour is unchanged — including the 2-min lookback that re-sends
        # heartbeat-grown durations.
        fetch_end = min(now, lookback_start + self._BACKLOG_WINDOW)
        events = self.aw.get_events(
            bucket_id, start=lookback_start, end=fetch_end, limit=self._BACKLOG_FETCH_LIMIT
        )
        # AW returns newest-first; sort oldest-first so the slice below keeps the
        # OLDEST events (and for deterministic gap-filling).
        events.sort(key=lambda e: e.timestamp)

        # Empty leading window (e.g. an overnight quiet stretch right after the
        # checkpoint): advance past it so we don't re-poll the same empty span
        # forever while a backlog waits beyond it.
        #
        # "Empty" must mean "no NEW events past the checkpoint" — NOT a literally
        # empty slice. The 2-min lookback (above) always re-includes the pre-gap
        # tail event, so an `if not events` test never fires after an idle gap
        # longer than _BACKLOG_WINDOW: the checkpoint pins at the last pre-gap
        # event, the fetch window stays parked ~2h behind it forever, and events
        # captured after the gap are never read. (PiratesMac / device 14,
        # 2026-06-23: window+input frozen at the prior evening's last event after
        # an overnight gap; the morning's events sat captured-but-unsent until a
        # checkpoint jump skipped them.) Gate the jump on events strictly AFTER
        # the checkpoint so an idle gap can't pin the cursor. In steady state
        # (fetch_end == now) the jump never fires, so the lookback's heartbeat-
        # grown re-send is preserved.
        # Walk ALL consecutive empty windows inside this cycle (bounded), not
        # one per cycle: a provably-empty span is safe to skip immediately, and
        # skipping it at one window per 60s cycle left a weekend-sized gap
        # uncrossed for ~30 minutes after Monday-morning startup while only the
        # afk heartbeat reached the server (device 14, 2026-08-03). Each skip
        # persists via set_checkpoint_forward, so an AWClientError mid-walk (it
        # propagates to _sync_window_buckets' handler) or the per-cycle cap
        # loses no progress — the next cycle resumes from the last empty span.
        new_events = [e for e in events if e.timestamp > checkpoint]
        empty_windows_skipped = 0
        walk_deadline = time.monotonic() + self._BACKLOG_WALK_BUDGET_SECONDS
        while (
            not new_events
            and fetch_end < now
            and empty_windows_skipped < self._BACKLOG_MAX_EMPTY_WINDOWS_PER_CYCLE
            and time.monotonic() < walk_deadline
        ):
            self.queue.set_checkpoint_forward(bucket_id, fetch_end)
            checkpoint = fetch_end
            lookback_start = checkpoint - timedelta(minutes=2)
            fetch_end = min(now, lookback_start + self._BACKLOG_WINDOW)
            events = self.aw.get_events(
                bucket_id, start=lookback_start, end=fetch_end, limit=self._BACKLOG_FETCH_LIMIT
            )
            events.sort(key=lambda e: e.timestamp)
            new_events = [e for e in events if e.timestamp > checkpoint]
            empty_windows_skipped += 1
        if not new_events and fetch_end < now:
            # Cap reached with the span still empty — resume next cycle.
            return [], lookback_start

        if len(events) > self.config.sync.batch_size:
            events = events[: self.config.sync.batch_size]
        stats.events_fetched += len(events)
        return events, lookback_start

    def _transform_and_checkpoint(
        self,
        events: list[AWEvent],
        bucket_id: str,
        bucket_type: str,
        stats: SyncStats,
        cycle: "_SyncCycleContext",
    ) -> tuple[list[dict], Optional[tuple[str, datetime, Optional[int]]]]:
        """Transform events to BetterFlow format and compute pending checkpoint.

        Returns (transformed_events, pending_checkpoint).
        The caller must commit the checkpoint AFTER send_events succeeds.

        Skips events already sent with unchanged duration (dedup).
        Re-sends if duration has grown (heartbeat extension).
        Returns (transformed_events, pending_checkpoint) — caller commits the
        checkpoint only after a successful send (N5). ``cycle`` is required so a
        new bucket-processing path can't silently classify against an empty
        context (this is the window-classification entry point).
        """
        # Feed window events to activity analyzer for window change detection.
        # Uses the RAW events (pre-coalesce) so genuine window switches are
        # still visible to change detection.
        if bucket_type in (BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT):
            self._activity_analyzer.add_window_events(events)

        # Coalesce runs of sub-threshold same-window fragments so an AW
        # heartbeat-merge failure doesn't cost the whole span's per-app
        # attribution to the flicker filter (see _coalesce_window_flickers).
        # The checkpoint below is computed from the ORIGINAL events, so
        # coalescing cannot skip, re-fetch, or double-send anything. Passing the
        # bucket's last-sent checkpoint keeps a multi-cycle flicker run from being
        # re-merged over time already delivered (cross-cycle double-count).
        checkpoint_events = events
        if _is_window_like(bucket_type):
            events = self._coalesce_window_flickers(
                events, self.queue.get_checkpoint(bucket_id)
            )

        transformed = []
        for event in events:
            # Dedup: skip if already sent with same duration.
            # For gap-filled events, also accept the original AW duration
            # (gap-filling is deterministic, so the extended version was
            # already sent on a prior cycle).
            cache_key = (bucket_id, event.id)
            with self._cache_lock:
                prev_duration = self._sent_cache.get(cache_key)
            if prev_duration is not None and abs(event.duration - prev_duration) < 0.5:
                stats.events_filtered += 1
                continue
            # If we previously sent a gap-filled (extended) duration for this
            # event and AW still returns a shorter duration, skip it — the
            # server already has the longer, more accurate version. Only
            # re-send when AW's duration catches up past what we sent.
            if prev_duration is not None and event.duration < prev_duration - 0.5:
                with self._cache_lock:
                    is_gap_filled = self._gap_filled_originals.get((bucket_id, event.id)) is not None
                if is_gap_filled:
                    stats.events_filtered += 1
                    continue

            # Working-hours gate: for restricted relationships (B2E / Trainee)
            # the agent must NOT upload activity outside the enforced window
            # (e.g. before 08:00 / after 22:00, or on non-working days). The
            # checkpoint still advances past skipped events (computed from all
            # fetched events below), so they are never re-fetched or sent.
            if not self._within_working_hours(event):
                stats.events_filtered += 1
                continue

            is_window = _is_window_like(bucket_type)
            if is_window:
                stats.window_seen += 1
                transformed_events = self._transform_window_event_with_timeout(
                    event, bucket_id, bucket_type, cycle
                )
            else:
                transformed_event = self._transform_event(
                    event, bucket_id, bucket_type, cycle=cycle
                )
                transformed_events = [transformed_event] if transformed_event else []

            if transformed_events:
                transformed.extend(transformed_events)
                with self._cache_lock:
                    self._sent_cache[cache_key] = event.duration
                if is_window:
                    stats.window_sent += 1
            else:
                stats.events_filtered += 1
                # Attribute a dropped WINDOW event so a "no window data" stall can
                # be classified as filter-side (vs the watcher going quiet). The
                # reasons mirror _transform_event's drop conditions.
                if is_window:
                    app = event.app
                    if app and app in self.config.privacy.exclude_apps:
                        stats.window_drop_excluded_apps.add(app)
                    elif event.duration < self.config.sync.min_window_event_seconds:
                        stats.window_drop_short += 1

        # Batch fraud assessment: one call per cycle instead of per-event.
        # The fraud detector's underlying data changes once per record_window_metrics()
        # call (guarded by sequence counter), so assessing once is equivalent.
        if (
            transformed
            and _is_window_like(bucket_type)
            and cycle.has_input_data
        ):
            last_event = events[-1]  # sorted oldest-first, use newest for assessment
            total_active = sum(ev.get("duration", 0) for ev in transformed)
            fraud = self._activity_analyzer.get_fraud_assessment(
                last_event.timestamp, app=last_event.app,
                active_seconds=total_active,
            )
            for ev in transformed:
                if "activity_metrics" in ev:
                    ev["fraud_score"] = fraud.score
                    ev["fraud_signals"] = fraud.signals
                    ev["activity_metrics"].update(fraud.extra_metrics)

        pending_checkpoint = None
        if checkpoint_events:
            newest = max(checkpoint_events, key=lambda e: e.timestamp)
            pending_checkpoint = (bucket_id, newest.timestamp, newest.id)

        return transformed, pending_checkpoint

    def _transform_window_event_with_timeout(
        self,
        event: AWEvent,
        bucket_id: str,
        bucket_type: str,
        cycle: "_SyncCycleContext",
    ) -> list[dict]:
        """Transform only the countable slices of a long window/web event."""
        event_start = event.timestamp
        event_end = event.timestamp + timedelta(seconds=event.duration)

        # NO client-side dropping. Always upload the full window/web event with
        # its real duration; active-vs-idle is decided SERVER-SIDE from the AFK
        # stream. The previous inactivity-cutoff + AFK-overlap + input-timeout
        # filter discarded genuinely-active work whenever input detection lagged
        # or an event arrived zero-duration, stranding hours of real activity on
        # users' machines (fleet incident, 2026-06-15). The client must never
        # decide to throw real activity away — send everything, every cycle.
        active_ranges = [(event_start, event_end)]

        transformed: list[dict] = []
        total_active_duration = 0.0
        for idx, (segment_start, segment_end) in enumerate(active_ranges):
            duration = round((segment_end - segment_start).total_seconds(), 2)
            if duration <= 0:
                continue
            total_active_duration += duration

            segment_event = AWEvent(
                id=event.id,
                timestamp=segment_start,
                duration=duration,
                data=event.data,
            )
            transformed_event = self._transform_event(
                segment_event,
                bucket_id,
                bucket_type,
                forced_activity_state=None if cycle.has_input_data else "active",
                custom_event_id=f"{event.id}:{idx}",
                skip_time_tracking=True,  # tracked per-event below
                cycle=cycle,
            )
            if transformed_event:
                transformed.append(transformed_event)

        # Track time at the EVENT level (not per-segment) to avoid
        # double-counting or undercounting when segmentation changes
        # across cycles (e.g. AFK splits differ between syncs).
        if transformed and total_active_duration > 0:
            event_date = event.timestamp.astimezone().date()
            time_key = (bucket_id, str(event.id))
            time_delta = 0.0
            with self._cache_lock:
                prev_counted = self._time_cache.get(time_key, 0.0)
                delta = total_active_duration - prev_counted
                if delta > 0:
                    time_delta = delta
                    self._time_cache[time_key] = total_active_duration
            # Persist outside the cache lock to avoid blocking dedup with I/O
            # (matches _classify_and_count_window's time_delta pattern).
            if time_delta > 0:
                self._time_tracker.add_active_time(time_delta, event_date)
                # Persist the new cumulative so a restart/reconcile doesn't
                # re-count this event (write-through, outside the cache lock).
                self._persist_counted_time(
                    bucket_id, str(event.id), total_active_duration,
                    event_date.isoformat(),
                )

        return transformed

    def _sync_bucket(
        self, bucket_id: str, bucket_type: str, stats: SyncStats, cycle: "_SyncCycleContext"
    ) -> tuple[list[dict], Optional[tuple[str, datetime, Optional[int]]]]:
        """Sync events from a single bucket.

        ActivityWatch extends the duration of the current (most recent) event
        via heartbeats.  If we only fetch events *after* the checkpoint we miss
        that growing duration.  To fix this we look back a short overlap window
        before the checkpoint so recently-synced events whose duration has
        grown are re-sent with the updated value.  The backend uses the AW
        event id to upsert, so the duration is simply patched in place.

        Returns (transformed_events, pending_checkpoint).
        """
        events, _ = self._fetch_bucket_events(bucket_id, stats)
        if not events:
            return [], None
        if _is_afk_like(bucket_type):
            # Collapse heartbeat-merge corruption before it reaches the backend
            # (see _collapse_afk_duplicates). Without this, a misbehaving idle
            # tracker's duplicate 'afk' rows are synced raw and billed as idle.
            events = self._collapse_afk_duplicates(events)
        return self._transform_and_checkpoint(events, bucket_id, bucket_type, stats, cycle)

    @staticmethod
    def _collapse_afk_duplicates(events: list[AWEvent]) -> list[AWEvent]:
        """Merge overlapping same-status AFK events into one span.

        A misbehaving idle tracker — or a server heartbeat-merge failure — can
        emit many AFK rows that share a start timestamp with growing durations
        (observed: 29 'afk' rows all starting at the same instant, furdui.iancu
        2026-06-17). This happens even with a single tracker, so it is distinct
        from the orphan-tracker bug. Synced raw, the overlapping rows blanket
        the period with redundant idle and poison both active-time
        classification and the backend's billing. Collapsing overlapping
        same-status spans to one (earliest-start, latest-end) event removes the
        corruption while preserving the real timeline. Source events are not
        mutated (new events are built via dataclasses.replace).
        """
        if len(events) <= 1:
            return events
        ordered = sorted(
            events,
            key=lambda e: (e.timestamp, e.timestamp + timedelta(seconds=e.duration)),
        )
        collapsed: list[AWEvent] = []
        for ev in ordered:
            ev_end = ev.timestamp + timedelta(seconds=ev.duration)
            if collapsed:
                last = collapsed[-1]
                last_end = last.timestamp + timedelta(seconds=last.duration)
                if last.status == ev.status and ev.timestamp <= last_end:
                    # Overlap with the same status → extend the existing span.
                    if ev_end > last_end:
                        collapsed[-1] = dataclasses.replace(
                            last, duration=(ev_end - last.timestamp).total_seconds()
                        )
                    continue
            collapsed.append(ev)
        return collapsed

    # Two same-window fragments this far apart (or closer) are treated as one
    # continuous focus. AW polls every ~1-2s, so a couple of seconds of slack
    # bridges normal polling jitter without merging across a real gap (a real
    # gap means a DIFFERENT window was focused, which breaks the run anyway).
    _WINDOW_COALESCE_GAP_TOLERANCE_S = 2.0

    @staticmethod
    def _coalesce_window_flickers(
        events: list[AWEvent], checkpoint: Optional[datetime] = None
    ) -> list[AWEvent]:
        """Merge a run of consecutive, time-contiguous events that describe the
        SAME window (app + title + url) into one event.

        ActivityWatch is supposed to heartbeat-merge successive polls of an
        unchanged window into a single growing event. When that merge fails it
        emits the focus as dozens of sub-second/second fragments (observed:
        bursts of ~100 window events all under the 5s flicker filter — Sachi
        device 16, recurring). Each fragment then falls under
        ``min_window_event_seconds`` in ``_transform_event`` and is dropped, so
        the WHOLE span's per-app attribution is lost even though the watcher
        was producing data the entire time.

        Merging the run restores it as one event whose duration is the real
        span, which clears the filter. This only ever combines fragments that
        are provably the same window (identical app/title/url, back-to-back in
        time) — exactly analogous to ``_collapse_afk_duplicates`` — so it can
        only recover real attribution, never invent it. Billing is unaffected
        either way (time comes from the AFK/input streams, not window events).

        Source events are not mutated (new events via ``dataclasses.replace``);
        the merged event keeps the FIRST fragment's id, so the ``_sent_cache``
        dedup keys deterministically and the checkpoint (computed by the caller
        from the ORIGINAL event list) is unaffected.

        Only fragments STRICTLY AFTER ``checkpoint`` (the bucket's last-sent
        position) are merged. A flicker run that outlives the 2-minute re-fetch
        lookback would otherwise be re-merged each cycle under a SHIFTED first-
        fragment id (its true start ages out of the lookback), evading the
        (id, duration) dedup and re-sending time already delivered last cycle —
        double-counting the very per-app attribution this recovers. Fragments
        at/before the checkpoint pass through unmerged for the normal per-event
        dedup / min_window filter to drop (they were already sent). checkpoint is
        None on the first sync for a bucket, which merges the whole run.

        (Narrow known edge: a passed-through already-sent fragment is dropped on
        replay by the min_window filter because flicker fragments are sub-5s by
        definition and were never cached under their own id. A same-window run
        containing an individual fragment AT/ABOVE min_window_event_seconds that
        isn't the group's first fragment could re-send once — outside the observed
        flicker shape, and billing-neutral, so not tracked with per-fragment state.)
        """
        if len(events) <= 1:
            return events
        if checkpoint is not None:
            already_sent = sorted(
                (e for e in events if e.timestamp <= checkpoint),
                key=lambda e: e.timestamp,
            )
            to_merge = [e for e in events if e.timestamp > checkpoint]
        else:
            already_sent = []
            to_merge = events
        if len(to_merge) <= 1:
            return already_sent + to_merge
        tolerance = timedelta(seconds=SyncEngine._WINDOW_COALESCE_GAP_TOLERANCE_S)
        ordered = sorted(to_merge, key=lambda e: e.timestamp)
        # Merge ONLY among the post-checkpoint fragments (never with an
        # already-sent one), so a new run stays adjacent to — not overlapping —
        # what was delivered last cycle.
        merged: list[AWEvent] = []
        for ev in ordered:
            if merged:
                last = merged[-1]
                last_end = last.timestamp + timedelta(seconds=last.duration)
                same_window = (
                    last.app == ev.app
                    and last.title == ev.title
                    and last.url == ev.url
                )
                if same_window and last.timestamp <= ev.timestamp <= last_end + tolerance:
                    ev_end = ev.timestamp + timedelta(seconds=ev.duration)
                    if ev_end > last_end:
                        merged[-1] = dataclasses.replace(
                            last, duration=(ev_end - last.timestamp).total_seconds()
                        )
                    # else: ev is fully contained in last — drop the duplicate.
                    continue
            merged.append(ev)
        return already_sent + merged

    def _get_afk_events_for_range(
        self, start: datetime, end: datetime
    ) -> list[AWEvent]:
        """Fetch AFK events covering [start, end] from all AFK buckets.

        Looks back up to 10 minutes before ``start`` to catch AFK events
        whose timestamp predates the query range but whose duration extends
        into it (ActivityWatch filters by timestamp only). 10 minutes is
        more than enough to bridge any AFK heartbeat gap; larger lookbacks
        risk hitting the limit=5000 cap on busy machines.
        """
        try:
            afk_buckets = self.aw.get_afk_buckets()
        except AWClientError:
            return []

        # Look back to capture AFK events that started earlier but are
        # still active during [start, end].
        lookback_start = start - timedelta(minutes=10)

        all_afk: list[AWEvent] = []
        for bucket in afk_buckets:
            try:
                events = self.aw.get_events(
                    bucket.id, start=lookback_start, end=end, limit=5000
                )
                if len(events) == 5000:
                    logger.warning(
                        f"AFK bucket {bucket.id} returned max 5000 events; "
                        f"activity classification for tail of window may be inaccurate"
                    )
                all_afk.extend(events)
            except AWClientError as e:
                logger.debug("AFK bucket %s fetch failed: %s", bucket.id, e)

        all_afk.sort(key=lambda e: e.timestamp)
        # Collapse heartbeat-merge corruption so active-time classification
        # isn't poisoned by duplicate overlapping 'afk' spans.
        return self._collapse_afk_duplicates(all_afk)

    @staticmethod
    def _is_active_during(
        start: datetime, end: datetime, afk_events: list[AWEvent]
    ) -> bool:
        """Check that the entire [start, end) interval is covered by not-afk.

        Walks AFK events chronologically.  Returns False if any portion of the
        interval is not covered by a ``not-afk`` event.
        """
        if not afk_events:
            return False

        cursor = start
        for ev in afk_events:
            ev_start = ev.timestamp
            ev_end = ev.timestamp + timedelta(seconds=ev.duration)

            # Skip events that end before our cursor
            if ev_end <= cursor:
                continue
            # If this event starts after the cursor, there's an uncovered gap
            if ev_start > cursor:
                return False
            # Event must be not-afk to count as active
            if ev.status != "not-afk":
                return False
            # Advance cursor to the end of this event
            cursor = ev_end
            if cursor >= end:
                return True

        # If we exhausted events without reaching ``end``, gap is uncovered
        return cursor >= end

    @staticmethod
    def _status_at(timestamp: datetime, afk_events: list[AWEvent]) -> str | None:
        """Return the AFK status covering ``timestamp``, if any."""
        for ev in afk_events:
            ev_start = ev.timestamp
            ev_end = ev.timestamp + timedelta(seconds=ev.duration)
            if ev_start <= timestamp < ev_end:
                return ev.status

        return None

    def _fill_window_gaps(
        self,
        window_events: list[AWEvent],
        afk_events: list[AWEvent],
        max_gap_seconds: float = 300.0,
        bucket_id: str = "",
    ) -> int:
        """Extend window event durations to cover gaps confirmed by AFK data.

        Replaces events in ``window_events`` list (sorted oldest-first).
        Returns count of gaps filled.
        """
        if len(window_events) < 2 or not afk_events:
            return 0

        filled = 0
        for i in range(len(window_events) - 1):
            current = window_events[i]
            next_ev = window_events[i + 1]

            current_end = current.timestamp + timedelta(seconds=current.duration)
            gap_seconds = (next_ev.timestamp - current_end).total_seconds()

            # Skip negligible or too-large gaps
            if gap_seconds < 2.0 or gap_seconds > max_gap_seconds:
                continue

            # Don't fill across app switches
            if current.app != next_ev.app:
                continue

            # Verify user was active during the entire gap
            if not self._is_active_during(current_end, next_ev.timestamp, afk_events):
                continue

            old_duration = current.duration
            new_duration = (next_ev.timestamp - current.timestamp).total_seconds()
            # AWEvent is frozen; replace in list with updated copy
            window_events[i] = AWEvent(
                id=current.id,
                timestamp=current.timestamp,
                duration=new_duration,
                data=current.data,
            )
            # Pre-seed sent cache with original AW duration so next sync's
            # dedup check sees original→original (unchanged) and skips.
            # The gap-filled event is already sent with extended duration.
            with self._cache_lock:
                self._gap_filled_originals[(bucket_id, current.id)] = old_duration
            filled += 1

        if filled:
            logger.debug(f"Filled {filled} window gap(s) across {len(window_events)} events")

        return filled

    def _transform_event(
        self,
        event: AWEvent,
        bucket_id: str,
        bucket_type: str,
        forced_activity_state: Optional[str] = None,
        custom_event_id: Optional[str] = None,
        skip_time_tracking: bool = False,
        *,
        cycle: Optional["_SyncCycleContext"] = None,
    ) -> Optional[dict]:
        """Transform an ActivityWatch event to BetterFlow format.

        ``cycle`` (keyword-only) carries the per-cycle activity context read
        during window classification. Production callers always pass it; when
        omitted it defaults to an empty context (no input, no AFK) — a safe,
        deterministic default, never stale cross-cycle state.

        Sends raw data to the server — the backend handles privacy
        (title hashing, URL domain extraction) based on device settings.
        """
        if cycle is None:
            cycle = _SyncCycleContext()
        privacy = self.config.privacy

        # Skip excluded apps (client-side — sensitive apps never leave the machine)
        app = event.app
        if app and app in privacy.exclude_apps:
            return None

        # Reject non-finite durations (NaN/inf from corrupt AW data)
        if not math.isfinite(event.duration):
            logger.warning(f"Skipping event id={event.id} with non-finite duration={event.duration}")
            return None

        # Skip very short events.
        # Window/web events use a configurable minimum (default 5s) to filter
        # sub-second flickers. AFK/input events keep the 0.5s floor since they
        # have legitimately short durations (e.g. input telemetry at 1s).
        if _is_window_like(bucket_type):
            if event.duration < self.config.sync.min_window_event_seconds:
                return None
        elif event.duration < 0.5:
            return None

        # Build data object
        result_bucket_type = bucket_type
        data = {}

        if _is_window_like(bucket_type):
            self._populate_window_data(event, app, data)
        elif _is_afk_like(bucket_type):
            data["status"] = event.status
            # AFK periods are sent with their real AFK bucket_type — NOT relabeled
            # as "break". A break is an intentional, user-initiated pause
            # (_send_break_event -> "break_time"); blanket-relabeling every
            # away-from-keyboard stretch as a break turned ordinary no-input work
            # (reading, meetings, watching a screen) into phantom "Break" cards
            # for people who never took a break. The backend already classifies
            # long AFK spans as Idle (TimelineCardBuilder::appendIdleFromAfk) and
            # uses AFK status for active-hours, so leaving bucket_type untouched
            # routes this to the correct "Idle" category.
        elif bucket_type == BUCKET_TYPE_INPUT:
            # Input events track keystrokes, clicks, scrolls for fraud detection.
            # The MacOSInputWatcher tags each batch with the frontmost app so the
            # server can attribute counts to the correct per-app aggregate.
            data["presses"] = event.presses
            data["clicks"] = event.clicks
            data["scrolls"] = event.scrolls
            if event.app:
                data["app"] = event.app
            bundle = event.data.get("bundle")
            if bundle:
                data["bundle"] = bundle

        # Clamp future timestamps and reject negative durations
        now = datetime.now(timezone.utc)
        timestamp = event.timestamp
        if timestamp > now + timedelta(minutes=1):
            logger.warning(f"Clamping future timestamp {timestamp} to now")
            timestamp = now
        duration = max(0, round(event.duration, 2))

        result = {
            "id": custom_event_id if custom_event_id is not None else event.id,
            "timestamp": timestamp.isoformat(),
            "duration": duration,
            "bucket_id": bucket_id,
            "bucket_type": result_bucket_type,
            "data": data,
        }

        # Tag with current project if set
        result = self._stamp_project(result)

        # Add activity classification + counted time for window events
        # (fraud assessment is batched per cycle in _transform_and_checkpoint)
        if _is_window_like(bucket_type):
            self._classify_and_count_window(
                event, bucket_id, result, forced_activity_state, skip_time_tracking, cycle
            )

        return result

    def _classify_and_count_window(
        self,
        event: AWEvent,
        bucket_id: str,
        result: dict,
        forced_activity_state: Optional[str],
        skip_time_tracking: bool,
        cycle: "_SyncCycleContext",
    ) -> None:
        """Set ``result['activity_state']`` (+ metrics) for a window/web event and
        accumulate counted active time.

        ``cycle`` (has_input_data / afk_events) is passed in explicitly — no
        per-cycle state lives on the instance, so this method is safe to call
        from anywhere as long as the caller supplies the right context.
        """
        activity_state: str | None = None
        if forced_activity_state is not None:
            activity_state = forced_activity_state
            result["activity_state"] = activity_state
        elif cycle.has_input_data:
            activity_state = self._activity_analyzer.get_activity_state(event.timestamp)
            activity_metrics = self._activity_analyzer.get_raw_metrics(event.timestamp)

            result["activity_state"] = activity_state
            result["activity_metrics"] = activity_metrics.to_dict()
        else:
            # No input watcher - use AFK data to determine activity.
            event_end = event.timestamp + timedelta(seconds=event.duration)
            has_afk = bool(cycle.afk_events)

            with self._state_lock:
                afk_watcher_available = self._afk_watcher_available
            if not afk_watcher_available:
                # AFK watcher is completely down - can't classify.
                # Default to "active" since window events prove the user
                # was at the computer.
                activity_state = "active"
                logger.debug(
                    f"AFK watcher unavailable; classifying window event as active: "
                    f"event={event.timestamp.isoformat()}->{event_end.isoformat()}"
                )
            elif has_afk:
                is_active = self._is_active_during(
                    event.timestamp, event_end, cycle.afk_events
                )
                # Fallback: treat as active when event end falls inside
                # a not-afk span, even if AFK doesn't fully cover start.
                if not is_active:
                    probe_time = event_end - timedelta(milliseconds=1)
                    is_active = (
                        self._status_at(probe_time, cycle.afk_events)
                        == "not-afk"
                    )
                activity_state = "active" if is_active else "inactive"
                if not is_active:
                    logger.debug(
                        f"Window event classified inactive: "
                        f"afk_count={len(cycle.afk_events)}, "
                        f"event={event.timestamp.isoformat()}->{event_end.isoformat()}"
                    )
            else:
                # AFK watcher is running and confirms user was idle
                activity_state = "inactive"
                logger.debug(
                    f"Window event classified inactive: "
                    f"afk_count={len(cycle.afk_events)}, "
                    f"event={event.timestamp.isoformat()}->{event_end.isoformat()}"
                )
            result["activity_state"] = activity_state

        # Track counted time for any event that is not explicitly inactive.
        # This keeps live hours aligned with "time on the machine" while
        # still stopping the counter after prolonged no-input periods.
        if (
            not skip_time_tracking
            and activity_state is not None
            and activity_state != "inactive"
        ):
            event_date = event.timestamp.astimezone().date()
            event_key = str(result["id"])
            time_key = (bucket_id, event_key)
            time_delta = 0.0
            with self._cache_lock:
                prev_counted = self._time_cache.get(time_key, 0.0)
                delta = event.duration - prev_counted
                if delta > 0:
                    time_delta = delta
                    self._time_cache[time_key] = event.duration
            # Persist outside the cache lock to avoid blocking
            # dedup checks with SQLite I/O
            if time_delta > 0:
                self._time_tracker.add_active_time(time_delta, event_date)
                # Persist cumulative so a restart/reconcile doesn't re-count.
                self._persist_counted_time(
                    bucket_id, event_key, event.duration, event_date.isoformat()
                )

    def _populate_window_data(self, event: AWEvent, app: Optional[str], data: dict) -> None:
        """Fill ``data`` for a window/web event: app, title, URL (with privacy
        rules), page/app category, and display info. Extracted from
        _transform_event verbatim — behaviour unchanged."""
        privacy = self.config.privacy
        data["app"] = app[:MAX_APP_LENGTH] if app else app
        title = event.title
        data["title"] = title[:MAX_TITLE_LENGTH] if title else title
        # The bundled window watcher carries no URL. On macOS, enrich browser
        # events with the active-tab URL captured around the event's time by
        # the browser tracker. Raw URL here; the privacy block below applies
        # domain-only / full-URL rules exactly as it does for extension URLs.
        raw_url = event.url
        if (
            not raw_url
            and self._browser_tracker is not None
            and is_browser_app(app)
        ):
            event_end = event.timestamp + timedelta(seconds=event.duration)
            raw_url = self._browser_tracker.url_at(event_end.timestamp())
        if raw_url:
            url = raw_url
            # When the full URL exceeds MAX_URL_LENGTH we deliberately
            # fall back to the domain rather than silent mid-string
            # truncation — truncation can change semantics (e.g. turn a
            # safe redirect target into an attacker-controlled one).
            if privacy.collect_full_urls and len(url) <= MAX_URL_LENGTH:
                data["url"] = url
            elif privacy.collect_full_urls or privacy.domain_only_urls:
                domain = self._extract_domain(url)
                if domain and len(domain) <= MAX_URL_LENGTH:
                    data["url"] = domain

            if privacy.collect_page_category:
                data["page_category"] = self._infer_page_category(raw_url, event.title)

        if app and privacy.auto_categorize:
            category = self._get_category(app)
            should_persist = False
            if category is None:
                # DB miss - try fallback map
                category = privacy.default_categories.get(app)
                if category:
                    with self._category_cache_lock:
                        if app not in self._persisted_fallbacks:
                            self._persisted_fallbacks.add(app)
                            should_persist = True
            if should_persist:
                try:
                    self.queue.set_category(app, category, source='fallback')
                except Exception as exc:
                    logger.warning(f"Failed to persist fallback category for {app!r}: {exc}")
            if category:
                data["app_category"] = category

        if self._display_tracker is not None and privacy.track_display_info:
            ds = self._display_tracker.state
            if ds.monitor_name is not None:
                data["monitor_name"] = ds.monitor_name
            if ds.monitor_index is not None:
                data["monitor_index"] = ds.monitor_index
            if ds.desktop_id is not None:
                data["desktop_id"] = ds.desktop_id
            if ds.desktop_index is not None:
                data["desktop_index"] = ds.desktop_index

    @staticmethod
    def _extract_domain(url: str) -> Optional[str]:
        """Extract domain from URL safely."""
        try:
            parsed = urlparse(url)
            return parsed.netloc or None
        except Exception:
            return None

    # Each category resolves to a precompiled word-boundary regex. Earlier
    # entries win: "code" is checked before "review" so repo URLs aren't
    # reclassified as reviews. Word boundaries prevent substring leaks —
    # "code" won't match "decode"/"encode", "diff" won't match "different".
    _PAGE_CATEGORY_RULES: tuple[tuple[str, "re.Pattern[str]"], ...] = tuple(
        (
            category,
            re.compile(
                r"\b(?:" + "|".join(re.escape(kw) for kw in keywords) + r")\b",
                re.IGNORECASE,
            ),
        )
        for category, keywords in (
            ("code", ("github", "gitlab", "bitbucket", "repo", "pull request", "merge request")),
            ("review", ("review", "diff", "changes")),
            ("documentation", ("docs", "confluence", "notion", "wiki")),
            ("communication", ("mail", "inbox", "slack", "teams", "chat", "meet")),
            ("planning", ("jira", "asana", "trello", "linear", "backlog", "sprint")),
            ("design", ("figma", "miro", "canva", "adobe")),
        )
    )

    @classmethod
    def _infer_page_category(cls, url: Optional[str], title: Optional[str]) -> str:
        """Infer a coarse page category from URL/title."""
        haystack = f"{url or ''} {title or ''}"
        for category, pattern in cls._PAGE_CATEGORY_RULES:
            if pattern.search(haystack):
                return category
        return "other"

    def _send_status_span(
        self,
        *,
        kind: str,
        start: datetime,
        end: Optional[datetime] = None,
        queue_on_failure: bool = True,
    ) -> None:
        """Send a duration event for a state-span (break/idle/private).

        Consolidates three formerly-identical send_*_event helpers. The only
        variation between them was the ``kind`` string used in the id prefix,
        bucket_type, and data.status field.

        ``queue_on_failure=False`` is for periodic in-progress snapshots of a
        still-growing span (the per-cycle private_time refresh): a failed
        snapshot must not be queued because the next cycle re-sends the same
        event id with a longer duration, which supersedes it — queueing every
        snapshot would replay a stack of stale intermediate durations after
        an outage. Final spans (sent when the state ends) keep the default
        and are queued for offline retry.
        """
        if end is None:
            end = datetime.now(timezone.utc)

        # Status spans are pushed by IdleManager / BreakManager / SystemEventHandler,
        # NOT from inside sync(), so they never met the working-hours gate applied to
        # bucket events.
        #
        # Gating on the START alone is not enough, and an earlier version of this
        # comment claimed a fix it did not implement. IdleManager polls the OS idle
        # clock on the 60s tick, independently of every tracker we stop. So: user
        # walks away 21:45; 22:00 capture is suppressed; 23:55 they touch the
        # keyboard; clear_idle_pause() emits an idle span start=21:45 end=23:55.
        # allows(21:45) is True, so it shipped — telling the server the employee
        # became active at 23:55. Same shape for a sleep span (asleep 21:00, wake
        # 23:00) and for the growing per-cycle private_time snapshot.
        #
        # Clamp the END to the window close instead of dropping: the 21:45-22:00
        # portion is real, in-window, and ours to keep; everything past 22:00 is not.
        if not self.config.working_hours.allows(start):
            logger.debug("Dropping %s_time span starting %s: outside working hours",
                         kind, start.isoformat())
            return

        close = self.config.working_hours.window_close_after(start)
        if close is not None and end > close:
            logger.debug(
                "Clamping %s_time span end %s -> %s (working-hours close)",
                kind, end.isoformat(), close.isoformat(),
            )
            end = close

        duration = (end - start).total_seconds()
        if duration < 1:
            # Distinguish "expected sub-second guard" (scheduler race / no-op)
            # from "clock went backwards between sleep and wake" (NTP correction
            # during a long Mac sleep silently discarded the entire span until
            # this log was added). Sleep events are the only kind regularly
            # exposed to multi-hour spans across a possible NTP step.
            if duration < 0:
                logger.warning(
                    "Discarding %s_time event: end<start by %.1fs "
                    "(NTP clock correction?). start=%s end=%s",
                    kind, -duration, start.isoformat(), end.isoformat(),
                )
            return
        bucket_type = f"{kind}_time"
        event = {
            "id": f"{kind}_{int(start.timestamp())}_{id(self)}",
            "timestamp": start.isoformat(),
            "duration": round(duration, 2),
            # Stable synthetic bucket id, mirroring how call events carry
            # bf-call-detector_<host>. The offline-queue storability classifier
            # (queue.failed_event_summary / _batch_has_storable_activity) keys on
            # bucket_id presence: without one, a failed real idle/private/break
            # span was mislabeled "unstorable ... buckets=unknown" and silently
            # flushed — even though the happy path proves the server accepts these
            # spans. Giving them a bucket_id makes them first-class in every
            # bucket-keyed path, so a failed span now routes through the
            # dead-letter path instead of being dropped, and reports as bucket
            # type "bf-status" rather than "unknown".
            "bucket_id": f"bf-status_{self._hostname}",
            "bucket_type": bucket_type,
            "data": {"status": kind},
        }
        event = self._stamp_project(event)
        # bf.send_events() returns SyncResult(success=False) on network errors —
        # it does NOT raise BetterFlowClientError — so the previous `except`
        # block was unreachable and break/idle/private events were silently
        # dropped on the first offline cycle. Inspect the result instead.
        try:
            self._note_delivery_attempt()
            result = self.bf.send_events([event])
        except BetterFlowAuthError as e:
            # Auth errors are not retryable without re-login; queueing risks
            # sending under a different user's session after re-auth. Drop.
            logger.warning("Auth error sending %s event — not queued: %s", bucket_type, e)
            return
        if result.success:
            logger.info("Sent %s event (%.0fs)", bucket_type, duration)
        elif queue_on_failure:
            logger.warning("Failed to send %s event: %s — queueing", bucket_type, result.error or "unknown")
            self.queue.enqueue([event])
        else:
            # In-progress snapshot: superseded by the next cycle's re-send.
            logger.debug(
                "Failed to send %s snapshot: %s — not queued (next cycle re-sends)",
                bucket_type, result.error or "unknown",
            )

    def send_break_event(self, start: datetime, end: Optional[datetime] = None) -> None:
        """Send a break_time event covering the break duration."""
        self._send_status_span(kind="break", start=start, end=end)

    def send_idle_event(self, start: datetime, end: Optional[datetime] = None) -> None:
        """Send an idle_time event covering the idle duration."""
        self._send_status_span(kind="idle", start=start, end=end)

    def send_sleep_event(self, start: datetime, end: Optional[datetime] = None) -> None:
        """Send a sleep_time event covering a system sleep span.

        Distinct from idle_time so the server-side aggregator can tell
        "Mac was asleep" (involuntary, machine-off) apart from "user
        walked away from a running machine" (idle). Without this, both
        get rendered as "Break" in the daily activity view, which is
        misleading for overnight sleep cycles.
        """
        self._send_status_span(kind="sleep", start=start, end=end)

    def _send_private_time_event(self, start: Optional[datetime] = None) -> None:
        """Send a private_time event covering the private mode duration."""
        if start is None:
            with self._state_lock:
                start = self._private_start
        if not start:
            return
        self._send_status_span(kind="private", start=start)

    def _make_call_bf_event(
        self, call_event: "CallEvent", status: str = CALL_STATUS_COMPLETED
    ) -> dict:
        """Convert a CallEvent into a BetterFlow event dict.

        Includes a deterministic id so the server can dedupe and so the
        offline-queue partial-success path (_process_queue) can match the
        event against the server's accepted_ids list. Without an id, a
        partial server reply that omits this event would otherwise be
        ambiguous between "accepted" and "not yet acknowledged".

        ``status`` is "completed" for a call that actually ended (grace
        expiry, outage flush, shutdown) and "ongoing" for the per-cycle live
        snapshot of a still-open call — the server must be able to tell a
        growing live row from a final one.
        """
        hostname = self._hostname
        call_id = f"call_{call_event.app}_{int(call_event.start.timestamp())}"
        result: dict = {
            "id": call_id,
            "timestamp": call_event.start.isoformat(),
            "duration": call_event.duration,
            "bucket_id": f"bf-call-detector_{hostname}",
            "bucket_type": BUCKET_TYPE_CALL,
            "data": {
                "app": call_event.app,
                "call_type": call_event.call_type,
                "status": status,
            },
        }
        return self._stamp_project(result)

    def _synthesize_active_afk_event(
        self, latest_afk, afk_bucket_id: str, now: Optional[datetime] = None
    ) -> Optional[dict]:
        """Emit a synthetic ``not-afk`` span when bf-idle-tracker has frozen but
        the OS idle clock shows the user kept working.

        The server bills active-vs-idle from the uploaded AFK stream
        (see ``_transform_window_event_with_timeout``). A frozen tracker stops
        emitting fresh not-afk events, so the worked span has no AFK coverage and
        the server counts it as idle — the "Active time not advancing while the
        user works" fleet alert. #53/#56 added an OS-idle-clock fallback, but only
        in IdleManager's LOCAL pause decision; it never reaches the uploaded
        stream, so server-side active time still freezes. This closes that loop:
        when the latest AFK event is stale AND the OS idle clock proves recent
        input, we upload a not-afk span covering [freeze point, last input].

        Conservative by construction — returns None and fabricates nothing when:
          * there is no AFK event to extend from,
          * the tracker is fresh (latest event still ~covers now),
          * the OS idle clock is unavailable (Linux / ioreg failure), or
          * the OS clock says the user is genuinely idle, or the last input
            predates the freeze point (no active span to claim).

        The id is keyed on the freeze point, so while the tracker stays stuck the
        same id is re-sent with a growing duration and the server upserts (patches
        in place) rather than accumulating overlapping spans — the same heartbeat-
        extend contract real AW events rely on.
        """
        if latest_afk is None:
            return None
        if now is None:
            now = datetime.now(timezone.utc)
        try:
            freeze_end = latest_afk.timestamp + timedelta(seconds=latest_afk.duration)
        except (TypeError, AttributeError):
            return None

        if (now - freeze_end).total_seconds() <= _AFK_STALENESS_GRACE:
            return None  # tracker still heartbeating — not frozen

        system_idle = self._get_system_idle_seconds()
        if system_idle is None or system_idle >= _AFK_STALENESS_GRACE:
            # Can't confirm activity, or the OS clock agrees the user is idle.
            return None

        last_input = now - timedelta(seconds=system_idle)
        duration = (last_input - freeze_end).total_seconds()
        if duration <= 0:
            return None  # last input predates the freeze — nothing active to claim

        synth_id = f"synth-active_{self._hostname}_{int(freeze_end.timestamp())}"
        result: dict = {
            "id": synth_id,
            "timestamp": freeze_end.isoformat(),
            "duration": round(duration, 2),
            "bucket_id": afk_bucket_id,
            "bucket_type": BUCKET_TYPE_AFK,
            "data": {"status": "not-afk", "synthetic": True},
        }
        return self._stamp_project(result)

    @staticmethod
    def _get_system_idle_seconds() -> Optional[float]:
        """OS idle clock (seconds since last input), or None. Delegates to the
        shared implementation also used by IdleManager."""
        return get_system_idle_seconds()

    def _synthesize_for_stale_afk(
        self, afk_buckets: list, now: Optional[datetime] = None
    ) -> Optional[dict]:
        """Cycle-level wrapper around ``_synthesize_active_afk_event``: gathers
        the latest AFK event and the target bucket, then synthesizes a not-afk
        span if the tracker is frozen while the user is active. Returns None when
        there is nothing to synthesize.
        """
        if not afk_buckets:
            return None
        try:
            latest = self.aw.get_latest_afk_event()
        except AWClientError:
            return None
        # Attach to the BetterFlow-owned AFK bucket so the server folds the span
        # into the same AFK stream it bills from; fall back to the first bucket.
        bucket_id = next(
            (b.id for b in afk_buckets if "bf-idle-tracker" in b.id),
            afk_buckets[0].id,
        )
        return self._synthesize_active_afk_event(latest, bucket_id, now=now)

    def _inproc_afk_active(self) -> bool:
        """True when the in-process AFK source should be the sole AFK source —
        config flag on, a source is wired, and the OS idle clock is readable
        (macOS/Windows; False on Linux)."""
        return (
            bool(self.config.sync.in_process_afk)
            and self.afk_source is not None
            and self.afk_source.available()
        )

    @property
    def inproc_afk_active(self) -> bool:
        """Public view of the per-cycle in-process-AFK decision, so the app can
        keep aw_manager's flag (which drives the idle-tracker watchdog + health
        telemetry) reconciled with what the sync engine actually does (A)."""
        return self._inproc_afk_active()

    def _should_skip_external_afk(self) -> bool:
        """Whether to drop the external bf-idle-tracker bucket from this cycle's
        upload (because the agent uploads its own AFK stream instead)."""
        return self._inproc_afk_active()

    def _publish_inproc_afk_flag(self, active: bool) -> None:
        """Push the per-cycle in-process-AFK decision to the flag sink
        (aw_manager.set_inproc_afk_active). Called from the cycle's decision point
        so the flag — which gates the idle-tracker watchdog + AFK health
        telemetry — is a direct consequence of what the engine actually did this
        cycle, not a cache a separate 60s timer keeps in sync. None / errors are
        tolerated: telemetry wiring must never break a sync cycle."""
        sink = self.inproc_afk_flag_sink
        if sink is None:
            return
        try:
            sink(active)
        except Exception as e:
            logger.debug("inproc_afk_flag_sink failed: %s", e)

    def _build_inproc_afk(self, now: datetime) -> list[dict]:
        """Build in-process AFK events for [checkpoint, now]. First cycle seeds
        the checkpoint at `now` (we only account for time while running). The
        checkpoint is NOT advanced here — it is committed by
        ``_commit_inproc_afk_checkpoint`` only once the send succeeds, so a
        terminal send failure can't lose a span (audit finding B). Returns []
        when not active."""
        if not self._inproc_afk_active():
            return []
        cp = self._afk_inproc_checkpoint
        if cp is None:
            self._afk_inproc_checkpoint = now
            return []

        # Re-seed rather than reconstruct when the checkpoint can't be trusted:
        #  - now < cp: the wall clock stepped backward (NTP/manual). Without this
        #    the finalize guard below would stall the stream forever (D).
        #  - gap > retention: a pause/sleep froze the checkpoint while samples
        #    were pruned (2h). Reconstructing over an unsampled multi-hour window
        #    would mis-bill; the unobserved span is simply left uncovered (C).
        gap = (now - cp).total_seconds()
        if now < cp:
            # The wall clock stepped backward (NTP/manual). The window can't be
            # trusted and the finalize guard below would stall the stream
            # forever (D) — re-seed and move on.
            logger.info(
                "in-process AFK: re-seeding checkpoint (clock stepped back %.0fs)",
                -gap,
            )
            self._afk_inproc_checkpoint = now
            self._afk_inproc_pending = None
            return []
        if gap > self.afk_source.retention_seconds:
            # A pause/sleep froze the checkpoint past the sample-retention
            # window (2h). We can't reconstruct the whole multi-hour span, but
            # discarding it outright dropped the genuine active work the agent
            # had already observed in the final minutes before the machine
            # slept — surfacing as a ~10-20 min idle/empty gap hugging every
            # pause (Ecaterina/Matei Cocora, device 44, 2026-06-25). Salvage the
            # spans the retained samples DO cover (cover_unsampled=False leaves
            # the unobserved window uncovered, exactly as before) and re-seed
            # past it. Emitted one-shot: the queue is the durability layer, and
            # the checkpoint is reset to `now` so nothing is rebuilt.
            salvaged = self.afk_source.build_afk_events(
                cp, self.afk_source.finalize_point(now),
                project_id=self._current_project_id(),
                cover_unsampled=False,
            )
            logger.info(
                "in-process AFK: re-seeding checkpoint (gap %.0fs) — salvaged %d "
                "observed span(s), leaving the unobserved window uncovered",
                gap, len(salvaged),
            )
            # INTENTIONAL deviation from finding B (checkpoint advances only after
            # a confirmed send): the salvaged spans can't be rebuilt next cycle —
            # the samples that produced them are about to age out. The offline
            # queue is the durability layer instead: a queued salvage drains later
            # and the server upserts idempotently on the stable (ms-precision) ids.
            # Advancing to `now` prevents re-salvaging the same window every wake.
            self._afk_inproc_checkpoint = now
            self._afk_inproc_pending = None
            return salvaged

        # Blind clock: the OS idle clock has failed for several consecutive
        # cycles, so there are no fresh samples. finalize_point would backdate
        # the whole un-observed span as afk — billing real work as idle. Hold the
        # checkpoint and surface it to ops instead (audit finding E). When the
        # clock recovers, fresh samples resume and the held span bills correctly.
        if self.afk_source.consecutive_clock_failures >= self._INPROC_BLIND_THRESHOLD:
            if not self._inproc_blind_reported:
                self._inproc_blind_reported = True
                self._report_inproc_blind(self.afk_source.consecutive_clock_failures)
            return []
        self._inproc_blind_reported = False

        # Only finalize up to the point whose afk classification is settled. While
        # the user is within the timeout of their last input, the trailing region
        # is still pending (could go not-afk or afk), so it waits for a later cycle
        # — otherwise we'd commit it not-afk and be unable to flip it to afk if they
        # stay idle past the timeout.
        finalize_to = self.afk_source.finalize_point(now)
        if finalize_to <= cp:
            return []
        events = self.afk_source.build_afk_events(
            cp, finalize_to,
            project_id=self._current_project_id(),
        )
        # Defer the checkpoint advance to _commit_inproc_afk_checkpoint (finding B).
        self._afk_inproc_pending = finalize_to
        return events

    def _commit_inproc_afk_checkpoint(self, stats: SyncStats) -> None:
        """Advance the in-process AFK checkpoint past the just-built span, but
        only if its bucket wasn't queued (the send succeeded). On a queued/failed
        send we leave the checkpoint where it was so the next cycle rebuilds the
        same span from samples — the synthetic ids are stable, so a later queue
        drain + the rebuild upsert idempotently. Unlike AW buckets, the
        in-process stream has no source to re-fetch from, so advancing before a
        confirmed send risks permanent loss on terminal queue failure (B)."""
        pending = self._afk_inproc_pending
        if pending is None:
            return
        inproc_bucket = self.afk_source.bucket_id
        if inproc_bucket not in stats.queued_bucket_ids:
            self._afk_inproc_checkpoint = pending
        self._afk_inproc_pending = None

    def _should_use_inproc_window(self) -> bool:
        """True when the in-process window source should be the sole per-app
        window source — config flag on, a source is wired, and the OS
        frontmost-window probe is usable (macOS/Windows; False on Linux without
        an X11 active-window pid). Gates every in-process-window action, so the
        whole path is a no-op when the flag is off (default)."""
        return (
            bool(self.config.sync.in_process_window)
            and self.window_source is not None
            and self.window_source.available()
        )

    def record_window_sample_if_active(self, now: datetime) -> None:
        """Sample the frontmost window IFF the in-process window source is the
        active per-app source. Called both per sync cycle and by the dedicated
        fast sampler (~5s) — window focus changes far faster than the 60s cycle,
        so dense samples are what give minute-vs-cycle resolution to the spans
        build_window_events reconstructs. Cheap, gated, and a no-op when the
        flag is off (default), so it adds no work to the default path."""
        if not self._should_use_inproc_window():
            return
        try:
            self.window_source.record_sample(now)
        except Exception as e:
            logger.debug("window_source.record_sample failed: %s", e)

    def _window_source_blind(self) -> bool:
        """True when the in-process window probe has failed to read the frontmost
        window for enough consecutive samples to be considered blind (e.g. psutil
        access-denied on win32). While blind it produces no per-app spans, so the
        external tracker must NOT be suppressed."""
        return (
            self.window_source is not None
            and self.window_source.consecutive_failures >= self._INPROC_BLIND_THRESHOLD
        )

    def _check_window_source_health(self) -> None:
        """Escalate a blind in-process window probe to ops exactly once per blind
        episode. Called each cycle. Mirrors the AFK blind-clock escalation
        (finding E): a logged-only blind probe is invisible until per-app
        attribution is already silently lost."""
        if not self._should_use_inproc_window():
            self._window_inproc_blind_reported = False
            return
        if self._window_source_blind():
            if not self._window_inproc_blind_reported:
                self._window_inproc_blind_reported = True
                self._report_window_blind(self.window_source.consecutive_failures)
        else:
            self._window_inproc_blind_reported = False

    def _report_window_blind(self, failures: int) -> None:
        """Surface a blind in-process window probe to the ops ingest (mirrors
        ``_report_inproc_blind``). While blind, ``_should_skip_external_window``
        also returns False so the external tracker covers per-app attribution
        instead of the device going dark."""
        logger.warning(
            "in-process window probe blind for %d consecutive samples — falling "
            "back to the external window tracker until it recovers",
            failures,
        )
        if self.error_reporter is None:
            return
        try:
            self.error_reporter.capture(
                f"In-process window probe blind for {failures} consecutive samples",
                level="warning",
                tags={"component": "inproc-window"},
                fingerprint="inproc-window-blind",
            )
        except Exception:
            logger.debug("inproc-window-blind report failed", exc_info=True)

    def _should_skip_external_window(self) -> bool:
        """Whether to drop the external bf-window-tracker bucket(s) from this
        cycle's upload (because the agent uploads its own per-app window stream
        instead). Mirrors ``_should_skip_external_afk``. Falls back to the external
        source (returns False) while the in-process probe is blind, so a device
        never loses per-app coverage entirely."""
        return self._should_use_inproc_window() and not self._window_source_blind()

    def _build_inproc_window(
        self, now: datetime
    ) -> tuple[list[dict], Optional[datetime]]:
        """Build in-process window events for [checkpoint, now]. First cycle seeds
        the checkpoint at `now` (we only account for time while running).

        Returns ``(events, pending)`` where `pending` is the checkpoint the caller
        must commit via ``_commit_inproc_window_checkpoint`` AFTER a confirmed send
        (or None to commit nothing). The pending value is RETURNED rather than
        stored on the instance: the wedge re-arm (main.py) can run two ``sync()``
        calls concurrently on this engine, and a shared ``_window_inproc_pending``
        field would let the second cycle clobber the first's in-flight value —
        advancing the checkpoint past a span the first cycle never confirmed
        (silent per-app loss). A local can't be clobbered.

        This closes the pending-clobber path specifically. The ``_window_inproc_checkpoint``
        field itself is still reassigned unsynchronized in the reseed branches
        below (a concurrent reseed could rewind it) — but that only re-emits spans
        that upsert idempotently by stable id, never fabricates or loses time, and
        it mirrors the pre-existing in-process AFK checkpoint. Full serialization
        would require gating both streams' checkpoints together and is deferred."""
        if not self._should_use_inproc_window():
            return [], None
        cp = self._window_inproc_checkpoint
        if cp is None:
            self._window_inproc_checkpoint = now
            return [], None
        # Re-seed rather than reconstruct when the checkpoint can't be trusted
        # (mirrors _build_inproc_afk):
        #  - now <= cp: the wall clock stepped backward (NTP/manual), or nothing
        #    new to cover. Re-seed to `now` so the stream doesn't stall.
        #  - gap > retention: a pause/sleep froze the checkpoint while samples
        #    were pruned (2h). Reconstructing over an unsampled multi-hour window
        #    would mis-count; the unobserved span is simply left uncovered.
        if now <= cp:
            self._window_inproc_checkpoint = now
            return [], None
        gap = (now - cp).total_seconds()
        if gap > self.window_source.retention_seconds:
            logger.info(
                "in-process window: re-seeding checkpoint (gap %.0fs) — leaving "
                "the unobserved window uncovered",
                gap,
            )
            self._window_inproc_checkpoint = now
            return [], None
        events = self.window_source.build_window_events(cp, now)
        return events, now

    def _commit_inproc_window_checkpoint(
        self, stats: SyncStats, pending: Optional[datetime]
    ) -> None:
        """Advance the in-process window checkpoint past the just-built span, but
        only if its bucket wasn't queued (the send succeeded). On a queued/failed
        send we leave the checkpoint where it was so the next cycle rebuilds the
        same span from samples — the synthetic ids are stable, so a later queue
        drain + the rebuild upsert idempotently. Mirrors
        ``_commit_inproc_afk_checkpoint``.

        `pending` is passed in (this cycle's own value from ``_build_inproc_window``)
        rather than read from a shared field, and the advance is forward-only: a
        stale pending from an abandoned wedge-recovery zombie can't rewind the
        checkpoint past a newer cycle's advance or a pause/private reset."""
        if pending is None:
            return
        inproc_bucket = self.window_source.bucket_id
        if inproc_bucket in stats.queued_bucket_ids:
            return
        cp = self._window_inproc_checkpoint
        if cp is None or pending > cp:
            self._window_inproc_checkpoint = pending

    def _should_use_inproc_input(self) -> bool:
        """True when the in-process input source should be the sole input source —
        config flag on, a source is wired, and an in-process counting backend is
        usable (Windows ctypes hooks / macOS CGEventTap; False on Linux). Gates
        every in-process-input action, so the whole path is a no-op when the flag
        is off (default). Mirrors ``_should_use_inproc_window``."""
        return (
            bool(self.config.sync.in_process_input)
            and self.input_source is not None
            and self.input_source.available()
        )

    def _should_skip_external_input(self) -> bool:
        """Whether to drop the external aw-watcher-input bucket(s) from this
        cycle's upload (because the agent uploads its own count stream instead).
        Mirrors ``_should_skip_external_afk`` / ``_should_skip_external_window``.

        Note: unlike the window source, the input backend has no per-sample
        "blind" fallback — a period with no keystrokes is a legitimate gap, not a
        blind probe. If the backend fails to install at all, ``available()``
        reports False and this returns False, so the external tracker keeps its
        job — and since ``available()`` caches nothing, a backend that dies
        AFTER a good start flips this back to False on the very next cycle
        rather than going on suppressing the external bucket.
        """
        return self._should_use_inproc_input()

    def _build_inproc_input(
        self, now: datetime
    ) -> tuple[Optional[dict], Optional[datetime]]:
        """Drain accumulated input counts into one event for [checkpoint, now].
        First cycle seeds the checkpoint at `now` (account only while running).

        Returns ``(event_or_None, pending)`` where `pending` is the checkpoint the
        caller must commit via ``_commit_inproc_input_checkpoint`` AFTER a
        confirmed send. The pending value is RETURNED, not stored on the instance,
        for the same wedge-recovery concurrency reason as ``_build_inproc_window``.

        Re-seed rather than build when the checkpoint can't be trusted (mirrors
        _build_inproc_window):
          - now <= cp: the wall clock stepped backward (NTP/manual), or nothing
            new to cover. Re-seed to `now` so the stream doesn't stall. Any counts
            accrued so far are intentionally dropped (we can't attribute them to a
            trustworthy range) — unlike window/AFK, counts have no sample log to
            rebuild from, but a clock step back is rare and bounded.
        Unlike AFK/window there is no gap>retention branch: counts don't come from
        a time-bounded sample deque, so a long sleep simply means whatever was
        typed before it drains into the (long) span — harmless, since the counts
        themselves are real. The drain returns None on a zero-count span, so an
        idle span emits nothing."""
        if not self._should_use_inproc_input():
            return None, None
        cp = self._input_inproc_checkpoint
        if cp is None:
            self._input_inproc_checkpoint = now
            return None, None
        if now <= cp:
            self._input_inproc_checkpoint = now
            return None, None
        event = self.input_source.drain_input_event(cp, now)
        if event is None:
            # Nothing counted this span: advance the checkpoint so the next span
            # starts at `now` (no event to send, so no confirmed-send gate needed).
            self._input_inproc_checkpoint = now
            return None, None
        return event, now

    def _commit_inproc_input_checkpoint(
        self, stats: SyncStats, pending: Optional[datetime]
    ) -> None:
        """Advance the in-process input checkpoint past the just-drained span.

        UNLIKE the window/AFK commits, this advances even on a QUEUED (failed)
        send. drain_input_event already destructively reset the counters into
        this cycle's stable-id event, so the counts now live ONLY in that event —
        which the offline queue redelivers, upserted idempotently by id. Holding
        the checkpoint (as window/AFK do, to rebuild from samples) would instead
        make the next cycle re-drain an already-empty counter into a duplicate,
        overlapping span; there is no counter left to rebuild from. `stats` is
        unused here for exactly that reason — the queued/not-queued distinction
        doesn't change the advance. Forward-only, mirroring
        ``_commit_inproc_window_checkpoint``'s monotonic guard."""
        del stats  # intentionally unused; see docstring (advance is unconditional)
        if pending is None:
            return
        cp = self._input_inproc_checkpoint
        if cp is None or pending > cp:
            self._input_inproc_checkpoint = pending

    def _report_inproc_blind(self, failures: int) -> None:
        """Surface a blind in-process idle clock to the ops ingest. Logged-only
        clock failures are invisible until billing is already wrong; the
        error_reporter is the channel an admin can see (audit finding E)."""
        logger.warning(
            "in-process AFK idle clock blind for %d consecutive cycles — holding "
            "checkpoint; active time may be under-counted until it recovers",
            failures,
        )
        if self.error_reporter is None:
            return
        try:
            self.error_reporter.capture(
                f"In-process AFK idle clock blind for {failures} consecutive cycles",
                level="warning",
                tags={"component": "inproc-afk"},
                fingerprint="inproc-afk-blind",
            )
        except Exception:
            logger.debug("inproc-afk-blind report failed", exc_info=True)

    def _send_events(self, events: list[dict], stats: SyncStats) -> None:
        """Send events to BetterFlow, grouped by bucket (#4 decouple buckets).

        Each bucket's events are batched and sent in their own sequence so a
        transient failure in one bucket (e.g. a frozen/duplicate AFK stream)
        re-queues and back-off-marks ONLY that bucket. Previously all buckets
        shared one batch list, so a single transient failure with no
        ``accepted_ids`` re-queued the whole mixed batch and tainted every
        bucket's ``queued_bucket_ids`` — withholding the window checkpoint over
        an unrelated AFK failure. Grouping confines the blast radius to the
        failing bucket; ``_send_and_advance_checkpoints`` then advances the
        healthy buckets normally.
        """
        # Preserve insertion order (dict is ordered in 3.7+) so behaviour is
        # deterministic and single-bucket cycles are unchanged.
        by_bucket: dict[str, list[dict]] = {}
        for event in events:
            by_bucket.setdefault(event.get("bucket_id", ""), []).append(event)

        bucket_groups = list(by_bucket.values())
        for idx, group in enumerate(bucket_groups):
            # In-cycle network budget: each bucket group is its own retrying
            # request chain (~94s against a hung server). Once this cycle has
            # spent the budget on network, queue the remaining groups instead of
            # starting another chain that would stack past the _do_sync watchdog
            # ("Sync hung" / "Sync wedged").
            #
            # The check gates EVERY group, including the first (idx == 0). The
            # first group used to attempt unconditionally "for forward progress",
            # but the only way the budget is already spent when the send loop
            # begins is a slow/hung PRE-send chain — a session-start (~94s) or a
            # config-fetch, each gated only at ITS entry, so once started it runs
            # to completion. A session-start chain (elapsed 0 -> ~94s) followed by
            # an unconditional first-send chain (~94s) reaches ~188s, blowing the
            # 150s watchdog even though nothing is wedged. Gating the first group
            # too caps the cycle at one surviving chain (budget + ~94s = ~144s <
            # 150s). Forward progress is preserved ACROSS cycles by the durable
            # OfflineQueue; a hung server must not force an in-cycle delivery that
            # overruns the watchdog. In a healthy cycle elapsed is ~0 here, so the
            # first group still sends. No-op when the cycle start is unset (a
            # direct _send_events call outside sync()).
            if self._cycle_network_budget_exceeded(
                self._SEND_SKIP_IF_CYCLE_ELAPSED
            ):
                logger.warning(
                    "Send budget spent (>=%ds) — queuing %d remaining bucket "
                    "group(s) for next cycle instead of stacking another upload "
                    "chain past the watchdog",
                    self._SEND_SKIP_IF_CYCLE_ELAPSED, len(bucket_groups) - idx,
                )
                for remaining in bucket_groups[idx:]:
                    self.queue.enqueue(remaining)
                    stats.events_queued += len(remaining)
                    stats.queued_bucket_ids.update(
                        e.get("bucket_id", "") for e in remaining
                    )
                return
            try:
                self._send_bucket_events(group, stats)
            except BetterFlowAuthError:
                # A 401 means the server rejected the request — every bucket not
                # yet attempted is also unsent. Queue them all before
                # propagating so nothing is silently dropped (the per-bucket
                # handler already queued this bucket's remaining batches).
                for remaining in bucket_groups[idx + 1:]:
                    self.queue.enqueue(remaining)
                    stats.events_queued += len(remaining)
                    stats.queued_bucket_ids.update(
                        e.get("bucket_id", "") for e in remaining
                    )
                raise

    def _send_bucket_events(self, events: list[dict], stats: SyncStats) -> None:
        """Batch and send a single bucket's events (or queue on failure)."""
        # Batch events
        batch_size = self.config.sync.batch_size
        batches = [events[i : i + batch_size] for i in range(0, len(events), batch_size)]

        for i, batch in enumerate(batches):
            try:
                self._note_delivery_attempt()
                result = self.bf.send_events(batch)
                if result.success:
                    stats.events_sent += result.events_synced
                    self._clear_queue_backoff("live batch accepted")
                else:
                    if result.accepted_ids:
                        # True partial success: re-queue only the events the
                        # server did NOT accept.
                        accepted_set = set(result.accepted_ids)
                        failed = [e for e in batch if e.get("id") not in accepted_set]
                        stats.events_sent += len(batch) - len(failed)
                        self._clear_queue_backoff("live batch partially accepted")
                        if failed:
                            self.queue.enqueue(failed)
                            stats.events_queued += len(failed)
                            stats.queued_bucket_ids.update(
                                e.get("bucket_id", "") for e in failed
                            )
                    else:
                        # N11: total failure with nothing accepted — a transient
                        # error (429 rate-limit backoff, network drop, timeout;
                        # bf_client catches BetterFlowClientError internally and
                        # returns SyncResult(success=False), so there is no
                        # separate network-error except branch — see Important-2
                        # in commit history). Re-queue the whole batch; the
                        # content-derived idempotency key (bf_client.send_events)
                        # makes the eventual resend safe against duplicates.
                        logger.warning(
                            "Batch not accepted (transient failure: %s) - re-queuing all %d events",
                            result.error or "unknown",
                            len(batch),
                        )
                        self.queue.enqueue(batch)
                        stats.events_queued += len(batch)
                        stats.queued_bucket_ids.update(
                            e.get("bucket_id", "") for e in batch
                        )
                    if result.error:
                        stats.errors.append(result.error)
            except BetterFlowAuthError as e:
                # Queue the current batch and all remaining unsent batches
                # before re-raising.  A 401 means the server rejected the
                # request, so batch i was NOT processed.  Re-sending is safe
                # because the server uses idempotency keys.
                for remaining in batches[i:]:
                    self.queue.enqueue(remaining)
                    stats.events_queued += len(remaining)
                    stats.queued_bucket_ids.update(
                        e.get("bucket_id", "") for e in remaining
                    )
                stats.errors.append(f"Authentication error: {e}")
                raise

    _QUEUE_PROCESS_TIMEOUT = 30.0  # Max wall-clock seconds for queue drain
    # A transient whole-batch failure normally does NOT count toward the drop, so
    # a server outage can't drop good activity (#99). But a batch that fails
    # transiently DETERMINISTICALLY — a content-specific 5xx, or a server stuck
    # returning no-confirmation — would otherwise sit at the oldest-first dequeue
    # head forever, freezing every newer event up to the 30-day expiry, silently
    # (the heartbeat stays green). After this many consecutive failed queue cycles
    # (with the backoff schedule, ~3h of sustained failure) we count the stuck
    # head toward the drop so it ages out and the queue unblocks — BUT only if the
    # head batch is unstorable (stale/bucketless); a stuck head that still holds
    # recent bucketed activity is held, never dropped, so a long events-route
    # degradation (server reachable, batches 5xx, nothing draining) can't lose real
    # billable time. The counter (_queue_consecutive_failures) counts every
    # transient queue failure and resets on any success.
    _STUCK_HEAD_CEILING = 20
    # ONE in-cycle network budget, shared by every retrying request chain a cycle
    # can start: the per-bucket regular send (_send_events) AND the offline-queue
    # drain (_process_queue). Each chain can take ~94s against a hung server; run
    # back-to-back inside the single _do_sync watchdog they exceed _DO_SYNC_DEADLINE
    # (150s -> "Sync hung") and, stacked deep enough (N buckets), the 420s wedge
    # ceiling ("Sync wedged") — the Azorel outage 2026-07-02 (fps 707a9ecc /
    # 63a18e4f / d31bb248). Once a cycle has spent this budget, no NEW chain is
    # started: remaining bucket groups / the queue drain are deferred to the next
    # cycle (durable OfflineQueue). The first bucket group always attempts so a
    # cycle makes forward progress. 50s + one ~94s chain stays under 150s with
    # margin. Both gates check it via `_cycle_network_budget_exceeded`.
    _CYCLE_NETWORK_BUDGET_SECONDS = 50  # seconds
    # How often to re-pull /config while running. Config was previously fetched
    # once per process, so a schedule change (e.g. HR marks an employee
    # restricted at 14:00) never reached a running agent until it restarted —
    # the restricted user kept being recorded for days. Re-pulling every 30 min
    # closes that without meaningfully adding load (one budgeted GET).
    _CONFIG_REFETCH_INTERVAL_SECONDS = 1800  # 30 min
    # Named aliases kept for the two call sites / their tests; both resolve to the
    # single source of truth above so the two gates can never drift apart.
    _QUEUE_SKIP_IF_CYCLE_ELAPSED = _CYCLE_NETWORK_BUDGET_SECONDS
    _SEND_SKIP_IF_CYCLE_ELAPSED = _CYCLE_NETWORK_BUDGET_SECONDS

    def _cycle_network_budget_exceeded(self, budget_seconds: float) -> bool:
        """True when the current sync() cycle has already spent `budget_seconds`
        on network IO — the shared gate that stops a second ~94s retry chain from
        stacking past the _do_sync watchdog. Used by both the per-bucket send loop
        and the offline-queue drain. Returns False when the cycle start is unset
        (a direct call outside sync(), e.g. a unit test)."""
        start = self._cycle_start_monotonic
        return start is not None and (time.monotonic() - start) >= budget_seconds

    # After this many CONSECUTIVE cycles in which the budget gate refused the
    # drain and nothing else delivered either, one drain is forced through even
    # with the budget spent. See _drain_gate_allows for why this exists and why
    # the number is small.
    _DELIVERY_STARVATION_FLOOR_CYCLES = 3

    def _note_delivery_attempt(self) -> None:
        """Record that this cycle put events on the wire.

        Called at every ``bf.send_events`` site. "Attempt", not "success", is
        deliberate: the property the floor protects is that the budget gate
        cannot stop us from TRYING. A batch that fails has its own backoff and
        retry-ceiling machinery; a batch that is never sent has nothing.
        """
        self._cycle_delivered = True

    def _drain_gate_allows(self) -> bool:
        """Whether to drain the offline queue this cycle — budget gate plus a
        floor so it can never refuse forever.

        The budget gate alone is a permanent freeze away from a real outage.
        When the backend degrades on ``/session/start`` only, ``start_session``
        raises, ``_session_active`` stays False, so ``need_session`` is True
        every cycle and each cycle burns a ~94s chain there before reaching any
        delivery gate. Both the send loop and this drain then see elapsed >= the
        budget, so the cycle sends nothing AND drains nothing — forever, with a
        green heartbeat ("alive but uploads frozen"). Events pile up to
        ``max_size`` and oldest-eviction begins destroying billable time.

        The floor is on the DRAIN rather than on the first send group, which is
        what the send-side gate's own comment would suggest. Reason: a blocked
        send is not a lost send — ``_send_events`` enqueues those events into the
        durable queue. So the drain is the COMPLETE delivery surface; everything
        reaches the server through it eventually. Forcing only the first send
        group would deliver one bucket group per forced cycle while the queue
        behind it grew without bound, which is the data-loss half of the bug
        left intact. Forcing the drain instead moves the whole backlog: it
        carries up to ``batch_size * 10`` events, far more than a few cycles of
        capture, so the queue cannot run away.

        Counted per CYCLE, and only when the cycle delivered NOTHING. A cycle
        that spent the budget but still put events on the wire is the gate
        working exactly as designed and resets the counter — otherwise a merely
        slow-but-healthy agent would be forced into a second chain every few
        cycles for no reason.

        The cost is explicit and bounded: a forced drain can put a cycle at
        ~94s (session chain) + ~94s (drain) ~= 188s, over the 150s "Sync hung"
        report threshold though well under the 420s wedge ceiling — and at most
        once every ``_DELIVERY_STARVATION_FLOOR_CYCLES + 1`` cycles, because
        the forced drain itself resets the counter. A noisy watchdog report
        every few cycles is the correct trade against silently losing billed
        time; the report is also how an operator finds out this is happening.
        """
        if not self._cycle_network_budget_exceeded(self._QUEUE_SKIP_IF_CYCLE_ELAPSED):
            self._consecutive_undelivered_cycles = 0
            return True
        if self._cycle_delivered:
            return False
        self._consecutive_undelivered_cycles += 1
        if self._consecutive_undelivered_cycles < self._DELIVERY_STARVATION_FLOOR_CYCLES:
            return False
        logger.warning(
            "Delivery starvation floor engaged: %d consecutive cycles delivered "
            "NOTHING because the %ds in-cycle network budget was already spent "
            "before any upload could start (queue_size=%d). Forcing one queue "
            "drain through — this cycle may exceed the sync watchdog, which is "
            "the intended trade against a permanently frozen upload path. "
            "Something ahead of the upload (session start / config fetch) is "
            "burning the whole budget every cycle; that is the real fault.",
            self._consecutive_undelivered_cycles,
            self._QUEUE_SKIP_IF_CYCLE_ELAPSED,
            self.queue.size(),
        )
        self._consecutive_undelivered_cycles = 0
        return True

    def _process_queue(self, stats: SyncStats) -> None:
        """Process offline queue with exponential backoff.

        Capped at 30s wall-clock time to prevent tying up the sync thread.
        """
        # Evict genuinely-unstorable events (no bucket to route to, or already
        # past the server's retention window) from the active queue BEFORE
        # batching. dequeue() is oldest-first, so an unstorable event sits at the
        # head and is batched with storable events behind it; the server 4xx's the
        # whole batch on the poison, and the whole-batch retry bump below then
        # drags the storable neighbours to the drop ceiling in lockstep — the
        # 2026-07 "Dropped N ... likely real lost activity (... ; M other
        # unstorable)" warnings. Removing them first keeps every batch
        # storable-only, so the server accepts it and no real activity is lost.
        # They're MOVED to dead_letter (preserved) and reported at info (benign
        # flush), never as real loss.
        evicted = self.queue.evict_unstorable(
            last_error="unstorable (no bucket / past retention) — evicted before batching"
        )
        if evicted.get("count", 0) > 0:
            self._report_dropped_events(evicted)

        # Remove events that exceeded retry limit. Surface the drop to ops FIRST
        # — with the server now confirming delivery per-event (accepted_ids),
        # anything that still exhausts its retries is a genuine permanent
        # rejection (out-of-range timestamp, server validation), i.e. real
        # activity we are about to lose. Report it instead of dropping silently
        # (the blindness that hid prior data loss for days).
        drop_summary = self.queue.failed_event_summary(max_retries=5)
        if drop_summary.get("count", 0) > 0:
            self._report_dropped_events(drop_summary)
        # Moves (does NOT hard-delete) exhausted events to the dead-letter table
        # so genuine 4xx-rejected activity is preserved for inspection/replay.
        self.queue.remove_failed(
            max_retries=5, last_error="exceeded max retries (5); definitive rejection"
        )

        # Skip queue processing if in backoff period
        now = datetime.now(timezone.utc)
        if now < self._queue_backoff_until:
            return

        # Bounded dead-letter replay: resurrect rows that are storable AGAIN into
        # the live queue for another delivery attempt. Done here — past the
        # backoff gate, so only when we're actually about to drain (server
        # presumed reachable) — so we never resurrect into an outage. The queue
        # method is conservative (bounded batch, MOVE-not-copy so no double-send,
        # same storable/retention classification as evict_unstorable, and rows
        # that are genuinely unstorable or have aged past retention are left
        # behind). Resurrected rows fall into the same drain loop below.
        try:
            self.queue.requeue_storable_dead_letter()
        except Exception as e:  # noqa: BLE001
            # WARNING, not debug. This is the only signal the replay ever emits
            # on failure, and debug is off in the shipped build — a replay that
            # never runs (locked DB, schema drift, disk full) is invisible while
            # dead_letter_count climbs and the operator has nothing to correlate
            # it against. Swallowed deliberately: a broken replay must never
            # cost the drain that follows it.
            logger.warning(
                "Dead-letter replay failed (%s: %s) — preserved events will not "
                "be retried this cycle; dead_letter_count will keep climbing",
                type(e).__name__, e, exc_info=True,
            )

        # Process queue in batches
        deadline = time.monotonic() + self._QUEUE_PROCESS_TIMEOUT
        batch_size = self.config.sync.batch_size
        processed = 0
        max_per_cycle = batch_size * 10  # Max 10 batches per cycle

        while processed < max_per_cycle and time.monotonic() < deadline:
            queued = self.queue.dequeue(batch_size)
            if not queued:
                break

            # Stale live snapshots must never be REPLAYED: a queued 'ongoing'
            # call-bucket row (a transient failure requeues the whole batch,
            # snapshots included) replayed after the meeting's final
            # 'completed' event landed would upsert the same deterministic id
            # back to a shorter, forever-'ongoing' span. A newer snapshot or
            # the final event always supersedes a stale snapshot, so dropping
            # them at drain time loses nothing for any meeting that closes
            # normally. ACCEPTED LOSS: a mic session whose every snapshot was
            # queued (offline for the whole meeting) AND whose process crashed
            # before the mic went cold has no other record — unlike window
            # calls, which re-derive from the un-advanced AW checkpoint after
            # a crash. Chosen over the alternative (replaying stale snapshots
            # regresses completed rows forever).
            stale_snapshot_ids = [
                q.id
                for q in queued
                if q.event_data.get("bucket_type") == BUCKET_TYPE_CALL
                and (q.event_data.get("data") or {}).get("status")
                == CALL_STATUS_ONGOING
            ]
            if stale_snapshot_ids:
                self.queue.remove(stale_snapshot_ids)
                stale_set = set(stale_snapshot_ids)
                queued = [q for q in queued if q.id not in stale_set]
                if not queued:
                    continue

            events = [q.event_data for q in queued]
            event_ids = [q.id for q in queued]

            try:
                self._note_delivery_attempt()
                result = self.bf.send_events(events)
                if result.success:
                    self.queue.remove(event_ids)
                    stats.events_sent += result.events_synced
                    processed += len(events)
                    self._clear_queue_backoff("queue batch accepted")
                elif result.accepted_ids and not result.transient:
                    # A per-event verdict and a transient (no-verdict) failure are
                    # mutually exclusive in send_events today. The `and not
                    # result.transient` is a fail-safe (NOT an assert: an assert is
                    # stripped under python -O, and an AssertionError would crash
                    # the whole sync cycle): if a future path ever returns both,
                    # fall through to the transient `else` and HOLD the batch
                    # rather than increment-and-drop the unconfirmed events during
                    # an outage (#99's bug).
                    #
                    # Partial success: remove accepted, increment retry on rest.
                    # An event without an "id" cannot be matched against
                    # accepted_ids — treat it as failed so it gets retried
                    # rather than silently dropped. All event producers
                    # (regular events, status spans, call events) now include
                    # a stable id; this is defensive against future regressions.
                    accepted_set = set(result.accepted_ids)
                    succeeded_ids = [eid for eid, ev in zip(event_ids, events)
                                     if ev.get("id") in accepted_set]
                    failed_ids = [eid for eid in event_ids if eid not in succeeded_ids]
                    if succeeded_ids:
                        self.queue.remove(succeeded_ids)
                        stats.events_sent += len(succeeded_ids)
                        self._clear_queue_backoff("queue batch partially accepted")
                    if failed_ids:
                        # NOT result.error — the SyncResult on this branch is
                        # built from a 200 with a per-event verdict and carries
                        # no error string. Passing it writes nothing, so the
                        # event would keep whatever reason an unrelated earlier
                        # whole-batch failure stamped on it, and the drop would
                        # be attributed to a status from a different cycle.
                        # This IS the more specific rejection: the server named
                        # these events individually.
                        self.queue.increment_retry(
                            failed_ids,
                            "per-event rejection: omitted from accepted_ids "
                            "(server gave no batch-level reason)",
                        )
                    processed += len(succeeded_ids)
                else:
                    # Whole-batch failure with no per-event verdict. Only count
                    # it toward the drop threshold when the server DEFINITIVELY
                    # rejected the batch (a 4xx). A transient failure — server
                    # down / 5xx / timeout / DNS / no delivery confirmation — is
                    # not these events' fault; incrementing here drops good
                    # activity after 5 down-cycles (the 2026-06-30 outage lost
                    # real spans this way). Hold the events at their current
                    # retry_count and retry next cycle; expire_old is the backstop
                    # against unbounded growth, and a genuinely poison 4xx batch
                    # still increments so it can't head-of-line-block the queue.
                    if not result.transient:
                        # Record WHY. send_events already captured the server's
                        # own text on SyncResult.error; nothing downstream had
                        # ever read it, so every dead-lettered event carried the
                        # agent's generic "definitive rejection" and the cause
                        # was unrecoverable by the time anyone looked.
                        self.queue.increment_retry(event_ids, result.error)
                    elif (
                        self._queue_consecutive_failures >= self._STUCK_HEAD_CEILING
                        and not self._batch_has_storable_activity(events)
                    ):
                        # Transient and stuck for many cycles AND the head batch is
                        # entirely UNSTORABLE (stale past retention or bucketless —
                        # the server would reject it anyway). Count it toward the
                        # drop so it ages out and stops blocking the queue head.
                        #
                        # We deliberately do NOT shed a stuck head that still holds
                        # recent, bucketed activity: a long events-route degradation
                        # (server reachable, batches 5xx) where nothing drains would
                        # otherwise lose real billable time after ~3h. Those events
                        # are held for recovery / the 30-day expire_old backstop.
                        # (A truly poison recent batch blocks only until it ages out
                        # of the retention window — rare, and never a wrong drop.)
                        logger.warning(
                            "Unstorable queue head batch (%d events) stuck %d cycles "
                            "— counting it toward the drop to unblock the queue.",
                            len(event_ids), self._queue_consecutive_failures,
                        )
                        self.queue.increment_retry(
                            event_ids,
                            "shed as unstorable after "
                            f"{self._queue_consecutive_failures} transient "
                            "failures — not a server rejection",
                        )
                    self._apply_queue_backoff()
                    break
            except BetterFlowAuthError:
                # Auth errors won't self-heal with retries; re-raise so
                # the caller's auth handler can trigger re-login.
                raise
            except BetterFlowClientError as e:
                # send_events normally returns a SyncResult; reaching here means an
                # unexpected client error escaped it (should never fire). Surface
                # it, then treat as transient (server-side): back off without
                # counting a retry.
                logger.warning("Unexpected BetterFlowClientError escaped send_events: %s", e)
                self._apply_queue_backoff()
                break

    def _batch_has_storable_activity(
        self, events: list, *, now: Optional[datetime] = None
    ) -> bool:
        """True if any event in the batch is STORABLE — has a bucket to route to,
        a timestamp within the server's retention window, and a duration inside
        the server's accepted bounds. A batch with no storable event (all stale,
        bucketless, or over-long) is one the server would reject anyway, so it's
        safe to shed when it blocks the queue head. Uses the shared
        ``is_event_storable`` classifier so this can never drift from the queue's
        eviction/real-loss verdict — a stuck head holding genuine recent activity
        is held, never dropped."""
        now = now or datetime.now(timezone.utc)
        # ONE definition of the window, imported — not a local literal that
        # silently drifts from the queue's eviction/replay pair.
        cutoff = now - timedelta(days=EVENT_RETENTION_DAYS)
        return any(is_event_storable(ev, stale_cutoff=cutoff) for ev in events)

    def _apply_queue_backoff(self) -> None:
        """Apply exponential backoff for queue processing failures."""
        self._queue_consecutive_failures += 1
        # 60s, 120s, 240s, 480s, max 600s (10 min)
        delay = min(60 * (2 ** (self._queue_consecutive_failures - 1)), 600)
        self._queue_backoff_until = datetime.now(timezone.utc) + timedelta(seconds=delay)
        logger.info(f"Queue backoff: retry in {delay}s (failure #{self._queue_consecutive_failures})")

    def _clear_queue_backoff(self, reason: str) -> None:
        """Clear stale queue backoff after confirmed events-route delivery.

        Queue backoff is correct while the events route is failing, but a later
        successful live upload proves the route is accepting writes again. Keep
        draining immediately instead of leaving older queued events parked until
        a previous 60s-600s backoff expires.
        """
        if (
            self._queue_consecutive_failures > 0
            or self._queue_backoff_until > datetime.now(timezone.utc)
        ):
            logger.info("Queue backoff cleared: %s", reason)
        self._queue_consecutive_failures = 0
        self._queue_backoff_until = datetime.min.replace(tzinfo=timezone.utc)

    def send_heartbeat_if_due(self, stats: "SyncStats") -> Optional["BetterFlowAuthError"]:
        """Public entry point: send heartbeat iff the sync stats marked it due.

        Encapsulates the heartbeat dispatch decision so callers don't reach
        into private state. Safe to call unconditionally after sync() — no-op
        when stats._should_heartbeat is False.

        Returns the BetterFlowAuthError if the heartbeat (or any server-driven
        config refresh it triggers) returned a 401/403; callers should treat
        this as a "session expired" signal and trigger re-login. Returns None
        on success or non-auth failures (which are logged internally).
        """
        if stats and stats._should_heartbeat:
            return self._send_heartbeat()
        return None

    def send_heartbeat_now(self) -> Optional["BetterFlowAuthError"]:
        """Send a heartbeat immediately, bypassing the sync-cycle counter.

        Used to keep the device alive on the server while the agent is PAUSED
        (break / screen lock / manual pause). The normal heartbeat rides the
        sync cycle, which is skipped while paused — so without this the device's
        last_seen_at goes stale and the server's 30-minute stale-session cleanup
        marks a long break as a 'crashed' session, and tracking doesn't resume
        when the user returns. A paused-but-running agent is alive, not crashed.
        Heartbeats only refresh last_seen_at; they never add active/tracked time.

        Returns a BetterFlowAuthError on 401/403 so the caller can re-login.
        """
        return self._send_heartbeat()

    def seconds_since_last_heartbeat(self) -> Optional[float]:
        """Monotonic age of the last heartbeat attempt (any path), or None if
        none has been sent this process. The main loop's 60s-tick heartbeat
        floor reads this to reach idle/paused devices whose sync-cadence
        heartbeat has gone dormant: an idle device drops to the 300s sync
        interval, whose every-5th-cycle heartbeat is ~25 min apart, so remote
        commands (pause / deregister / min-version / logs_requested) would
        otherwise take that long to land. Active devices heartbeat ~every 150s,
        keeping this age under the floor, so the floor never fires for them."""
        with self._state_lock:
            last = self._last_heartbeat_monotonic
        if last is None:
            return None
        return time.monotonic() - last

    def _send_heartbeat(self) -> Optional["BetterFlowAuthError"]:
        """Send heartbeat to server and process commands.

        Returns BetterFlowAuthError on 401/403 so the caller can trigger
        re-login. Other client errors are logged at debug and swallowed —
        a transient heartbeat failure should not surface as a user error.
        """
        # Serialize the send + command-processing body. _send_heartbeat is
        # reachable from two scheduler threads at once — the sync-cadence path
        # (_do_sync → send_heartbeat_if_due) and the 60s-tick heartbeat floor
        # (_tick_60s → send_heartbeat_now). On an idle device both are live and
        # their periods are harmonics (cadence 5×300s, floor 300s), so they
        # periodically fire near-coincidentally. Without this guard two
        # concurrent runs would double-POST, double-upload logs, and race
        # fetch_server_config() on the shared config object.
        if not self._heartbeat_inflight.acquire(blocking=False):
            # A beat is already in flight; a second caller no-ops WITHOUT
            # advancing the stamp — so if the in-flight beat ever hangs past the
            # floor interval, the stamp keeps ageing (staleness stays truthful)
            # rather than losers refreshing it to a false "fresh" and silently
            # defeating the floor.
            return None
        try:
            # Stamp the ATTEMPT time (before the HTTP) so a down/failed beat still
            # counts as one attempt — the floor throttles on this and must not
            # re-fire every 60s against a down server. Only the guard holder (the
            # real attempter) advances it; see the acquire-failure branch above.
            with self._state_lock:
                self._last_heartbeat_monotonic = time.monotonic()
            health = None
            if self.health_provider is not None:
                try:
                    health = self.health_provider()
                except Exception as e:  # noqa: BLE001
                    # Telemetry is best-effort; never let it block the heartbeat.
                    logger.debug("health_provider failed: %s", e)
            response = self.bf.heartbeat(health=health)

            # The server wraps the heartbeat payload in an envelope: the real
            # fields live under response["data"], NOT at the top level — the same
            # convention send_events already accounts for (it had this exact bug
            # and unwraps with response.get("data", response)). Reading top-level
            # here made EVERY heartbeat-driven feature a silent no-op: remote
            # pause/deregister commands, the minimum-version check, clock-skew
            # detection, config-change refetch, and the admin logs_requested
            # upload all never fired. Unwrap once, with a fallback so a future
            # un-enveloped response still works.
            payload = response.get("data", response) if isinstance(response, dict) else {}

            # Handle server commands
            commands = payload.get("commands", [])
            for cmd in commands:
                cmd_type = cmd.get("type")
                if cmd_type == "pause":
                    logger.info(f"Server requested pause: {cmd.get('reason')}")
                    self.pause()
                elif cmd_type == "deregister":
                    logger.warning(f"Device revoked: {cmd.get('reason')}")
                    self._advance_checkpoints_to_now("server_deregister")
                    with self._state_lock:
                        self._paused = True

            # Version compatibility check
            min_version = payload.get("minimum_agent_version")
            if min_version and self._version_below(AGENT_VERSION, min_version):
                logger.warning(
                    f"Agent {AGENT_VERSION} is below minimum {min_version} — update required"
                )
                # Off-device too. Both actions below are LOCAL — a log nobody
                # pulls and a staged update that may keep failing — so a device
                # stuck under the floor was invisible to ops for days. #211
                # caught one at 1.5.119 against a 1.5.124 floor, losing
                # window-title categorisation the whole time.
                self._report_below_minimum(min_version)
                # Act on it, don't just log: stage the latest build now (applied
                # on next idle) so a server-pushed urgent fix lands in minutes
                # instead of waiting up to 6h for the periodic check. The handler
                # throttles itself so the 5-min heartbeat won't re-download.
                if self.on_update_required is not None:
                    try:
                        self.on_update_required(min_version)
                    except Exception as e:  # noqa: BLE001
                        logger.debug("on_update_required failed: %s", e)

            # Clock skew detection: compare server time with local time
            server_time_str = payload.get("server_time")
            if server_time_str:
                try:
                    server_time = datetime.fromisoformat(
                        server_time_str.replace("Z", "+00:00")
                    )
                    if server_time.tzinfo is None:
                        server_time = server_time.replace(tzinfo=timezone.utc)
                    local_time = datetime.now(timezone.utc)
                    self._server_time_offset = (server_time - local_time).total_seconds()
                    if abs(self._server_time_offset) > 300:
                        logger.warning(
                            f"Clock skew detected: server time differs by "
                            f"{self._server_time_offset:.0f}s — timestamps may be inaccurate"
                        )
                except (ValueError, TypeError) as e:
                    logger.debug("server_time parse failed: %s", e)

            # Re-fetch config if server says it changed
            if payload.get("config_updated"):
                self.fetch_server_config()

            # Admin requested this device's logs for diagnostics. Upload the
            # tail; the server clears the flag only on success, so a failed
            # upload simply retries on the next heartbeat.
            if payload.get("logs_requested"):
                self._upload_requested_logs()

        except BetterFlowAuthError as e:
            # Surface auth errors to the caller so re-login can fire.
            # The generic BetterFlowClientError handler below would otherwise
            # swallow this at debug level (auth-error subclasses client-error).
            logger.warning("Heartbeat auth error — session likely expired: %s", e)
            return e
        except BetterFlowClientError as e:
            logger.debug(f"Heartbeat failed: {e}")
        finally:
            self._heartbeat_inflight.release()
        return None

    def _upload_requested_logs(self) -> None:
        """Upload this device's log tail(s) on server request (admin
        diagnostics). betterflow.log is required by the server; the relaunch log
        is included when present. Never clears anything client-side — the server
        clears its logs_requested flag only on success, so a failed upload just
        retries on the next heartbeat.
        """
        try:
            log_dir = self.config.get_log_dir()
        except Exception as e:
            logger.debug("logs_requested: could not resolve log dir: %s", e)
            return
        log_tail = self._read_rotated_log_tail(log_dir / "betterflow.log")
        if not log_tail:
            # WARNING (not debug) so the next successful upload carries a record
            # of why earlier ones were skipped — the admin's flag stays set and
            # we retry every heartbeat, which is correct for the normal cause
            # (a transient log-rotation window).
            logger.warning(
                "logs_requested but betterflow.log is empty/unreadable — "
                "skipping this cycle (will retry next heartbeat)"
            )
            self._report_upload_failure("betterflow.log empty/unreadable")
            return
        relaunch_tail = self._read_log_tail(log_dir / "self-update-relaunch.log")
        try:
            self.bf.upload_logs(log_tail, relaunch_tail)
            logger.info("Uploaded log tail on server request (%d bytes)", len(log_tail))
        except BetterFlowAuthError:
            # Make the source explicit — _send_heartbeat's shared handler would
            # otherwise log this as a plain "Heartbeat auth error".
            logger.warning("Log-upload auth error — session likely expired")
            raise  # let _send_heartbeat surface re-login
        except BetterFlowClientError as e:
            logger.debug("Log upload failed (will retry next heartbeat): %s", e)
            self._report_upload_failure(f"upload POST failed: {e}")

    def _report_dropped_events(self, summary: dict) -> None:
        """Surface permanently-dropped queued events to the ops ingest, split by
        whether the drop is genuine loss or a benign flush.

        Only RECENT, BUCKETED events are real lost activity worth a warning — the
        server should have accepted them. Events that are stale (>retention) or
        carry no bucket are unstorable by nature: the server always rejects them,
        so flushing them after max retries loses nothing. Reporting those at
        warning level (as it used to) cried wolf — a week-old queue aging out, or
        a single bucketless event, paged ops the same as real loss. Now the
        benign flush logs at info under a distinct fingerprint; warning is
        reserved for actual loss. The reporter's dedup keeps repeats from
        flooding."""
        if self.error_reporter is None:
            return
        try:
            count = summary.get("count", 0)
            # Default real=count keeps back-compat if a caller passes the old
            # shape (treat as loss rather than silently swallow).
            real = summary.get("real_loss_count", count)
            # bucket TYPES only — bucket ids embed the hostname (often a person's
            # name); don't ship that to the cross-tenant ops ingest (privacy F4).
            types = sorted({str(b).rsplit("_", 1)[0] for b in (summary.get("bucket_ids") or [])})
            buckets = ",".join(types) or "unknown"
            # Age, not wall-clock — a precise activity timestamp anchors a
            # high-resolution timeline in the ops ingest, which the privacy model
            # avoids (privacy F3; same discipline as the blind-tracker report).
            window = self._dropped_window_age(summary.get("oldest"), summary.get("newest"))
            if real > 0:
                other = count - real
                extra = f"; {other} other unstorable" if other > 0 else ""
                # The server's status code, and ONLY that. The full rejection
                # text is kept in the local dead-letter row where an engineer can
                # pull it deliberately; it must not ride to the cross-tenant ops
                # ingest, because a validation error routinely echoes the value
                # it rejected and our event payloads carry window titles
                # (privacy F4, same reason bucket ids are reduced to types).
                # Allowlist a safe shape rather than blocklisting unsafe ones —
                # the ways free text can carry a title are unbounded.
                extra += self._dropped_reason_code(summary.get("last_errors"))
                self.error_reporter.capture(
                    f"Dropped {real} queued event(s) after max retries — the "
                    f"server rejected them; held in dead-letter for replay, not "
                    f"discarded (buckets={buckets}, {window}{extra})",
                    level="warning",
                    tags={"component": "offline-queue"},
                    fingerprint="offline-queue-events-dropped",
                )
            else:
                self.error_reporter.capture(
                    f"Flushed {count} unstorable queued event(s) — stale or no "
                    f"bucket, never server-acceptable (buckets={buckets}, {window})",
                    level="info",
                    tags={"component": "offline-queue"},
                    fingerprint="offline-queue-events-flushed-unstorable",
                )
        except Exception:
            logger.debug("dropped-events report failed", exc_info=True)

    # A definitive rejection is formatted by http_client as
    # ``API error (NNN): <body>``. Anchoring on that PREFIX rather than
    # searching for any 3-digit run is what makes this a PROVENANCE test
    # instead of a shape test: a free-text search happily reports a digit run
    # from a rejected filename ("Bug 404 fix.txt" -> "server status 404") or
    # from our OWN agent-authored reason ("shed after 137 transient failures"
    # -> "server status 137", on a string that says it is not a rejection).
    # Both were live before this anchor existed.
    _SERVER_STATUS_RE = _SERVER_STATUS_RE

    def _report_below_minimum(self, min_version: str) -> None:
        """Tell ops this device is running under the server's version floor.

        The two existing responses are local: a warning in a log that is only
        uploaded on request, and a staged update that a broken updater may never
        apply. So "this machine has been stuck for days" was answerable only by
        visiting it — the fail-closed test ("if this failed, would anything be
        different?") answered no.

        DURATION IS DELIBERATELY NOT TRACKED HERE. The ops ingest keys by
        fingerprint and records first_seen/last_seen, so "below the floor for
        more than a day" is a question the monitor answers from the report
        stream. Persisting a first-seen timestamp on the device would need its
        own table, would reset whenever the queue DB is rebuilt, and would
        answer worse a question the ingest already answers for free.

        Hourly dedup rather than the reporter's 300s default: this is a
        slow-moving condition, and one report an hour is enough to establish a
        span without burying the ingest.

        Swallowed on failure — telemetry must never break the heartbeat it
        rides on.
        """
        if self.error_reporter is None:
            return
        try:
            self.error_reporter.capture(
                f"Agent {AGENT_VERSION} is below the server minimum "
                f"{min_version} and is still running — the update has not "
                f"applied. Window-title categorisation degrades while stuck.",
                level="warning",
                tags={"component": "self-update"},
                fingerprint="agent-below-minimum-version",
                dedup_window=3600.0,
            )
        except Exception:  # noqa: BLE001
            logger.debug("below-minimum report failed", exc_info=True)

    @staticmethod
    def _dropped_reason_code(last_errors) -> str:
        """Render the servers' rejections as bare HTTP statuses, or nothing.

        Answers "malformed event or receiving-side change?" at the class level
        without shipping any server-authored text off the device: only digits
        matched by ``_SERVER_STATUS_RE`` are ever emitted. Anything else yields
        the locally-recorded pointer, so the message cannot carry a payload
        echo.

        Takes the DISTINCT set from ``failed_event_summary``. One drop cycle
        routinely spans several batches with several rejections, and naming one
        of them as "the" cause is a confident wrong answer to the question this
        exists to settle.
        """
        return server_status_summary(last_errors)

    @staticmethod
    def _dropped_window_age(oldest, newest, now: Optional[datetime] = None) -> str:
        """Coarse age + duration of a dropped batch for the ops report — NEVER the
        raw wall-clock event timestamps (those anchor a high-resolution activity
        timeline in the cross-tenant ingest, which the privacy model avoids)."""
        now = now or datetime.now(timezone.utc)

        def _parse(s):
            try:
                d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
                return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                return None

        o, n = _parse(oldest), _parse(newest)
        if o is None:
            return "age unknown"
        age_min = max(0.0, (now - o).total_seconds()) / 60
        span_min = max(0.0, ((n or o) - o).total_seconds()) / 60
        return f"oldest ~{age_min:.0f}m old, spans ~{span_min:.0f}m"

    def _report_upload_failure(self, reason: str) -> None:
        """Surface a logs_requested upload failure to the ops ingest. We can't
        rely on the local log to carry this — it IS the file we failed to send,
        and the admin requested it precisely because the agent is sick. The
        error_reporter is a separate channel that keeps working when the log
        fetch doesn't, so a wedged/failing agent stays diagnosable. The
        reporter's own dedup window keeps this from flooding on each retry."""
        if self.error_reporter is None:
            return
        try:
            self.error_reporter.capture(
                f"Agent log upload failed: {reason}",
                level="warning",
                tags={"component": "log-upload"},
                fingerprint="log-upload-failed",
            )
        except Exception:
            logger.debug("log-upload-failure report failed", exc_info=True)

    @staticmethod
    def _read_log_tail(path, max_bytes: int = 512 * 1024) -> Optional[bytes]:
        """Return up to the last ``max_bytes`` of a log file as VALID UTF-8
        (matching the server's per-file tail cap). Returns ``None`` if it can't
        be read (OSError) and ``b""`` for an empty file — callers treat both as
        "no content" (``if not tail``), so don't rely on None-vs-b"" to
        distinguish unreadable from empty.

        The bytes are normalized to valid UTF-8 (invalid sequences replaced)
        before returning. Older Windows logs were written in cp1252, so a tail
        could carry bytes like \\x97 that make the server's INSERT into the utf8
        `content` column fail with MySQL 1366 — silently dropping the upload.
        New logs are UTF-8 (handler `encoding`), but logs already on disk and
        any stray bytes still need this guard so the upload always stores.
        """
        try:
            size = path.stat().st_size
            with open(path, "rb") as f:
                if size > max_bytes:
                    f.seek(size - max_bytes)
                raw = f.read()
        except OSError:
            return None
        # Re-encode so the result is always valid UTF-8 the server can store.
        return raw.decode("utf-8", errors="replace").encode("utf-8")

    @staticmethod
    def _read_rotated_log_tail(path, max_bytes: int = 512 * 1024) -> bytes:
        """The last ``max_bytes`` of the LOGICAL log, spanning rotations.

        ``_read_log_tail`` reads one file. ``setup_logging`` configures
        ``RotatingFileHandler(maxBytes=5 MiB, backupCount=3)``, so up to ~20 MB
        of history sits beside it in ``betterflow.log.1/.2/.3`` and none of it
        was ever uploaded — the moment the live file rotated, an incident became
        unreachable by any means we have.

        #225, and it cost a real answer: a capture from device 14 on 2026-08-25
        covered only that day, so the line that would have settled #211's
        diagnosis (2026-08-23) was gone and the fix shipped marked *inferred*.

        This deliberately costs nothing from the trade-offs #225 weighed. The
        budget is enforced on the RETURNED bytes, so the payload cannot grow. The
        server still gets one ``log`` file, so the contract is unchanged. Nothing
        new is written to disk. It only fills the SAME budget from further back
        when the live file does not fill it alone — which is exactly the
        post-rotation state that lost the evidence.

        The final cap is not belt-and-braces. ``_read_log_tail`` normalises each
        chunk with ``errors="replace"``, and U+FFFD is THREE bytes, so an invalid
        byte expands 1→3 *after* the budget was decremented. Measured at the real
        512 KB budget, a rotated file of invalid bytes returned **1,572,864** —
        three times the cap. That matters twice over, because the server
        (``AgentLogController``) enforces ``max:1024`` KB and hard-422s above it,
        and ``logs_requested_at`` clears only on success, so the agent would
        retry every heartbeat forever and the admin would never get logs. Below
        that it silently keeps the LAST 512 KB — trimming from the opposite end
        to the one we fill, which would discard exactly the rotated history this
        function exists to deliver.

        When ``betterflow.log`` already exceeds the budget the result is
        byte-identical to reading it alone, so the common case on every device in
        the fleet is untouched.

        Returns oldest-first so a reader can follow it, and ``b""`` when there is
        nothing — callers use ``if not tail``, which is unchanged.
        """
        from pathlib import Path

        live = Path(path)
        # Newest first, taking each file's own tail; reversed at the end.
        candidates = [live] + [
            Path(f"{live}.{i}") for i in range(1, 4)
        ]

        chunks: list[bytes] = []
        remaining = max_bytes
        for candidate in candidates:
            if remaining <= 0:
                break
            # _read_log_tail already normalises each chunk to valid UTF-8, so the
            # concatenation is valid too. One invalid byte makes the server's
            # INSERT fail with MySQL 1366 and drops the whole upload silently.
            part = SyncEngine._read_log_tail(candidate, remaining)
            if not part:
                # None (unreadable) and b"" (empty) both just mean "nothing here";
                # keep going, because one bad rotation must not cost us the rest.
                continue
            chunks.append(part)
            remaining -= len(part)

        joined = b"".join(reversed(chunks))
        if len(joined) > max_bytes:
            # Keep the NEWEST max_bytes — the same end the server keeps, so the
            # two agree instead of the server silently trimming the other one.
            joined = joined[-max_bytes:]
            # A byte slice can land inside a multi-byte character. Drop leading
            # continuation bytes (0x80-0xBF) so the result stays valid UTF-8 —
            # one invalid byte makes the server's INSERT fail with MySQL 1366 and
            # drops the whole upload.
            i = 0
            while i < len(joined) and 0x80 <= joined[i] < 0xC0:
                i += 1
            joined = joined[i:]
        return joined

    @staticmethod
    def _version_below(current: str, minimum: str) -> bool:
        """Compare semver-style version strings.

        Returns True (conservative) on parse failure so the user sees
        the update warning rather than silently skipping it.
        """
        try:
            cur = tuple(int(x.split("-")[0]) for x in current.split(".")[:3])
            min_ = tuple(int(x.split("-")[0]) for x in minimum.split(".")[:3])
            return cur < min_
        except (ValueError, AttributeError):
            logger.warning(f"Cannot parse version strings: current={current!r}, minimum={minimum!r}")
            return True

    def get_status(self) -> dict:
        """Get current sync status."""
        with self._state_lock:
            paused = self._paused
            session_active = self._session_active
            upload_suspended = self._upload_suspended
        aw_running = self.aw.is_running()
        bf_reachable = self.bf.is_reachable() if not paused else False
        queue_size = self.queue.size()
        checkpoints = self.queue.get_all_checkpoints()

        return {
            "paused": paused,
            # The one reader of the suspend marker: without it the flag is a
            # write nothing consumes, and "offline but still capturing" is
            # indistinguishable from "paused" in diagnostics.
            "upload_suspended": upload_suspended,
            "session_active": session_active,
            "aw_running": aw_running,
            "bf_reachable": bf_reachable,
            "queue_size": queue_size,
            "buckets_tracked": len(checkpoints),
            "last_sync": max(checkpoints.values()).isoformat() if checkpoints else None,
        }

    def _advance_checkpoints_to_now(self, reason: str) -> None:
        """Fast-forward all known watcher checkpoints to now.

        This prevents buffered events collected while paused/private from being
        uploaded when syncing resumes. Called on BOTH the enter and the leave of
        a pause/private window — the leave call is the one that skips the window
        AW just recorded (the enter call only drops pre-window buffered events).
        """
        now = datetime.now(timezone.utc)

        # The in-process AFK stream keeps its own checkpoint (not an AW bucket),
        # so advance it here too — otherwise the next _build_inproc_afk
        # reconstructs the paused/private window and bills it (Lucian,
        # 2026-06-22). Done first + unconditionally (independent of the AW bucket
        # fetch below). Forward-only: `now` is always >= the checkpoint here, so
        # this never rewinds the stream.
        if self._afk_inproc_checkpoint is not None:
            self._afk_inproc_checkpoint = now
        self._afk_inproc_pending = None

        # Same for the in-process window stream (its own checkpoint, not an AW
        # bucket) — otherwise the next _build_inproc_window reconstructs the
        # paused/private window and uploads it. Forward-only. (No pending field to
        # clear: it's a per-cycle local now; the commit's forward-only guard stops
        # an in-flight cycle from rewinding past this reset.)
        if self._window_inproc_checkpoint is not None:
            self._window_inproc_checkpoint = now

        # And the in-process INPUT stream. Two steps, both required: advancing
        # the checkpoint alone would leave the counters holding every keystroke
        # typed inside the window, and the next drain bills them against the
        # first span after it. Private Time records nothing — that is the
        # contract, not a tuning choice — so the counts are DISCARDED rather
        # than re-attributed. discard_counts() ignores available() on purpose:
        # the source is normally already stopped by the time we get here (the
        # capture policy stops the watchers first), and a drain-based clear
        # would no-op and strand them until counting resumed.
        if self._input_inproc_checkpoint is not None:
            self._input_inproc_checkpoint = now
        if self.input_source is not None:
            try:
                dropped = self.input_source.discard_counts()
                if any(dropped):
                    # WHETHER a discard happened, never HOW MUCH. Every reason
                    # that reaches this method names a window this machine
                    # contractually does not record — private time, a pause,
                    # outside working hours — and betterflow.log is uploaded to
                    # the server on admin request (_upload_requested_logs). An
                    # exact keystroke/click/scroll volume in that tail is a
                    # low-resolution recording of the window we just refused to
                    # record. The reason alone is what a reader needs to debug
                    # the mechanism.
                    logger.info(
                        "Discarded buffered in-process input counts on %s", reason
                    )
            except Exception as e:
                logger.warning("Discarding in-process input counts failed: %s", e)

        bucket_ids: set[str] = set()
        buckets: list = []

        def _collect(fetcher) -> None:
            try:
                for bucket in fetcher():
                    if bucket.id not in bucket_ids:
                        bucket_ids.add(bucket.id)
                        buckets.append(bucket)
            except (AWClientError, TypeError, AttributeError) as e:
                logger.debug(f"_advance_checkpoints_to_now: {getattr(fetcher, '__name__', '?')}: {e}")

        _collect(self.aw.get_window_buckets)
        _collect(self.aw.get_web_buckets)
        _collect(self.aw.get_afk_buckets)
        _collect(self.aw.get_input_buckets)

        if not bucket_ids:
            return

        # On the way IN, rescue the tail of real work first.
        #
        # This method's own contract is that "the enter call only drops
        # pre-window buffered events" — but those events are not part of the
        # window being excluded. They are seconds the person actually worked in
        # the up-to-60s since the last fetch, immediately before they locked the
        # screen, started a break or turned on Private Time. Losing them was
        # never the intent of any of those three features; it was collateral,
        # and it happened on every single one, fleet-wide.
        #
        # ENTER only. On the way out `now` is the END of the window, so flushing
        # there would upload the very span the caller is excluding — that is the
        # Private Time contract, not a tuning choice.
        if reason in _FLUSH_TAIL_ON_ENTER:
            self._flush_pre_window_tail(buckets, now, reason)

        for bucket_id in bucket_ids:
            self.queue.set_checkpoint(bucket_id, now)

        logger.info(
            f"Advanced checkpoints for {len(bucket_ids)} buckets due to {reason}"
        )

    def _flush_pre_window_tail(self, buckets: list, boundary: datetime, reason: str) -> None:
        """Queue whatever AW recorded between each bucket's checkpoint and
        ``boundary``, so the checkpoint advance that follows discards only the
        window itself.

        Deliberately mirrors _reconcile_backlog, which does the same
        fetch-transform-enqueue and is the proven path: the backend upserts by
        AW event id so re-enqueuing is deduped, window events go through the
        counted-time cache so an already-counted event is a no-op, and
        non-window events pass skip_time_tracking so the daily total is never
        touched twice. A fresh, empty _SyncCycleContext is used for the same
        reason it is there — deterministic, never inherits a prior cycle.

        Bounded and local: the range is normally the ~60s since the last sync,
        ActivityWatch is on localhost and the queue is SQLite, so this adds no
        backend network to a path that runs on the tray thread. Never raises —
        every failure degrades to exactly the old behaviour, never worse.
        """
        cycle = _SyncCycleContext()
        enqueued = 0
        for bucket in buckets:
            try:
                checkpoint = self.queue.get_checkpoint(bucket.id)
                if checkpoint is None or checkpoint >= boundary:
                    continue
                events = self.aw.get_events(
                    bucket.id,
                    start=checkpoint,
                    end=boundary,
                    limit=self._BACKLOG_FETCH_LIMIT,
                )
                if not events:
                    continue
                events.sort(key=lambda e: e.timestamp)  # AW is newest-first
                batch: list[dict] = []
                for event in events:
                    if _is_window_like(bucket.type):
                        batch.extend(
                            self._transform_window_event_with_timeout(
                                event, bucket.id, bucket.type, cycle
                            )
                        )
                    else:
                        transformed = self._transform_event(
                            event, bucket.id, bucket.type,
                            skip_time_tracking=True, cycle=cycle,
                        )
                        if transformed:
                            batch.append(transformed)
                if batch:
                    enqueued += self.queue.enqueue(batch)
            except Exception as e:
                logger.debug("pre-%s tail flush failed for %s: %s", reason, bucket.id, e)
        if enqueued:
            logger.info(
                "Queued %d event(s) captured before %s took effect", enqueued, reason
            )

    def get_today_active_time(self) -> timedelta:
        """Get cumulative active work time for today.

        Only "active" events (engaged work) count toward this total.
        """
        return self._time_tracker.get_today_active_time()

    def shutdown(self) -> None:
        """Shutdown the sync engine gracefully.

        Resets per-session state so the engine can be reused after
        logout/re-login without stale pause, config, or backoff state
        carrying over from the previous session.
        """
        with self._state_lock:
            need_end = self._session_active
            self._session_active = False
            self._paused = False
            self._private_mode = False
            self._config_fetched = False
        self._queue_consecutive_failures = 0
        self._queue_backoff_until = datetime.min.replace(tzinfo=timezone.utc)
        if need_end:
            for attempt in range(2):
                try:
                    self.bf.end_session("app_quit")
                    break
                except BetterFlowClientError:
                    if attempt == 0:
                        logger.debug("Session end attempt 1 failed, retrying")

        # Free fraud detector accumulators
        self._activity_analyzer.clear()
        # Clear session-scoped category state for clean re-login
        with self._category_cache_lock:
            self._category_cache = None
            self._persisted_fallbacks.clear()
        # Clear dedup caches so a re-login (possibly as a different user on
        # a shared machine) doesn't silently skip events that were already
        # sent in the previous session.
        with self._cache_lock:
            self._sent_cache = BoundedLRU(maxsize=5_000)
            self._gap_filled_originals = BoundedLRU(maxsize=5_000)
            self._time_cache = BoundedLRU(maxsize=5_000)
        # Re-sending events on re-login is safe (server dedups by id), but the
        # daily total must NOT be re-counted — restore the persisted per-event
        # counts so a same-day re-login doesn't double-count the tray's hours.
        self._load_counted_time_cache()
        # Close any open foreground-activity session so is_active_dev_session()
        # doesn't stay True across a logout. The last per-cycle snapshot already
        # uploaded the span, so the discarded return here loses nothing.
        if self._foreground_detector is not None:
            try:
                self._foreground_detector.flush()
            except Exception as e:
                logger.debug("foreground detector flush on shutdown failed: %s", e)
        # Same for any open call / mic session: both stay open across sync
        # boundaries now (per-cycle snapshot, not flush), so close them here or
        # is_in_call()/is_mic_meeting_active() survives the logout. The final
        # event is SENT NOW when possible: shutdown runs on logout, and a row
        # left in the (account-agnostic) offline queue would be delivered
        # under whoever logs in NEXT on a shared machine. _deliver_final_event
        # queues only on non-auth send failure (auth errors DROP — see its
        # docstring), and the deterministic id makes any replay an upsert,
        # never a duplicate.
        if self._call_detector is not None:
            try:
                remaining = self._call_detector.flush()
                if remaining:
                    self._deliver_final_event(
                        self._make_call_bf_event(remaining), "shutdown call flush"
                    )
            except Exception as e:
                logger.debug("call detector flush on shutdown failed: %s", e)
        if self._mic_detector is not None:
            try:
                ended = self._mic_detector.flush()
                if ended:
                    self._deliver_final_event(
                        self._stamp_project(ended), "shutdown mic flush"
                    )
            except Exception as e:
                logger.debug("mic detector flush on shutdown failed: %s", e)
        # Close time tracker
        self._time_tracker.close()
