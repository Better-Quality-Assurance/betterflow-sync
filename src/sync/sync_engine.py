"""Sync engine - orchestrates data flow from ActivityWatch to BetterFlow."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
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
except ImportError:
    from config import Config, PrivacySettings
    from sync.aw_client import AWClientError, AWEvent, BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT, BUCKET_TYPE_AFK, BUCKET_TYPE_AFK_ALT, BUCKET_TYPE_WEB, BUCKET_TYPE_INPUT
    from sync.bf_client import BetterFlowClientError, BetterFlowAuthError
    from sync.protocols import AWClientProtocol, BFClientProtocol, OfflineQueueProtocol

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
        on_config_updated: Optional[callable] = None,
    ):
        self.aw = aw
        self.bf = bf
        self.queue = queue
        self.config = config
        self._on_config_updated = on_config_updated
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

        # Dedup: track (bucket_id, event_id) pairs already sent this session.
        # The lookback window re-fetches recent events for duration updates —
        # we only re-send if the duration actually changed.
        self._sent_cache: dict[tuple[str, int], float] = {}
        self._SENT_CACHE_MAX = 10_000

    def pause(self) -> None:
        """Pause syncing and drop buffered events until resume."""
        if not self._paused:
            self._advance_checkpoints_to_now("pause")
        self._paused = True
        if self._session_active:
            try:
                self.bf.end_session("app_quit")
                self._session_active = False
            except BetterFlowClientError:
                pass

    def resume(self) -> None:
        """Resume syncing."""
        self._paused = False

    @property
    def is_paused(self) -> bool:
        return self._paused

    def set_private_mode(self, enabled: bool) -> None:
        """Enable/disable private time (no events recorded)."""
        if enabled and not self._private_mode:
            self._advance_checkpoints_to_now("private_time")
            self._private_start = datetime.now(timezone.utc)
        elif not enabled and self._private_mode and self._private_start:
            self._send_private_time_event()
            self._private_start = None
        self._private_mode = enabled
        if enabled and self._session_active:
            try:
                self.bf.end_session("private_time")
                self._session_active = False
            except BetterFlowClientError:
                pass

    @property
    def is_private(self) -> bool:
        return self._private_mode

    def set_current_project(self, project: Optional[dict]) -> None:
        """Set the current project for event tagging."""
        self._current_project = project

    def fetch_server_config(self) -> None:
        """Fetch and apply server-side configuration."""
        try:
            server_config = self.bf.get_config()
            self.config.update_from_server(server_config)
            self._config_fetched = True
            logger.info("Server configuration applied")
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
        if not self._session_active:
            try:
                self.bf.start_session()
                self._session_active = True
            except BetterFlowClientError as e:
                logger.warning(f"Failed to start session: {e}")

        # Get buckets to sync
        try:
            window_buckets = self.aw.get_window_buckets()
            web_buckets = self.aw.get_web_buckets()
            afk_buckets = self.aw.get_afk_buckets()
            input_buckets = self.aw.get_input_buckets()
        except AWClientError as e:
            stats.errors.append(f"Failed to get buckets: {e}")
            return stats

        # Sync window buckets with gap-filling
        all_events = []
        for bucket in window_buckets:
            try:
                raw_events, _ = self._fetch_bucket_events(bucket.id, stats)
                if raw_events:
                    # Fetch AFK data covering the same time range
                    earliest = raw_events[0].timestamp
                    latest_ev = raw_events[-1]
                    latest_end = latest_ev.timestamp + timedelta(seconds=latest_ev.duration)
                    afk_events = self._get_afk_events_for_range(earliest, latest_end)

                    filled = self._fill_window_gaps(raw_events, afk_events)
                    stats.gaps_filled += filled

                    transformed = self._transform_and_checkpoint(
                        raw_events, bucket.id, bucket.type, stats
                    )
                    all_events.extend(transformed)
                stats.buckets_synced += 1
            except AWClientError as e:
                stats.errors.append(f"Failed to sync bucket {bucket.id}: {e}")

        # Sync non-window buckets normally
        for bucket in web_buckets + afk_buckets + input_buckets:
            try:
                events = self._sync_bucket(bucket.id, bucket.type, stats)
                all_events.extend(events)
                stats.buckets_synced += 1
            except AWClientError as e:
                stats.errors.append(f"Failed to sync bucket {bucket.id}: {e}")

        # Send events
        if all_events:
            self._send_events(all_events, stats)

        # Process offline queue if we're online
        if self.bf.is_reachable() and not self.queue.is_empty():
            self._process_queue(stats)

        # Periodic heartbeat
        self._heartbeat_count += 1
        if self._heartbeat_count >= self._heartbeat_interval:
            self._send_heartbeat()
            self._heartbeat_count = 0

        return stats

    def _fetch_bucket_events(
        self, bucket_id: str, stats: SyncStats
    ) -> tuple[list[AWEvent], datetime]:
        """Fetch events from a bucket with lookback window.

        Returns (events, lookback_start) — events sorted oldest-first.
        """
        checkpoint = self.queue.get_checkpoint(bucket_id)
        if checkpoint is None:
            checkpoint = datetime.now(timezone.utc) - timedelta(hours=24)
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
    ) -> list[dict]:
        """Transform events to BetterFlow format and update checkpoint.

        Skips events already sent with unchanged duration (dedup).
        Re-sends if duration has grown (heartbeat extension).
        """
        transformed = []
        for event in events:
            # Dedup: skip if already sent with same duration
            cache_key = (bucket_id, event.id)
            prev_duration = self._sent_cache.get(cache_key)
            if prev_duration is not None and abs(event.duration - prev_duration) < 0.5:
                stats.events_filtered += 1
                continue

            transformed_event = self._transform_event(event, bucket_id, bucket_type)
            if transformed_event:
                transformed.append(transformed_event)
                self._sent_cache[cache_key] = event.duration
            else:
                stats.events_filtered += 1

        # Evict oldest entries if cache grows too large
        if len(self._sent_cache) > self._SENT_CACHE_MAX:
            excess = len(self._sent_cache) - self._SENT_CACHE_MAX
            for key in list(self._sent_cache)[:excess]:
                del self._sent_cache[key]

        if events:
            newest = max(events, key=lambda e: e.timestamp)
            self.queue.set_checkpoint(bucket_id, newest.timestamp, newest.id)

        return transformed

    def _sync_bucket(
        self, bucket_id: str, bucket_type: str, stats: SyncStats
    ) -> list[dict]:
        """Sync events from a single bucket.

        ActivityWatch extends the duration of the current (most recent) event
        via heartbeats.  If we only fetch events *after* the checkpoint we miss
        that growing duration.  To fix this we look back a short overlap window
        before the checkpoint so recently-synced events whose duration has
        grown are re-sent with the updated value.  The backend uses the AW
        event id to upsert, so the duration is simply patched in place.
        """
        events, _ = self._fetch_bucket_events(bucket_id, stats)
        if not events:
            return []
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

        Mutates ``window_events`` in-place (sorted oldest-first).
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
            current.duration = (next_ev.timestamp - current.timestamp).total_seconds()
            filled += 1
            logger.info(
                f"Filling {gap_seconds:.1f}s window gap: event {current.id} "
                f"({current.app}) duration {old_duration:.1f}s -> {current.duration:.1f}s"
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
        if self._current_project:
            result["project_id"] = self._current_project["id"]

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

    def _send_private_time_event(self) -> None:
        """Send a private_time event covering the private mode duration."""
        if not self._private_start:
            return
        now = datetime.now(timezone.utc)
        duration = (now - self._private_start).total_seconds()
        if duration < 1:
            return
        event = {
            "timestamp": self._private_start.isoformat(),
            "duration": round(duration, 2),
            "bucket_type": "private_time",
            "data": {"status": "private"},
        }
        if self._current_project:
            event["project_id"] = self._current_project["id"]
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
                    # Queue failed events
                    self.queue.enqueue(batch)
                    stats.events_queued += len(batch)
                    if result.error:
                        stats.errors.append(result.error)
            except BetterFlowAuthError as e:
                # Queue remaining unsent batches before re-raising
                for remaining in batches[i:]:
                    self.queue.enqueue(remaining)
                    stats.events_queued += len(remaining)
                stats.errors.append(f"Authentication error: {e}")
                raise
            except BetterFlowClientError:
                # Network error - queue for later
                self.queue.enqueue(batch)
                stats.events_queued += len(batch)

    def _process_queue(self, stats: SyncStats) -> None:
        """Process offline queue with exponential backoff."""
        # Remove events that exceeded retry limit
        self.queue.remove_failed(max_retries=5)

        # Skip queue processing if in backoff period
        now = datetime.now(timezone.utc)
        if now < self._queue_backoff_until:
            return

        # Process queue in batches
        batch_size = self.config.sync.batch_size
        processed = 0
        max_per_cycle = batch_size * 10  # Max 10 batches per cycle

        while processed < max_per_cycle:
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
                    self._paused = True
                elif cmd_type == "deregister":
                    logger.warning(f"Device revoked: {cmd.get('reason')}")
                    self._advance_checkpoints_to_now("server_deregister")
                    self._paused = True

            # Version compatibility check
            min_version = response.get("minimum_agent_version")
            if min_version and self._version_below(AGENT_VERSION, min_version):
                logger.warning(
                    f"Agent {AGENT_VERSION} is below minimum {min_version} — update required"
                )

            # Re-fetch config if server says it changed
            if response.get("config_updated"):
                self.fetch_server_config()

        except BetterFlowClientError as e:
            logger.debug(f"Heartbeat failed: {e}")

    @staticmethod
    def _version_below(current: str, minimum: str) -> bool:
        """Compare semver-style version strings."""
        try:
            cur = tuple(int(x) for x in current.split(".")[:3])
            min_ = tuple(int(x) for x in minimum.split(".")[:3])
            return cur < min_
        except (ValueError, AttributeError):
            return False

    def get_status(self) -> dict:
        """Get current sync status."""
        aw_running = self.aw.is_running()
        bf_reachable = self.bf.is_reachable() if not self._paused else False
        queue_size = self.queue.size()
        checkpoints = self.queue.get_all_checkpoints()

        return {
            "paused": self._paused,
            "session_active": self._session_active,
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
            except (AWClientError, TypeError, AttributeError):
                pass

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

    def shutdown(self) -> None:
        """Shutdown the sync engine gracefully."""
        if self._session_active:
            try:
                self.bf.end_session("app_quit")
            except BetterFlowClientError:
                pass
            self._session_active = False
