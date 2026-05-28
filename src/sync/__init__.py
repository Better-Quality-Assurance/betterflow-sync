"""Sync module - handles ActivityWatch reading and BetterFlow uploading."""

from .activity_analyzer import ActivityAnalyzer, ActivityMetrics, EngagementThresholds
from .aw_client import AWClient
from .bf_client import BetterFlowClient
from .daily_time_tracker import DailyTimeTracker

# Data models extracted from sync_engine (SRP split)
from .models import MAX_APP_LENGTH, MAX_TITLE_LENGTH, MAX_URL_LENGTH, BoundedLRU, SyncStats
from .protocols import AWClientProtocol, BFClientProtocol, OfflineQueueProtocol
from .queue import OfflineQueue
from .retry import RetryConfig, retry_with_backoff
from .sync_engine import SyncEngine

# Pure transformation helpers extracted from sync_engine (SRP split)
from .transform import (
    PAGE_CATEGORY_RULES,
    extract_domain,
    infer_page_category,
    is_active_during,
    overlap_range,
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
