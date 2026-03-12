"""Sync engine - orchestrates data flow from ActivityWatch to BetterFlow."""

import logging
import threading
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
    from ..config import Config, PrivacySettings
    from .aw_client import AWClientError, AWEvent, BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT, BUCKET_TYPE_AFK, BUCKET_TYPE_AFK_ALT, BUCKET_TYPE_WEB, BUCKET_TYPE_INPUT
    from .bf_client import BetterFlowClientError, BetterFlowAuthError
    from .protocols import AWClientProtocol, BFClientProtocol, OfflineQueueProtocol
    from .activity_analyzer import ActivityAnalyzer, EngagementThresholds
    from .daily_time_tracker import DailyTimeTracker
except ImportError:
    from config import Config, PrivacySettings
    from sync.aw_client import AWClientError, AWEvent, BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT, BUCKET_TYPE_AFK, BUCKET_TYPE_AFK_ALT, BUCKET_TYPE_WEB, BUCKET_TYPE_INPUT
    from sync.bf_client import BetterFlowClientError, BetterFlowAuthError
    from sync.protocols import AWClientProtocol, BFClientProtocol, OfflineQueueProtocol
    from sync.activity_analyzer import ActivityAnalyzer, EngagementThresholds
    from sync.daily_time_tracker import DailyTimeTracker

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
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


MAX_APP_LENGTH = 256
MAX_TITLE_LENGTH = 1024
MAX_URL_LENGTH = 2048


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

        # Dedup: track (bucket_id, event_id) pairs already sent this session.
        # The lookback window re-fetches recent events for duration updates -
        # we only re-send if the duration actually changed.
        # Bounded LRU: evicts oldest on every insert once at capacity.
        self._sent_cache: OrderedDict[tuple[str, int], float] = OrderedDict()
        self._SENT_CACHE_MAX = 5_000
        # Track original AW durations for gap-filled events to prevent re-send
        self._gap_filled_originals: dict[int, float] = {}

        # Thread safety: protects cross-thread mutable state
        self._state_lock = threading.Lock()
        # Dedicated lock for _sent_cache (OrderedDict LRU ops are multi-step)
        self._cache_lock = threading.Lock()

        # Time tracking dedup: track last-counted duration per event to avoid
        # double-counting when the lookback window re-fetches events with grown
        # durations.  Only the delta (new - old) is added to the time tracker.
        self._time_cache: OrderedDict[tuple[str, int], float] = OrderedDict()
        self._TIME_CACHE_MAX = 5_000

        # App category cache — avoids SQLite lookups on every event.
        # Populated lazily; invalidated when categories are refreshed.
        self._category_cache: Optional[dict[str, str]] = None
        self._category_cache_lock = threading.Lock()

        # Activity analysis for fraud detection (DIP)
        self._activity_analyzer = activity_analyzer or ActivityAnalyzer(
            thresholds=self._create_engagement_thresholds(),
            fraud_config=self.config.fraud_detection,
        )
        self._time_tracker = time_tracker or DailyTimeTracker()
        self._has_input_data = False  # Set to True when input buckets exist
        self._current_afk_events: list[AWEvent] = []  # AFK events for current sync cycle

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
            except BetterFlowClientError:
                pass

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
                self.bf.end_session("private_time")
            except BetterFlowClientError:
                pass

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

    def _get_category(self, app_name: str) -> Optional[str]:
        """Look up category for an app, using thread-safe in-memory cache (M3)."""
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
            logger.info("Server configuration applied")

            # Update activity analyzer thresholds from new config
            self._activity_analyzer.update_thresholds(
                self._create_engagement_thresholds()
            )
            self._activity_analyzer.update_fraud_config(self.config.fraud_detection)

            if self._on_config_updated:
                self._on_config_updated()
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

        # Fetch input events for activity analysis before processing window events
        # Use 2x engagement window to ensure coverage for rolling window calculations
        input_lookback_minutes = self.config.engagement.window_minutes * 2
        input_events_for_analysis: list[AWEvent] = []
        for bucket in input_buckets:
            try:
                events = self.aw.get_events_since(
                    bucket.id,
                    datetime.now(timezone.utc) - timedelta(minutes=input_lookback_minutes),
                    limit=1000,
                )
                input_events_for_analysis.extend(events)
            except AWClientError:
                pass
        self._activity_analyzer.add_input_events(input_events_for_analysis)
        self._has_input_data = len(input_events_for_analysis) > 0

        # Sync window buckets with gap-filling
        all_events = []
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

                    filled = self._fill_window_gaps(raw_events, afk_events)
                    stats.gaps_filled += filled

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

        # Send events, then commit checkpoints only on success (N5)
        if all_events:
            self._send_events(all_events, stats)
        # Commit checkpoints after send (or if nothing to send, still advance)
        if stats.events_sent > 0 or not all_events:
            for bucket_id, ts, event_id in pending_checkpoints:
                self.queue.set_checkpoint(bucket_id, ts, event_id)

        # Process offline queue if we're online
        if self.bf.is_reachable() and not self.queue.is_empty():
            self._process_queue(stats)

        # Periodic heartbeat
        with self._state_lock:
            self._heartbeat_count += 1
            should_heartbeat = self._heartbeat_count >= self._heartbeat_interval
            if should_heartbeat:
                self._heartbeat_count = 0
        if should_heartbeat:
            self._send_heartbeat()

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
            # BetterFlow was running.
            checkpoint = datetime.now(timezone.utc)
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
            # Check if this event was gap-filled on a prior cycle —
            # AW returns original duration but we already sent the extended one
            orig = self._gap_filled_originals.get(event.id)
            if orig is not None and abs(event.duration - orig) < 0.5:
                stats.events_filtered += 1
                continue

            transformed_event = self._transform_event(event, bucket_id, bucket_type)
            if transformed_event:
                transformed.append(transformed_event)
                # LRU insert: add/move to end, evict oldest if over capacity
                with self._cache_lock:
                    self._sent_cache[cache_key] = event.duration
                    self._sent_cache.move_to_end(cache_key)
                    if len(self._sent_cache) > self._SENT_CACHE_MAX:
                        self._sent_cache.popitem(last=False)
            else:
                stats.events_filtered += 1

        pending_checkpoint = None
        if events:
            newest = max(events, key=lambda e: e.timestamp)
            pending_checkpoint = (bucket_id, newest.timestamp, newest.id)

        return transformed, pending_checkpoint

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
        """Fetch AFK events covering [start, end] from all AFK buckets."""
        try:
            afk_buckets = self.aw.get_afk_buckets()
        except AWClientError:
            return []

        all_afk: list[AWEvent] = []
        for bucket in afk_buckets:
            try:
                events = self.aw.get_events(
                    bucket.id, start=start, end=end, limit=5000
                )
                all_afk.extend(events)
            except AWClientError:
                pass

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

    def _fill_window_gaps(
        self,
        window_events: list[AWEvent],
        afk_events: list[AWEvent],
        max_gap_seconds: float = 300.0,
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
            self._gap_filled_originals[current.id] = old_duration
            filled += 1
            logger.info(
                f"Filling {gap_seconds:.1f}s window gap: event {current.id} "
                f"({current.app}) duration {old_duration:.1f}s -> {new_duration:.1f}s"
            )

        return filled

    def _transform_event(
        self, event: AWEvent, bucket_id: str, bucket_type: str
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

        # Skip very short events (< 0.5 second — those that round to 0)
        if event.duration < 0.5:
            return None

        # Build data object
        data = {}

        if bucket_type in (BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT, BUCKET_TYPE_WEB):
            data["app"] = app[:MAX_APP_LENGTH] if app else app
            title = event.title
            data["title"] = title[:MAX_TITLE_LENGTH] if title else title
            if event.url:
                url = event.url
                if privacy.collect_full_urls:
                    data["url"] = url[:MAX_URL_LENGTH]
                elif privacy.domain_only_urls:
                    domain = self._extract_domain(url)
                    if domain:
                        data["url"] = domain[:MAX_URL_LENGTH]
                else:
                    data["url"] = url[:MAX_URL_LENGTH]

                if privacy.collect_page_category:
                    data["page_category"] = self._infer_page_category(event.url, event.title)

            if app and privacy.auto_categorize:
                category = self._get_category(app)
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
            # Send AFK periods as "break" bucket_type for chart display
            if event.status == "afk":
                bucket_type = "break"
        elif bucket_type == BUCKET_TYPE_INPUT:
            # Input events track keystrokes, clicks, scrolls for fraud detection
            data["presses"] = event.presses
            data["clicks"] = event.clicks
            data["scrolls"] = event.scrolls

        # Clamp future timestamps and reject negative durations
        now = datetime.now(timezone.utc)
        timestamp = event.timestamp
        if timestamp > now + timedelta(minutes=1):
            logger.warning(f"Clamping future timestamp {timestamp} to now")
            timestamp = now
        duration = max(0, round(event.duration, 2))

        result = {
            "id": event.id,
            "timestamp": timestamp.isoformat(),
            "duration": duration,
            "bucket_id": bucket_id,
            "bucket_type": bucket_type,
            "data": data,
        }

        # Tag with current project if set
        with self._state_lock:
            project = self._current_project
        if project:
            result["project_id"] = project["id"]

        # Add activity classification for window events (fraud detection)
        if bucket_type in (BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT, BUCKET_TYPE_WEB):
            if self._has_input_data:
                activity_state = self._activity_analyzer.get_activity_state(event.timestamp)
                activity_metrics = self._activity_analyzer.get_raw_metrics(event.timestamp)

                result["activity_state"] = activity_state
                result["activity_metrics"] = activity_metrics.to_dict()

                # Add fraud assessment from session-level signals
                fraud = self._activity_analyzer.get_fraud_assessment(event.timestamp, app=app)
                result["fraud_score"] = fraud.score
                result["fraud_signals"] = fraud.signals
                result["activity_metrics"].update(fraud.extra_metrics)
            else:
                # No input watcher — use AFK data to determine activity
                event_end = event.timestamp + timedelta(seconds=event.duration)
                if self._current_afk_events and not self._is_active_during(
                    event.timestamp, event_end, self._current_afk_events
                ):
                    activity_state = "inactive"
                else:
                    activity_state = "active"
                result["activity_state"] = activity_state

            # Track active time (only "active" events count).
            # Use delta to avoid double-counting re-fetched events whose
            # duration grew via heartbeat extension or gap-filling.
            if activity_state == "active":
                event_date = event.timestamp.astimezone().date()
                time_key = (bucket_id, event.id)
                with self._cache_lock:
                    prev_counted = self._time_cache.get(time_key, 0.0)
                    delta = event.duration - prev_counted
                    if delta > 0:
                        self._time_tracker.add_active_time(delta, event_date)
                        logger.debug(f"Time tracked: +{delta:.1f}s for event {event.id} (total today: {self._time_tracker.get_today_active_time()})")
                        self._time_cache[time_key] = event.duration
                        self._time_cache.move_to_end(time_key)
                        if len(self._time_cache) > self._TIME_CACHE_MAX:
                            self._time_cache.popitem(last=False)

        return result

    @staticmethod
    def _extract_domain(url: str) -> Optional[str]:
        """Extract domain from URL safely."""
        try:
            parsed = urlparse(url)
            return parsed.netloc or None
        except Exception:
            return None

    @staticmethod
    def _infer_page_category(url: Optional[str], title: Optional[str]) -> str:
        """Infer a coarse page category from URL/title."""
        haystack = f"{url or ''} {title or ''}".lower()
        patterns = {
            "code": ["github", "gitlab", "bitbucket", "repo", "pull request", "merge request"],
            "review": ["review", "diff", "changes"],
            "documentation": ["docs", "confluence", "notion", "wiki"],
            "communication": ["mail", "inbox", "slack", "teams", "chat", "meet"],
            "planning": ["jira", "asana", "trello", "linear", "backlog", "sprint"],
            "design": ["figma", "miro", "canva", "adobe"],
        }
        for category, keywords in patterns.items():
            if any(keyword in haystack for keyword in keywords):
                return category
        return "other"

    def send_break_event(self, start: datetime, end: Optional[datetime] = None) -> None:
        """Send a break_time event covering the break duration."""
        if end is None:
            end = datetime.now(timezone.utc)
        duration = (end - start).total_seconds()
        if duration < 1:
            return
        event = {
            "timestamp": start.isoformat(),
            "duration": round(duration, 2),
            "bucket_type": "break_time",
            "data": {"status": "break"},
        }
        with self._state_lock:
            project = self._current_project
        if project:
            event["project_id"] = project["id"]
        try:
            self.bf.send_events([event])
            logger.info(f"Sent break_time event ({duration:.0f}s)")
        except BetterFlowClientError as e:
            logger.warning(f"Failed to send break_time event: {e}")
            self.queue.enqueue([event])

    def _send_private_time_event(self, start: Optional[datetime] = None) -> None:
        """Send a private_time event covering the private mode duration."""
        if start is None:
            with self._state_lock:
                start = self._private_start
        if not start:
            return
        now = datetime.now(timezone.utc)
        duration = (now - start).total_seconds()
        if duration < 1:
            return
        event = {
            "timestamp": start.isoformat(),
            "duration": round(duration, 2),
            "bucket_type": "private_time",
            "data": {"status": "private"},
        }
        with self._state_lock:
            project = self._current_project
        if project:
            event["project_id"] = project["id"]
        try:
            self.bf.send_events([event])
            logger.info(f"Sent private_time event ({duration:.0f}s)")
        except BetterFlowClientError as e:
            logger.warning(f"Failed to send private_time event: {e}")
            self.queue.enqueue([event])

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
                        stats.events_sent += len(result.accepted_ids)
                        if failed:
                            self.queue.enqueue(failed)
                            stats.events_queued += len(failed)
                    else:
                        # N11: server returned non-success without accepted_ids
                        logger.warning(
                            "Server returned partial failure without accepted_ids - "
                            "re-queuing entire batch"
                        )
                        self.queue.enqueue(batch)
                        stats.events_queued += len(batch)
                    if result.error:
                        stats.errors.append(result.error)
            except BetterFlowAuthError as e:
                # Queue remaining unsent batches before re-raising.
                # Start from i+1: the current batch (i) was already sent to
                # the server and may have been processed before the 401.
                for remaining in batches[i + 1:]:
                    self.queue.enqueue(remaining)
                    stats.events_queued += len(remaining)
                stats.errors.append(f"Authentication error: {e}")
                raise
            except BetterFlowClientError:
                # Network error — queue this and all remaining batches, then stop
                for remaining in batches[i:]:
                    self.queue.enqueue(remaining)
                    stats.events_queued += len(remaining)
                break

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
        import time as _time
        deadline = _time.monotonic() + self._QUEUE_PROCESS_TIMEOUT
        batch_size = self.config.sync.batch_size
        processed = 0
        max_per_cycle = batch_size * 10  # Max 10 batches per cycle

        while processed < max_per_cycle and _time.monotonic() < deadline:
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
                    # Partial success: remove accepted, increment retry on rest
                    accepted_set = set(result.accepted_ids)
                    succeeded_ids = [eid for eid, ev in zip(event_ids, events)
                                     if ev.get("id") in accepted_set]
                    failed_ids = [eid for eid in event_ids if eid not in succeeded_ids]
                    if succeeded_ids:
                        self.queue.remove(succeeded_ids)
                        stats.events_sent += len(succeeded_ids)
                    if failed_ids:
                        self.queue.increment_retry(failed_ids)
                    self._queue_consecutive_failures = 0
                    processed += len(succeeded_ids)
                else:
                    self.queue.increment_retry(event_ids)
                    self._apply_queue_backoff()
                    break
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

    def _send_heartbeat(self) -> None:
        """Send heartbeat to server and process commands."""
        try:
            response = self.bf.heartbeat()

            # Handle server commands
            commands = response.get("commands", [])
            for cmd in commands:
                cmd_type = cmd.get("type")
                if cmd_type == "pause":
                    logger.info(f"Server requested pause: {cmd.get('reason')}")
                    self._advance_checkpoints_to_now("server_pause")
                    with self._state_lock:
                        self._paused = True
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
                    server_time = datetime.fromisoformat(server_time_str)
                    local_time = datetime.now(timezone.utc)
                    self._server_time_offset = (server_time - local_time).total_seconds()
                    if abs(self._server_time_offset) > 300:
                        logger.warning(
                            f"Clock skew detected: server time differs by "
                            f"{self._server_time_offset:.0f}s — timestamps may be inaccurate"
                        )
                except (ValueError, TypeError):
                    pass

            # Re-fetch config if server says it changed
            if response.get("config_updated"):
                self.fetch_server_config()

        except BetterFlowClientError as e:
            logger.debug(f"Heartbeat failed: {e}")

    @staticmethod
    def _version_below(current: str, minimum: str) -> bool:
        """Compare semver-style version strings.

        Returns True (conservative) on parse failure so the user sees
        the update warning rather than silently skipping it.
        """
        try:
            cur = tuple(int(x) for x in current.split(".")[:3])
            min_ = tuple(int(x) for x in minimum.split(".")[:3])
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
        """Shutdown the sync engine gracefully."""
        with self._state_lock:
            need_end = self._session_active
            self._session_active = False
        if need_end:
            for attempt in range(2):
                try:
                    self.bf.end_session("app_quit")
                    break
                except BetterFlowClientError:
                    if attempt == 0:
                        logger.debug("Session end attempt 1 failed, retrying")

        # Close time tracker
        self._time_tracker.close()
