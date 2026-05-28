"""Smoke tests for the SRP split of sync_engine.

These tests prove that:
1. ``sync.models`` imports cleanly and its public symbols are accessible.
2. ``sync.transform`` imports cleanly and its functions behave correctly.
3. ``sync.sync_engine`` (and by extension the ``sync`` package) imports cleanly.
4. ``SyncEngine`` can be fully instantiated with mock dependencies — this
   exercises the constructor and confirms nothing was broken by the split.
5. The backward-compat class-level static wrappers on ``SyncEngine`` still
   delegate correctly to the module-level functions in ``sync.transform``.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

# ── 1. models ────────────────────────────────────────────────────────────────

def test_models_import():
    from src.sync.models import SyncStats, BoundedLRU, MAX_APP_LENGTH, MAX_TITLE_LENGTH, MAX_URL_LENGTH
    assert MAX_APP_LENGTH == 256
    assert MAX_TITLE_LENGTH == 1024
    assert MAX_URL_LENGTH == 2048


def test_sync_stats_defaults():
    from src.sync.models import SyncStats
    s = SyncStats()
    assert s.events_fetched == 0
    assert s.success is True
    s.errors.append("boom")
    assert s.success is False


def test_bounded_lru_eviction():
    from src.sync.models import BoundedLRU
    lru = BoundedLRU(maxsize=3)
    for i in range(5):
        lru[i] = i * 10
    # Insert 0,1,2,3,4 with maxsize=3:
    #   after insert 3  →  0 evicted  →  {1,2,3}
    #   after insert 4  →  1 evicted  →  {2,3,4}
    assert len(lru) == 3
    assert 0 not in lru
    assert 1 not in lru
    assert 2 in lru
    assert 3 in lru
    assert 4 in lru


def test_bounded_lru_rejects_nonpositive_maxsize():
    from src.sync.models import BoundedLRU
    with pytest.raises(ValueError):
        BoundedLRU(maxsize=0)


# ── 2. transform ─────────────────────────────────────────────────────────────

def test_transform_import():
    from src.sync import transform
    assert callable(transform.extract_domain)
    assert callable(transform.infer_page_category)
    assert callable(transform.overlap_range)
    assert callable(transform.is_active_during)
    assert callable(transform.status_at)
    assert callable(transform.version_below)


def test_extract_domain_happy_path():
    from src.sync.transform import extract_domain
    assert extract_domain("https://github.com/org/repo") == "github.com"


def test_extract_domain_invalid_returns_none():
    from src.sync.transform import extract_domain
    # urlparse won't raise on garbage — it returns empty netloc
    result = extract_domain("not-a-url")
    assert result is None


def test_infer_page_category_code():
    from src.sync.transform import infer_page_category
    assert infer_page_category("https://github.com/pull/123", "PR review") == "code"


def test_infer_page_category_other():
    from src.sync.transform import infer_page_category
    assert infer_page_category("https://example.com", "Homepage") == "other"


def test_overlap_range_overlap():
    from src.sync.transform import overlap_range
    now = datetime.now(timezone.utc)
    result = overlap_range(now, now + timedelta(minutes=5), now + timedelta(minutes=2), now + timedelta(minutes=7))
    assert result is not None
    start, end = result
    assert start == now + timedelta(minutes=2)
    assert end == now + timedelta(minutes=5)


def test_overlap_range_no_overlap():
    from src.sync.transform import overlap_range
    now = datetime.now(timezone.utc)
    result = overlap_range(now, now + timedelta(minutes=2), now + timedelta(minutes=3), now + timedelta(minutes=5))
    assert result is None


def test_version_below_true():
    from src.sync.transform import version_below
    assert version_below("1.0.0", "2.0.0") is True


def test_version_below_false():
    from src.sync.transform import version_below
    assert version_below("2.1.0", "2.0.0") is False


def test_version_below_conservative_on_bad_input():
    from src.sync.transform import version_below
    # Should return True (conservative) rather than crash
    assert version_below("not-a-version", "1.0.0") is True


# ── 3. sync package re-exports ────────────────────────────────────────────────

def test_sync_package_exports_new_symbols():
    from src.sync import (
        SyncStats, BoundedLRU,
        extract_domain, infer_page_category,
        overlap_range, is_active_during, status_at, version_below,
    )
    assert SyncStats is not None
    assert BoundedLRU is not None


# ── 4. SyncEngine instantiation (full constructor smoke) ─────────────────────

def test_sync_engine_instantiation():
    """SyncEngine must be fully constructible from mock dependencies."""
    from src.sync.sync_engine import SyncEngine
    from src.sync.activity_analyzer import ActivityAnalyzer
    from src.sync.daily_time_tracker import DailyTimeTracker
    from src.config import Config

    aw = Mock()
    bf = Mock()
    queue = Mock()
    config = Config()

    activity_analyzer = Mock(spec=ActivityAnalyzer)
    time_tracker = Mock(spec=DailyTimeTracker)

    engine = SyncEngine(
        aw=aw,
        bf=bf,
        queue=queue,
        config=config,
        activity_analyzer=activity_analyzer,
        time_tracker=time_tracker,
    )

    # Confirm key public attributes exist
    assert engine.aw is aw
    assert engine.bf is bf
    assert engine.queue is queue
    assert engine.config is config
    assert engine.is_paused is False
    assert engine.is_private is False


# ── 5. Backward-compat static wrappers on SyncEngine ─────────────────────────

def test_sync_engine_static_extract_domain_compat():
    from src.sync.sync_engine import SyncEngine
    assert SyncEngine._extract_domain("https://example.com/path") == "example.com"


def test_sync_engine_static_version_below_compat():
    from src.sync.sync_engine import SyncEngine
    assert SyncEngine._version_below("1.0.0", "2.0.0") is True
    assert SyncEngine._version_below("3.0.0", "2.0.0") is False


def test_sync_engine_classmethod_infer_page_category_compat():
    from src.sync.sync_engine import SyncEngine
    assert SyncEngine._infer_page_category("https://github.com", None) == "code"
    assert SyncEngine._infer_page_category("https://example.com", "nothing") == "other"


def test_sync_engine_static_overlap_range_compat():
    from src.sync.sync_engine import SyncEngine
    now = datetime.now(timezone.utc)
    result = SyncEngine._overlap_range(
        now,
        now + timedelta(minutes=5),
        now + timedelta(minutes=2),
        now + timedelta(minutes=7),
    )
    assert result is not None
