"""Sync engine - orchestrates data flow from ActivityWatch to BetterFlow."""

import logging
import math
import re
import socket
import threading
import time
from collections import OrderedDict
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
    from ..config import Config
    from .aw_client import AWClientError, AWEvent, BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT, BUCKET_TYPE_AFK, BUCKET_TYPE_AFK_ALT, BUCKET_TYPE_WEB, BUCKET_TYPE_INPUT, BUCKET_TYPE_CALL
    from .bf_client import BetterFlowClientError, BetterFlowAuthError
    from .protocols import AWClientProtocol, BFClientProtocol, OfflineQueueProtocol
    from .activity_analyzer import ActivityAnalyzer, EngagementThresholds
    from .daily_time_tracker import DailyTimeTracker
    from .call_detector import CallDetector, CallEvent
except ImportError:
    from config import Config
    from sync.aw_client import AWClientError, AWEvent, BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT, BUCKET_TYPE_AFK, BUCKET_TYPE_AFK_ALT, BUCKET_TYPE_WEB, BUCKET_TYPE_INPUT, BUCKET_TYPE_CALL
    from sync.bf_client import BetterFlowClientError, BetterFlowAuthError
    from sync.protocols import AWClientProtocol, BFClientProtocol, OfflineQueueProtocol
    from sync.activity_analyzer import ActivityAnalyzer, EngagementThresholds
    from sync.daily_time_tracker import DailyTimeTracker
    from sync.call_detector import CallDetector, CallEvent

logger = logging.getLogger(__name__)


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


class SyncEngine:
    """Core sync engine that orchestrates AW -> BetterFlow data flow."""

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
    ):
        self.aw = aw
        self.bf = bf
        self.queue = queue
        self.config = config
        self._on_config_updated = on_config_updated
        self._display_tracker = display_tracker
        self._paused = False
        self._private_mode = False
        self._private_start: Optional[datetime] = None
        self._current_project: Optional[dict] = None
        self._session_active = False
        self._config_fetched = False
        self._heartbeat_count = 0
        # Send heartbeat every 5 sync cycles (5 * 60s = 5 min default)
        self._heartbeat_interval = 5

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
        self._has_input_data = False  # Set to True when input buckets exist
        self._latest_input_at: Optional[datetime] = None
        self._current_afk_events: list[AWEvent] = []  # AFK events for current sync cycle
        self._afk_watcher_available = False  # True when AFK buckets exist this cycle

        # Call/meeting detection
        self._call_detector: Optional[CallDetector] = (
            CallDetector(min_duration=config.call_detection.min_call_duration)
            if config.call_detection.enabled
            else None
        )

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
            self._paused = False

    @property
    def is_paused(self) -> bool:
        with self._state_lock:
            return self._paused

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

        with self._state_lock:
            if self._paused or self._private_mode:
                return stats

        # Fetch server config on first successful sync
        if not self._config_fetched and self.bf.is_reachable():
            self.fetch_server_config()

        # Check ActivityWatch
        if not self.aw.is_running():
            stats.errors.append("ActivityWatch is not running")
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

        # Fetch input events for activity analysis before processing window events.
        # The lookback must cover both the engagement window and the full AFK
        # grace period so we can cap counted time at last_input + afk_timeout.
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
        self._has_input_data = len(input_events_for_analysis) > 0
        self._latest_input_at = None
        for ev in input_events_for_analysis:
            if ev.presses <= 0 and ev.clicks <= 0 and ev.scrolls <= 0:
                continue

            event_end = ev.timestamp + timedelta(seconds=ev.duration)
            if self._latest_input_at is None or event_end > self._latest_input_at:
                self._latest_input_at = event_end

        # Sync window buckets with gap-filling
        all_events = []
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
                    self._current_afk_events = afk_events

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
                        raw_events, bucket.id, bucket.type, stats
                    )
                    all_events.extend(transformed)
                    if checkpoint:
                        pending_checkpoints.append(checkpoint)
                stats.buckets_synced += 1
            except AWClientError as e:
                stats.errors.append(f"Failed to sync bucket {bucket.id}: {e}")

        # Clear window-specific AFK context before processing non-window buckets
        # to prevent AFK events from one window bucket leaking into unrelated buckets.
        self._current_afk_events = []

        # Sync non-window buckets normally
        for bucket in web_buckets + afk_buckets + input_buckets:
            try:
                events, checkpoint = self._sync_bucket(bucket.id, bucket.type, stats)
                all_events.extend(events)
                if checkpoint:
                    pending_checkpoints.append(checkpoint)
                stats.buckets_synced += 1
            except AWClientError as e:
                stats.errors.append(f"Failed to sync bucket {bucket.id}: {e}")

        # Flush any ongoing call at sync boundary
        if self._call_detector:
            remaining = self._call_detector.flush()
            if remaining:
                call_events.append(self._make_call_bf_event(remaining))
        if call_events:
            stats.calls_detected += len(call_events)
            all_events.extend(call_events)

        # Send events, then advance checkpoints per-bucket. Only hold back
        # checkpoints for buckets that had events queued (partial failure).
        # Previously this was all-or-nothing: if ANY event was queued, NO
        # checkpoint advanced, causing indefinite re-fetch of already-sent
        # events that slowly filled the dedup LRU cache.
        if all_events:
            pre_queued = stats.events_queued
            self._send_events(all_events, stats)
            if stats.events_queued == pre_queued:
                # Full success — advance all checkpoints
                for bucket_id, ts, event_id in pending_checkpoints:
                    self.queue.set_checkpoint(bucket_id, ts, event_id)
            else:
                # Partial failure — advance only buckets whose events
                # were all sent (none queued).
                for bucket_id, ts, event_id in pending_checkpoints:
                    if bucket_id not in stats.queued_bucket_ids:
                        self.queue.set_checkpoint(bucket_id, ts, event_id)
        elif pending_checkpoints:
            # All events were dedup-filtered (already sent); safe to advance.
            for bucket_id, ts, event_id in pending_checkpoints:
                self.queue.set_checkpoint(bucket_id, ts, event_id)

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

    def _fetch_bucket_events(
        self, bucket_id: str, stats: SyncStats
    ) -> tuple[list[AWEvent], datetime]:
        """Fetch events from a bucket with lookback window.

        Returns (events, lookback_start) — events sorted oldest-first.
        """
        checkpoint = self.queue.get_checkpoint(bucket_id)
        if checkpoint is None:
            # First sync for this bucket — start from now so we don't
            # retroactively sync old AW events that accumulated before
            # BetterFlow was running.  Persist immediately so the next
            # cycle uses the 2-min lookback instead of resetting to "now".
            checkpoint = datetime.now(timezone.utc)
            self.queue.set_checkpoint(bucket_id, checkpoint)
            lookback_start = checkpoint
        else:
            lookback_start = checkpoint - timedelta(minutes=2)

        events = self.aw.get_events_since(
            bucket_id, lookback_start, limit=self.config.sync.batch_size
        )
        stats.events_fetched += len(events)

        # AW returns newest-first; sort oldest-first for gap-filling
        events.sort(key=lambda e: e.timestamp)
        return events, lookback_start

    def _transform_and_checkpoint(
        self,
        events: list[AWEvent],
        bucket_id: str,
        bucket_type: str,
        stats: SyncStats,
    ) -> tuple[list[dict], Optional[tuple[str, datetime, Optional[int]]]]:
        """Transform events to BetterFlow format and compute pending checkpoint.

        Returns (transformed_events, pending_checkpoint).
        The caller must commit the checkpoint AFTER send_events succeeds.

        Skips events already sent with unchanged duration (dedup).
        Re-sends if duration has grown (heartbeat extension).
        Returns (transformed_events, pending_checkpoint) — caller commits the
        checkpoint only after a successful send (N5).
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

            if bucket_type in (BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT, BUCKET_TYPE_WEB):
                transformed_events = self._transform_window_event_with_timeout(
                    event, bucket_id, bucket_type
                )
            else:
                transformed_event = self._transform_event(event, bucket_id, bucket_type)
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
            and bucket_type in (BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT, BUCKET_TYPE_WEB)
            and self._has_input_data
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

    @staticmethod
    def _overlap_range(
        start: datetime,
        end: datetime,
        other_start: datetime,
        other_end: datetime,
    ) -> Optional[tuple[datetime, datetime]]:
        """Return the overlapped range, if any."""
        overlap_start = max(start, other_start)
        overlap_end = min(end, other_end)
        if overlap_end <= overlap_start:
            return None

        return overlap_start, overlap_end

    def _active_ranges_from_afk(self, event: AWEvent) -> list[tuple[datetime, datetime]]:
        """Return the non-AFK slices for a window/web event."""
        event_start = event.timestamp
        event_end = event.timestamp + timedelta(seconds=event.duration)
        afk_ranges: list[tuple[datetime, datetime]] = []

        for afk_event in self._current_afk_events:
            if afk_event.status != "afk":
                continue

            afk_start = afk_event.timestamp
            afk_end = afk_event.timestamp + timedelta(seconds=afk_event.duration)
            overlap = self._overlap_range(event_start, event_end, afk_start, afk_end)
            if overlap is not None:
                afk_ranges.append(overlap)

        if not afk_ranges:
            return [(event_start, event_end)]

        afk_ranges.sort(key=lambda item: item[0])
        merged_afk: list[tuple[datetime, datetime]] = []
        for start, end in afk_ranges:
            if not merged_afk or start > merged_afk[-1][1]:
                merged_afk.append((start, end))
            else:
                merged_afk[-1] = (merged_afk[-1][0], max(merged_afk[-1][1], end))

        active_ranges: list[tuple[datetime, datetime]] = []
        cursor = event_start
        for afk_start, afk_end in merged_afk:
            if afk_start > cursor:
                active_ranges.append((cursor, afk_start))
            cursor = max(cursor, afk_end)
            if cursor >= event_end:
                break

        if cursor < event_end:
            active_ranges.append((cursor, event_end))

        return active_ranges

    def _cap_ranges_to_input_timeout(
        self,
        ranges: list[tuple[datetime, datetime]],
    ) -> list[tuple[datetime, datetime]]:
        """Cap counted ranges at last confirmed input + AFK timeout."""
        if self._latest_input_at is None:
            return list(ranges)

        timeout_cutoff = self._latest_input_at + timedelta(
            minutes=self.config.aw.afk_timeout_minutes
        )
        capped: list[tuple[datetime, datetime]] = []
        for start, end in ranges:
            if start >= timeout_cutoff:
                continue

            capped_end = min(end, timeout_cutoff)
            if capped_end > start:
                capped.append((start, capped_end))

        return capped

    def _transform_window_event_with_timeout(
        self,
        event: AWEvent,
        bucket_id: str,
        bucket_type: str,
    ) -> list[dict]:
        """Transform only the countable slices of a long window/web event."""
        event_start = event.timestamp
        event_end = event.timestamp + timedelta(seconds=event.duration)
        ranges: list[tuple[datetime, datetime]]
        if self._afk_watcher_available:
            ranges = self._active_ranges_from_afk(event)
        else:
            ranges = [(event_start, event_end)]

        # Apply input timeout cap when input data is available and recent
        # enough to be relevant. The AFK watcher has a lagging timeout
        # (typically 5 min) during which it reports "not-afk" despite no
        # actual input. The input cap trims those trailing minutes for more
        # accurate time. But when input data is stale (e.g. input watcher
        # crashed 30 min ago), AFK data alone is authoritative.
        input_is_recent = (
            self._has_input_data
            and self._latest_input_at is not None
            and (event_end - self._latest_input_at).total_seconds()
            < self.config.aw.afk_timeout_minutes * 60 * 2
        )
        if input_is_recent:
            active_ranges = self._cap_ranges_to_input_timeout(ranges)
        else:
            active_ranges = ranges
        if not active_ranges:
            logger.info(
                "Window event skipped after inactivity cutoff/AFK overlap: "
                "event=%s->%s",
                event.timestamp.isoformat(),
                event_end.isoformat(),
            )
            return []

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
                forced_activity_state=None if self._has_input_data else "active",
                custom_event_id=f"{event.id}:{idx}",
                skip_time_tracking=True,  # tracked per-event below
            )
            if transformed_event:
                transformed.append(transformed_event)

        # Track time at the EVENT level (not per-segment) to avoid
        # double-counting or undercounting when segmentation changes
        # across cycles (e.g. AFK splits differ between syncs).
        if transformed and total_active_duration > 0:
            event_date = event.timestamp.astimezone().date()
            time_key = (bucket_id, event.id)
            with self._cache_lock:
                prev_counted = self._time_cache.get(time_key, 0.0)
                delta = total_active_duration - prev_counted
                if delta > 0:
                    self._time_cache[time_key] = total_active_duration
            if delta > 0:
                self._time_tracker.add_active_time(delta, event_date)

        return transformed

    def _sync_bucket(
        self, bucket_id: str, bucket_type: str, stats: SyncStats
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
        return self._transform_and_checkpoint(events, bucket_id, bucket_type, stats)

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
        return all_afk

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
    ) -> Optional[dict]:
        """Transform an ActivityWatch event to BetterFlow format.

        Sends raw data to the server — the backend handles privacy
        (title hashing, URL domain extraction) based on device settings.
        """
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
        if bucket_type in (BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT, BUCKET_TYPE_WEB):
            if event.duration < self.config.sync.min_window_event_seconds:
                return None
        elif event.duration < 0.5:
            return None

        # Build data object
        result_bucket_type = bucket_type
        data = {}

        if bucket_type in (BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT, BUCKET_TYPE_WEB):
            data["app"] = app[:MAX_APP_LENGTH] if app else app
            title = event.title
            data["title"] = title[:MAX_TITLE_LENGTH] if title else title
            if event.url:
                url = event.url
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
                    data["page_category"] = self._infer_page_category(event.url, event.title)

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
        elif bucket_type in (BUCKET_TYPE_AFK, BUCKET_TYPE_AFK_ALT):
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

        # Add activity classification for window events
        # (fraud assessment is batched per cycle in _transform_and_checkpoint)
        activity_state: str | None = None
        if bucket_type in (BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT, BUCKET_TYPE_WEB):
            if forced_activity_state is not None:
                activity_state = forced_activity_state
                result["activity_state"] = activity_state
            elif self._has_input_data:
                activity_state = self._activity_analyzer.get_activity_state(event.timestamp)
                activity_metrics = self._activity_analyzer.get_raw_metrics(event.timestamp)

                result["activity_state"] = activity_state
                result["activity_metrics"] = activity_metrics.to_dict()
            else:
                # No input watcher - use AFK data to determine activity.
                event_end = event.timestamp + timedelta(seconds=event.duration)
                has_afk = bool(self._current_afk_events)

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
                        event.timestamp, event_end, self._current_afk_events
                    )
                    # Fallback: treat as active when event end falls inside
                    # a not-afk span, even if AFK doesn't fully cover start.
                    if not is_active:
                        probe_time = event_end - timedelta(milliseconds=1)
                        is_active = (
                            self._status_at(probe_time, self._current_afk_events)
                            == "not-afk"
                        )
                    activity_state = "active" if is_active else "inactive"
                    if not is_active:
                        logger.debug(
                            f"Window event classified inactive: "
                            f"afk_count={len(self._current_afk_events)}, "
                            f"event={event.timestamp.isoformat()}->{event_end.isoformat()}"
                        )
                else:
                    # AFK watcher is running and confirms user was idle
                    activity_state = "inactive"
                    logger.debug(
                        f"Window event classified inactive: "
                        f"afk_count={len(self._current_afk_events)}, "
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
                time_key = (bucket_id, result["id"])
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

        return result

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

    def _send_events(self, events: list[dict], stats: SyncStats) -> None:
        """Send events to BetterFlow or queue if offline."""
        # Batch events
        batch_size = self.config.sync.batch_size
        batches = [events[i : i + batch_size] for i in range(0, len(events), batch_size)]

        for i, batch in enumerate(batches):
            try:
                result = self.bf.send_events(batch)
                if result.success:
                    stats.events_sent += result.events_synced
                else:
                    # Partial batch: only re-queue events the server didn't accept
                    if result.accepted_ids:
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
                        # N11: server returned non-success without accepted_ids.
                        # This branch also covers network failures: bf_client
                        # catches BetterFlowClientError internally and returns
                        # SyncResult(success=False), so there is no separate
                        # network-error except branch — see Important-2 in
                        # commit history.
                        logger.warning(
                            "Server returned partial failure without accepted_ids - "
                            "re-queuing entire batch"
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
        # Remove events that exceeded retry limit
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

    def _send_heartbeat(self) -> Optional["BetterFlowAuthError"]:
        """Send heartbeat to server and process commands.

        Returns BetterFlowAuthError on 401/403 so the caller can trigger
        re-login. Other client errors are logged at debug and swallowed —
        a transient heartbeat failure should not surface as a user error.
        """
        try:
            response = self.bf.heartbeat()

            # Handle server commands
            commands = response.get("commands", [])
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
            min_version = response.get("minimum_agent_version")
            if min_version and self._version_below(AGENT_VERSION, min_version):
                logger.warning(
                    f"Agent {AGENT_VERSION} is below minimum {min_version} — update required"
                )

            # Clock skew detection: compare server time with local time
            server_time_str = response.get("server_time")
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
            if response.get("config_updated"):
                self.fetch_server_config()

        except BetterFlowAuthError as e:
            # Surface auth errors to the caller so re-login can fire.
            # The generic BetterFlowClientError handler below would otherwise
            # swallow this at debug level (auth-error subclasses client-error).
            logger.warning("Heartbeat auth error — session likely expired: %s", e)
            return e
        except BetterFlowClientError as e:
            logger.debug(f"Heartbeat failed: {e}")
        return None

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
        uploaded when syncing resumes.
        """
        now = datetime.now(timezone.utc)
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
        # Close time tracker
        self._time_tracker.close()
