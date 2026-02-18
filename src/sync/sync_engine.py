"""Sync engine - orchestrates data flow from ActivityWatch to BetterFlow."""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from ..config import Config, PrivacySettings
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
        """Initialize sync engine.

        Args:
            aw: ActivityWatch client
            bf: BetterFlow client
            queue: Offline queue for failed syncs
            config: Application configuration
        """
        self.aw = aw
        self.bf = bf
        self.queue = queue
        self.config = config
        self._paused = False
        self._session_active = False

    def pause(self) -> None:
        """Pause syncing."""
        self._paused = True
        if self._session_active:
            try:
                self.bf.end_session("user_paused")
                self._session_active = False
            except BetterFlowClientError:
                pass

    def resume(self) -> None:
        """Resume syncing."""
        self._paused = False

    @property
    def is_paused(self) -> bool:
        return self._paused

    def sync(self) -> SyncStats:
        """Perform a sync cycle.

        1. Check if ActivityWatch is running
        2. Get events since last checkpoint
        3. Transform and filter events
        4. Send to BetterFlow (or queue if offline)
        5. Update checkpoints

        Returns:
            SyncStats with results
        """
        stats = SyncStats()

        if self._paused:
            return stats

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

        return stats

    def _sync_bucket(
        self, bucket_id: str, bucket_type: str, stats: SyncStats
    ) -> list[dict]:
        """Sync events from a single bucket.

        Args:
            bucket_id: ActivityWatch bucket ID
            bucket_type: Bucket type (window, afk, etc.)
            stats: Stats object to update

        Returns:
            List of transformed events ready for BetterFlow
        """
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

        # Transform and filter
        transformed = []
        for event in events:
            transformed_event = self._transform_event(event, bucket_type)
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
        self, event: AWEvent, bucket_type: str
    ) -> Optional[dict]:
        """Transform an ActivityWatch event to BetterFlow format.

        Applies privacy filtering and normalization.

        Args:
            event: ActivityWatch event
            bucket_type: Type of bucket (window, afk, etc.)

        Returns:
            Transformed event dict, or None if filtered out
        """
        privacy = self.config.privacy

        # Skip excluded apps
        app = event.app
        if app and app in privacy.exclude_apps:
            return None

        # Skip very short events (< 1 second)
        if event.duration < 1:
            return None

        # Build data object
        data = {}

        if bucket_type == BUCKET_TYPE_WINDOW:
            data["app"] = app
            data["title"] = self._process_title(app, event.title, privacy)
            if event.url:
                data["url"] = self._process_url(event.url, privacy)
        elif bucket_type == BUCKET_TYPE_AFK:
            data["status"] = event.status

        return {
            "timestamp": event.timestamp.isoformat(),
            "duration": round(event.duration, 2),
            "data": data,
        }

    def _process_title(
        self, app: Optional[str], title: Optional[str], privacy: PrivacySettings
    ) -> Optional[str]:
        """Process window title according to privacy settings.

        Args:
            app: Application name
            title: Window title
            privacy: Privacy settings

        Returns:
            Processed title (hashed, original, or None)
        """
        if not title:
            return None

        # Check if app is in allowlist for raw titles
        if app and app in privacy.title_allowlist:
            return title

        # Hash title if configured
        if privacy.hash_titles:
            return self._hash_string(title)

        return title

    def _process_url(self, url: Optional[str], privacy: PrivacySettings) -> Optional[str]:
        """Process URL according to privacy settings.

        Args:
            url: Full URL
            privacy: Privacy settings

        Returns:
            Processed URL (domain only or full)
        """
        if not url:
            return None

        if privacy.domain_only_urls:
            try:
                parsed = urlparse(url)
                return parsed.netloc
            except Exception:
                return None

        return url

    def _hash_string(self, value: str) -> str:
        """Hash a string with SHA-256.

        Returns first 16 characters of hex digest for readability.
        """
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    def _send_events(self, events: list[dict], stats: SyncStats) -> None:
        """Send events to BetterFlow or queue if offline.

        Args:
            events: Events to send
            stats: Stats object to update
        """
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
        """Process offline queue.

        Args:
            stats: Stats object to update
        """
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

    def get_status(self) -> dict:
        """Get current sync status.

        Returns:
            Dict with status information
        """
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
                self.bf.end_session("shutdown")
            except BetterFlowClientError:
                pass
            self._session_active = False
