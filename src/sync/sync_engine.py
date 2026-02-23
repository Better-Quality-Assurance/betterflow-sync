"""Sync engine - orchestrates data flow from ActivityWatch to BetterFlow."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

try:
    from ..config import Config, PrivacySettings
    from .aw_client import AWClient, AWClientError, AWEvent, BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT, BUCKET_TYPE_AFK, BUCKET_TYPE_AFK_ALT, BUCKET_TYPE_WEB, BUCKET_TYPE_INPUT
    from .bf_client import BetterFlowClient, BetterFlowClientError, BetterFlowAuthError, SyncResult
    from .queue import OfflineQueue
except ImportError:
    from config import Config, PrivacySettings
    from sync.aw_client import AWClient, AWClientError, AWEvent, BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT, BUCKET_TYPE_AFK, BUCKET_TYPE_AFK_ALT, BUCKET_TYPE_WEB, BUCKET_TYPE_INPUT
    from sync.bf_client import BetterFlowClient, BetterFlowClientError, BetterFlowAuthError, SyncResult
    from sync.queue import OfflineQueue

logger = logging.getLogger(__name__)


@dataclass
class SyncStats:
    """Statistics from a sync cycle."""

    events_fetched: int = 0
    events_filtered: int = 0
    events_sent: int = 0
    events_queued: int = 0
    buckets_synced: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


class SyncEngine:
    """Core sync engine that orchestrates AW -> BetterFlow data flow."""

    def __init__(
        self,
        aw: AWClient,
        bf: BetterFlowClient,
        queue: OfflineQueue,
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
        self._current_project: Optional[dict] = None
        self._session_active = False
        self._config_fetched = False
        self._heartbeat_count = 0
        # Send heartbeat every 5 sync cycles (5 * 60s = 5 min default)
        self._heartbeat_interval = 5

    def pause(self) -> None:
        """Pause syncing."""
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

        # Start session if needed
        if not self._session_active:
            try:
                if self.bf.is_reachable():
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

        # Sync each bucket (including input for fraud detection)
        all_events = []
        for bucket in window_buckets + web_buckets + afk_buckets + input_buckets:
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
        # Get checkpoint
        checkpoint = self.queue.get_checkpoint(bucket_id)
        if checkpoint is None:
            # First sync - start from 24 hours ago
            checkpoint = datetime.now(timezone.utc) - timedelta(hours=24)
            lookback_start = checkpoint
        else:
            # Look back 2 minutes before checkpoint to catch events whose
            # duration grew since we last synced them.
            lookback_start = checkpoint - timedelta(minutes=2)

        # Get events (including the overlap window)
        events = self.aw.get_events_since(
            bucket_id, lookback_start, limit=self.config.sync.batch_size
        )
        stats.events_fetched += len(events)

        if not events:
            return []

        # Transform events — include bucket_id and pass raw data
        transformed = []
        for event in events:
            transformed_event = self._transform_event(event, bucket_id, bucket_type)
            if transformed_event:
                transformed.append(transformed_event)
            else:
                stats.events_filtered += 1

        # Update checkpoint to newest event
        if events:
            newest = max(events, key=lambda e: e.timestamp)
            self.queue.set_checkpoint(bucket_id, newest.timestamp, newest.id)

        return transformed

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
            data["app"] = app
            data["title"] = event.title
            if event.url:
                if privacy.collect_full_urls:
                    data["url"] = event.url
                elif privacy.domain_only_urls:
                    domain = self._extract_domain(event.url)
                    if domain:
                        data["url"] = domain
                else:
                    data["url"] = event.url

                if privacy.collect_page_category:
                    data["page_category"] = self._infer_page_category(event.url, event.title)
        elif bucket_type in (BUCKET_TYPE_AFK, BUCKET_TYPE_AFK_ALT):
            data["status"] = event.status
        elif bucket_type == BUCKET_TYPE_INPUT:
            # Input events track keystrokes, clicks, scrolls for fraud detection
            data["presses"] = event.presses
            data["clicks"] = event.clicks
            data["scrolls"] = event.scrolls

        result = {
            "id": event.id,
            "timestamp": event.timestamp.isoformat(),
            "duration": round(event.duration, 2),
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

    def _send_events(self, events: list[dict], stats: SyncStats) -> None:
        """Send events to BetterFlow or queue if offline."""
        # Batch events
        batch_size = self.config.sync.batch_size
        batches = [events[i : i + batch_size] for i in range(0, len(events), batch_size)]

        for batch in batches:
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
                # Auth error - don't queue, need to re-authenticate
                stats.errors.append(f"Authentication error: {e}")
                raise
            except BetterFlowClientError:
                # Network error - queue for later
                self.queue.enqueue(batch)
                stats.events_queued += len(batch)

    def _process_queue(self, stats: SyncStats) -> None:
        """Process offline queue."""
        # Remove events that exceeded retry limit
        self.queue.remove_failed(max_retries=5)

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
                else:
                    # Increment retry count
                    self.queue.increment_retry(event_ids)
                    break
            except BetterFlowClientError:
                # Still offline
                self.queue.increment_retry(event_ids)
                break

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
                    self._paused = True
                elif cmd_type == "deregister":
                    logger.warning(f"Device revoked: {cmd.get('reason')}")
                    self._paused = True

            # Re-fetch config if server says it changed
            if response.get("config_updated"):
                self.fetch_server_config()

        except BetterFlowClientError as e:
            logger.debug(f"Heartbeat failed: {e}")

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

    def shutdown(self) -> None:
        """Shutdown the sync engine gracefully."""
        if self._session_active:
            try:
                self.bf.end_session("app_quit")
            except BetterFlowClientError:
                pass
            self._session_active = False
