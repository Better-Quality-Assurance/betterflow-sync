"""Sync engine - orchestrates data flow from ActivityWatch to BetterFlow."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import Config
from .aw_client import AWClient, AWClientError, AWEvent, BUCKET_TYPE_WINDOW, BUCKET_TYPE_AFK
from .bf_client import BetterFlowClient, BetterFlowClientError, BetterFlowAuthError, SyncResult
from .queue import OfflineQueue

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
    ):
        self.aw = aw
        self.bf = bf
        self.queue = queue
        self.config = config
        self._paused = False
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

    def fetch_server_config(self) -> None:
        """Fetch and apply server-side configuration."""
        try:
            server_config = self.bf.get_config()
            self.config.update_from_server(server_config)
            self._config_fetched = True
            logger.info("Server configuration applied")
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

        if self._paused:
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
            afk_buckets = self.aw.get_afk_buckets()
        except AWClientError as e:
            stats.errors.append(f"Failed to get buckets: {e}")
            return stats

        # Sync each bucket
        all_events = []
        for bucket in window_buckets + afk_buckets:
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
        """Sync events from a single bucket."""
        # Get checkpoint
        checkpoint = self.queue.get_checkpoint(bucket_id)
        if checkpoint is None:
            # First sync - start from 24 hours ago
            checkpoint = datetime.now(timezone.utc) - timedelta(hours=24)

        # Get new events
        events = self.aw.get_events_since(
            bucket_id, checkpoint, limit=self.config.sync.batch_size
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

        # Skip very short events (< 1 second)
        if event.duration < 1:
            return None

        # Pass through all raw AW event data — server handles privacy
        data = dict(event.data)

        return {
            "id": event.id,
            "timestamp": event.timestamp.isoformat(),
            "duration": round(event.duration, 2),
            "bucket_id": bucket_id,
            "data": data,
        }

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
