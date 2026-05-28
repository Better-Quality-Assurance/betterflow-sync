"""Sync module - handles ActivityWatch reading and BetterFlow uploading."""

from .aw_client import AWClient
from .bf_client import BetterFlowClient
from .sync_engine import SyncEngine
from .queue import OfflineQueue
from .retry import RetryConfig, retry_with_backoff
from .protocols import AWClientProtocol, BFClientProtocol, OfflineQueueProtocol
from .activity_analyzer import ActivityAnalyzer, ActivityMetrics, EngagementThresholds
from .daily_time_tracker import DailyTimeTracker
# Data models extracted from sync_engine (SRP split)
from .models import SyncStats, BoundedLRU, MAX_APP_LENGTH, MAX_TITLE_LENGTH, MAX_URL_LENGTH
# Pure transformation helpers extracted from sync_engine (SRP split)
from .transform import (
    extract_domain,
    infer_page_category,
    PAGE_CATEGORY_RULES,
    overlap_range,
    is_active_during,
    status_at,
    version_below,
)

__all__ = [
    "AWClient",
    "BetterFlowClient",
    "SyncEngine",
    "OfflineQueue",
    "RetryConfig",
    "retry_with_backoff",
    "AWClientProtocol",
    "BFClientProtocol",
    "OfflineQueueProtocol",
    "ActivityAnalyzer",
    "ActivityMetrics",
    "EngagementThresholds",
    "DailyTimeTracker",
    # models
    "SyncStats",
    "BoundedLRU",
    "MAX_APP_LENGTH",
    "MAX_TITLE_LENGTH",
    "MAX_URL_LENGTH",
    # transform
    "extract_domain",
    "infer_page_category",
    "PAGE_CATEGORY_RULES",
    "overlap_range",
    "is_active_during",
    "status_at",
    "version_below",
]
