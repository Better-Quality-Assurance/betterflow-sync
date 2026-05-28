"""Pure transformation helpers for the sync engine.

All functions here are stateless and side-effect-free — they take plain
Python values and return plain Python values.  No shared mutable state,
no network I/O, no DB access.

Contains:
- extract_domain          — safely parse domain from a URL string
- PAGE_CATEGORY_RULES     — compiled regex table for page-category inference
- infer_page_category     — classify a URL/title into a coarse category
- overlap_range           — compute overlap of two time intervals
- is_active_during        — check AFK-event coverage for a time interval
- status_at               — look up AFK status at a point in time
- version_below           — semver string comparison
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# URL helpers


def extract_domain(url: str) -> Optional[str]:
    """Extract domain from URL safely.

    Returns ``None`` on any parse error rather than raising.
    """
    try:
        parsed = urlparse(url)
        return parsed.netloc or None
    except Exception:
        return None


# Page-category classification

# Each category resolves to a precompiled word-boundary regex. Earlier
# entries win: "code" is checked before "review" so repo URLs aren't
# reclassified as reviews. Word boundaries prevent substring leaks —
# "code" won't match "decode"/"encode", "diff" won't match "different".
PAGE_CATEGORY_RULES: tuple[tuple[str, "re.Pattern[str]"], ...] = tuple(
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


def infer_page_category(url: Optional[str], title: Optional[str]) -> str:
    """Infer a coarse page category from URL/title.

    Returns one of the category strings from ``PAGE_CATEGORY_RULES``,
    or ``"other"`` when no rule matches.
    """
    haystack = f"{url or ''} {title or ''}"
    for category, pattern in PAGE_CATEGORY_RULES:
        if pattern.search(haystack):
            return category
    return "other"


# AFK / time-interval helpers

# Import AWEvent here to avoid a circular dependency at module level.
# sync_engine → transform → aw_client (no cycle; aw_client has no local deps)
try:
    from .aw_client import AWEvent
except ImportError:
    from sync.aw_client import AWEvent  # type: ignore[no-redef]


def overlap_range(
    start: datetime,
    end: datetime,
    other_start: datetime,
    other_end: datetime,
) -> Optional[tuple[datetime, datetime]]:
    """Return the overlapping sub-interval of two time ranges, or None."""
    overlap_start = max(start, other_start)
    overlap_end = min(end, other_end)
    if overlap_end <= overlap_start:
        return None
    return overlap_start, overlap_end


def is_active_during(start: datetime, end: datetime, afk_events: list[AWEvent]) -> bool:
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


def status_at(timestamp: datetime, afk_events: list[AWEvent]) -> str | None:
    """Return the AFK status covering ``timestamp``, if any."""
    for ev in afk_events:
        ev_start = ev.timestamp
        ev_end = ev.timestamp + timedelta(seconds=ev.duration)
        if ev_start <= timestamp < ev_end:
            return ev.status
    return None


# Version comparison


def version_below(current: str, minimum: str) -> bool:
    """Compare semver-style version strings.

    Returns True (conservative) on parse failure so the user sees
    the update warning rather than silently skipping it.
    """
    try:
        cur = tuple(int(x.split("-")[0]) for x in current.split(".")[:3])
        min_ = tuple(int(x.split("-")[0]) for x in minimum.split(".")[:3])
        return cur < min_
    except (ValueError, AttributeError):
        logger.warning("Cannot parse version strings: current=%r, minimum=%r", current, minimum)
        return True
