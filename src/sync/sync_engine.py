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
from zoneinfo import ZoneInfo

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
    from .aw_client import AWClientError, AWEvent, BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT, BUCKET_TYPE_AFK, BUCKET_TYPE_AFK_ALT, BUCKET_TYPE_WEB, BUCKET_TYPE_INPUT, BUCKET_TYPE_CALL
    from .bf_client import BetterFlowClientError, BetterFlowAuthError
    from .protocols import AWClientProtocol, BFClientProtocol, OfflineQueueProtocol
    from .activity_analyzer import ActivityAnalyzer, EngagementThresholds
    from .daily_time_tracker import DailyTimeTracker
    from .call_detector import CallDetector, CallEvent
    from .os_idle import get_system_idle_seconds
except ImportError:
    from browser_tracker import is_browser_app
    from config import Config
    from sync.aw_client import AWClientError, AWEvent, BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT, BUCKET_TYPE_AFK, BUCKET_TYPE_AFK_ALT, BUCKET_TYPE_WEB, BUCKET_TYPE_INPUT, BUCKET_TYPE_CALL
    from sync.bf_client import BetterFlowClientError, BetterFlowAuthError
    from sync.protocols import AWClientProtocol, BFClientProtocol, OfflineQueueProtocol
    from sync.activity_analyzer import ActivityAnalyzer, EngagementThresholds
    from sync.daily_time_tracker import DailyTimeTracker
    from sync.call_detector import CallDetector, CallEvent
    from sync.os_idle import get_system_idle_seconds

logger = logging.getLogger(__name__)


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
    errors: list[str] = field(default_factory=list)
    queued_bucket_ids: set = field(default_factory=set)
    _should_heartbeat: bool = False

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
        self._private_mode = False
        self._private_start: Optional[datetime] = None
        self._current_project: Optional[dict] = None
        self._session_active = False
        self._config_fetched = False
        self._heartbeat_count = 0
        # Send heartbeat every 5 sync cycles (5 * 60s = 5 min default)
        self._heartbeat_interval = 5
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

        # Queue retry backoff
        self._queue_consecutive_failures = 0
        self._queue_backoff_until = datetime.min.replace(tzinfo=timezone.utc)

        # Clock skew: track server-vs-local time offset (seconds, positive = server ahead)
        self._server_time_offset: Optional[float] = None
        self._hostname = socket.gethostname()

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
            CallDetector(min_duration=config.call_detection.min_call_duration)
            if config.call_detection.enabled
            else None
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

    @property
    def is_paused(self) -> bool:
        with self._state_lock:
            return self._paused

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
        return bool(detector and detector.is_in_call())

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

    def fetch_server_config(self) -> None:
        """Fetch and apply server-side configuration."""
        try:
            server_config = self.bf.get_config()
            self.config.update_from_server(server_config)
            self._config_fetched = True
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

        # Daily housekeeping before anything else, so it still runs while paused:
        # a long-running agent that never restarts across midnight would
        # otherwise never prune persisted counted-time (prune_counted_time only
        # ran at startup), leaking a day's rows every day it stays up.
        self._maybe_rollover_counted_day()

        with self._state_lock:
            if self._paused or self._private_mode:
                return stats

        # Fetch server config on first successful sync
        if not self._config_fetched and self.bf.is_reachable():
            self.fetch_server_config()

        # Check ActivityWatch
        if not self.aw.is_running():
            stats.errors.append("ActivityWatch is not running")
            # Finalize any in-progress call before bailing. Otherwise is_in_call()
            # stays True for the whole outage (sync returns here every cycle, so
            # the end-of-sync flush at line ~522 never runs), and the idle guard
            # in IdleManager keeps suppressing idle long after the call ended —
            # painting the post-call AFK stretch as worked time. The observed call
            # portion is still recorded if the backend is reachable; if AW comes
            # back mid-call a fresh call starts cleanly.
            #
            # That recovery can emit a SECOND event for the same meeting (the
            # continuation is picked up from the un-advanced checkpoint). Both
            # carry the deterministic id call_<app>_<start_ts> and the backend
            # upserts call events by id, so the overlapping recovered portion
            # supersedes this flushed one rather than double-billing.
            if self._call_detector:
                remaining = self._call_detector.flush()
                if remaining and self.bf.is_reachable():
                    # _send_events may set stats.queued_bucket_ids on failure; we
                    # return immediately and intentionally don't act on it — call
                    # events go to the synthetic call bucket, not a checkpointed AW
                    # bucket, so there is no checkpoint to withhold.
                    self._send_events([self._make_call_bf_event(remaining)], stats)
                    stats.calls_detected += 1
            return stats

        # Start session if needed (attempt directly; no pre-check to avoid TOCTOU)
        # Retry once on transient failure (N13)
        with self._state_lock:
            need_session = not self._session_active
        if need_session:
            for attempt in range(2):
                try:
                    self.bf.start_session()
                    with self._state_lock:
                        self._session_active = True
                    break
                except BetterFlowClientError as e:
                    if attempt == 0:
                        logger.debug(f"Session start attempt 1 failed: {e}, retrying")
                    else:
                        logger.warning(f"Failed to start session after retry: {e}")

        # Record an activity sample for the in-process AFK timeline (no-op when
        # no source is wired or the OS idle clock is unavailable). Done every
        # cycle so the sample log stays dense regardless of bucket-fetch outcome.
        if self.afk_source is not None:
            try:
                self.afk_source.record_sample(datetime.now(timezone.utc))
            except Exception as e:
                logger.debug("afk_source.record_sample failed: %s", e)

        # Get buckets to sync
        try:
            window_buckets = self.aw.get_window_buckets()
            web_buckets = self.aw.get_web_buckets()
            afk_buckets = self.aw.get_afk_buckets()
            input_buckets = self.aw.get_input_buckets()
        except AWClientError as e:
            stats.errors.append(f"Failed to get buckets: {e}")
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

        # Sync window buckets with gap-filling
        all_events, call_events, pending_checkpoints = self._sync_window_buckets(
            window_buckets, stats, cycle
        )

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
        for bucket in web_buckets + afk_buckets + input_buckets:
            if skip_external_afk and _is_afk_like(bucket.type):
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

        # Flush any ongoing call at sync boundary
        if self._call_detector:
            remaining = self._call_detector.flush()
            if remaining:
                call_events.append(self._make_call_bf_event(remaining))
        if call_events:
            stats.calls_detected += len(call_events)
            all_events.extend(call_events)

        # Send events, then advance checkpoints per-bucket.
        self._send_and_advance_checkpoints(all_events, pending_checkpoints, stats)
        # Commit the in-process AFK checkpoint only if its bucket wasn't queued
        # (the send was confirmed) — otherwise rebuild it next cycle (finding B).
        self._commit_inproc_afk_checkpoint(stats)

        # Process offline queue if we're online
        if self.bf.is_reachable() and not self.queue.is_empty():
            self._process_queue(stats)

        # Check heartbeat counter — actual HTTP call is deferred to
        # after sync() returns so _sync_lock is not held during the
        # blocking network request.
        with self._state_lock:
            self._heartbeat_count += 1
            should_heartbeat = self._heartbeat_count >= self._heartbeat_interval
            if should_heartbeat:
                self._heartbeat_count = 0
        stats._should_heartbeat = should_heartbeat

        return stats

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
        return _SyncCycleContext(has_input_data=len(input_events_for_analysis) > 0)

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
        """True if the event's start falls inside the server-enforced working-
        hours window. When the schedule is not enforced (B2B / unrestricted)
        this is always True. Evaluated in the schedule's timezone, falling back
        to the machine's local timezone when none is provided."""
        wh = self.config.working_hours
        if not getattr(wh, "enforced", False):
            return True
        try:
            tz = ZoneInfo(wh.timezone) if wh.timezone else None
        except Exception:
            tz = None
        local = event.timestamp.astimezone(tz) if tz else event.timestamp.astimezone()
        if wh.working_days and local.isoweekday() not in wh.working_days:
            return False
        hhmm = local.strftime("%H:%M")
        return wh.work_start <= hhmm <= wh.work_end

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
        new_events = [e for e in events if e.timestamp > checkpoint]
        if not new_events and fetch_end < now:
            self.queue.set_checkpoint_forward(bucket_id, fetch_end)
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
        # Feed window events to activity analyzer for window change detection
        if bucket_type in (BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT):
            self._activity_analyzer.add_window_events(events)

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

            if _is_window_like(bucket_type):
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
            else:
                stats.events_filtered += 1

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
        if events:
            newest = max(events, key=lambda e: e.timestamp)
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
        with self._state_lock:
            project = self._current_project
        if project:
            result["project_id"] = project["id"]

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
    ) -> None:
        """Send a duration event for a state-span (break/idle/private).

        Consolidates three formerly-identical send_*_event helpers. The only
        variation between them was the ``kind`` string used in the id prefix,
        bucket_type, and data.status field.
        """
        if end is None:
            end = datetime.now(timezone.utc)
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
            "bucket_type": bucket_type,
            "data": {"status": kind},
        }
        with self._state_lock:
            project = self._current_project
        if project:
            event["project_id"] = project["id"]
        # bf.send_events() returns SyncResult(success=False) on network errors —
        # it does NOT raise BetterFlowClientError — so the previous `except`
        # block was unreachable and break/idle/private events were silently
        # dropped on the first offline cycle. Inspect the result instead.
        try:
            result = self.bf.send_events([event])
        except BetterFlowAuthError as e:
            # Auth errors are not retryable without re-login; queueing risks
            # sending under a different user's session after re-auth. Drop.
            logger.warning("Auth error sending %s event — not queued: %s", bucket_type, e)
            return
        if result.success:
            logger.info("Sent %s event (%.0fs)", bucket_type, duration)
        else:
            logger.warning("Failed to send %s event: %s — queueing", bucket_type, result.error or "unknown")
            self.queue.enqueue([event])

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

    def _make_call_bf_event(self, call_event: "CallEvent") -> dict:
        """Convert a CallEvent into a BetterFlow event dict.

        Includes a deterministic id so the server can dedupe and so the
        offline-queue partial-success path (_process_queue) can match the
        event against the server's accepted_ids list. Without an id, a
        partial server reply that omits this event would otherwise be
        ambiguous between "accepted" and "not yet acknowledged".
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
                "status": "completed",
            },
        }
        with self._state_lock:
            project = self._current_project
        if project:
            result["project_id"] = project["id"]
        return result

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
        with self._state_lock:
            project = self._current_project
        if project:
            result["project_id"] = project["id"]
        return result

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
        if now < cp or gap > self.afk_source.retention_seconds:
            logger.info(
                "in-process AFK: re-seeding checkpoint (gap %.0fs) — not billing "
                "the unobserved window", gap,
            )
            self._afk_inproc_checkpoint = now
            self._afk_inproc_pending = None
            return []

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
        with self._state_lock:
            project = self._current_project
        events = self.afk_source.build_afk_events(
            cp, finalize_to,
            project_id=project["id"] if project else None,
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
                result = self.bf.send_events(batch)
                if result.success:
                    stats.events_sent += result.events_synced
                else:
                    if result.accepted_ids:
                        # True partial success: re-queue only the events the
                        # server did NOT accept.
                        accepted_set = set(result.accepted_ids)
                        failed = [e for e in batch if e.get("id") not in accepted_set]
                        stats.events_sent += len(batch) - len(failed)
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

    def _process_queue(self, stats: SyncStats) -> None:
        """Process offline queue with exponential backoff.

        Capped at 30s wall-clock time to prevent tying up the sync thread.
        """
        # Remove events that exceeded retry limit. Surface the drop to ops FIRST
        # — with the server now confirming delivery per-event (accepted_ids),
        # anything that still exhausts its retries is a genuine permanent
        # rejection (out-of-range timestamp, server validation), i.e. real
        # activity we are about to lose. Report it instead of dropping silently
        # (the blindness that hid prior data loss for days).
        drop_summary = self.queue.failed_event_summary(max_retries=5)
        if drop_summary.get("count", 0) > 0:
            self._report_dropped_events(drop_summary)
        self.queue.remove_failed(max_retries=5)

        # Skip queue processing if in backoff period
        now = datetime.now(timezone.utc)
        if now < self._queue_backoff_until:
            return

        # Process queue in batches
        deadline = time.monotonic() + self._QUEUE_PROCESS_TIMEOUT
        batch_size = self.config.sync.batch_size
        processed = 0
        max_per_cycle = batch_size * 10  # Max 10 batches per cycle

        while processed < max_per_cycle and time.monotonic() < deadline:
            queued = self.queue.dequeue(batch_size)
            if not queued:
                break

            events = [q.event_data for q in queued]
            event_ids = [q.id for q in queued]

            try:
                result = self.bf.send_events(events)
                if result.success:
                    self.queue.remove(event_ids)
                    stats.events_sent += result.events_synced
                    processed += len(events)
                    self._queue_consecutive_failures = 0
                elif result.accepted_ids:
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
                        self._queue_consecutive_failures = 0
                    if failed_ids:
                        self.queue.increment_retry(failed_ids)
                    processed += len(succeeded_ids)
                else:
                    self.queue.increment_retry(event_ids)
                    self._apply_queue_backoff()
                    break
            except BetterFlowAuthError:
                # Auth errors won't self-heal with retries; re-raise so
                # the caller's auth handler can trigger re-login.
                raise
            except BetterFlowClientError:
                self.queue.increment_retry(event_ids)
                self._apply_queue_backoff()
                break

    def _apply_queue_backoff(self) -> None:
        """Apply exponential backoff for queue processing failures."""
        self._queue_consecutive_failures += 1
        # 60s, 120s, 240s, 480s, max 600s (10 min)
        delay = min(60 * (2 ** (self._queue_consecutive_failures - 1)), 600)
        self._queue_backoff_until = datetime.now(timezone.utc) + timedelta(seconds=delay)
        logger.info(f"Queue backoff: retry in {delay}s (failure #{self._queue_consecutive_failures})")

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

    def _send_heartbeat(self) -> Optional["BetterFlowAuthError"]:
        """Send heartbeat to server and process commands.

        Returns BetterFlowAuthError on 401/403 so the caller can trigger
        re-login. Other client errors are logged at debug and swallowed —
        a transient heartbeat failure should not surface as a user error.
        """
        try:
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
        log_tail = self._read_log_tail(log_dir / "betterflow.log")
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
        """Surface permanently-dropped queued events to the ops ingest. These
        are events the server rejected on every retry until they hit the ceiling
        — real captured activity that will never reach the dashboard. The
        reporter's dedup window keeps repeated drops from flooding."""
        if self.error_reporter is None:
            return
        try:
            count = summary.get("count", 0)
            buckets = ",".join(summary.get("bucket_ids") or []) or "unknown"
            span = f"{summary.get('oldest')}..{summary.get('newest')}"
            self.error_reporter.capture(
                f"Dropped {count} queued event(s) after max retries "
                f"(buckets={buckets}, span={span})",
                level="warning",
                tags={"component": "offline-queue"},
                fingerprint="offline-queue-events-dropped",
            )
        except Exception:
            logger.debug("dropped-events report failed", exc_info=True)

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
        aw_running = self.aw.is_running()
        bf_reachable = self.bf.is_reachable() if not paused else False
        queue_size = self.queue.size()
        checkpoints = self.queue.get_all_checkpoints()

        return {
            "paused": paused,
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

        bucket_ids: set[str] = set()

        def _collect(fetcher) -> None:
            try:
                for bucket in fetcher():
                    bucket_ids.add(bucket.id)
            except (AWClientError, TypeError, AttributeError) as e:
                logger.debug(f"_advance_checkpoints_to_now: {getattr(fetcher, '__name__', '?')}: {e}")

        _collect(self.aw.get_window_buckets)
        _collect(self.aw.get_web_buckets)
        _collect(self.aw.get_afk_buckets)
        _collect(self.aw.get_input_buckets)

        if not bucket_ids:
            return

        for bucket_id in bucket_ids:
            self.queue.set_checkpoint(bucket_id, now)

        logger.info(
            f"Advanced checkpoints for {len(bucket_ids)} buckets due to {reason}"
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
        # Close time tracker
        self._time_tracker.close()
