"""Tests for the shared engagement credit-cap math."""

from datetime import datetime, timedelta, timezone

from src.sync.engagement_credit import capped_credit


def _ts(minutes: float = 0) -> datetime:
    return datetime(2026, 7, 15, 17, 0, 0, tzinfo=timezone.utc) + timedelta(
        minutes=minutes
    )


def test_uncapped_when_within_window():
    got = capped_credit(
        _ts(30), anchor=_ts(0), engagement_start=_ts(5), cap_seconds=3600, now=_ts(30)
    )
    assert got == _ts(30)


def test_caps_past_anchor_plus_cap():
    got = capped_credit(
        _ts(90), anchor=_ts(0), engagement_start=_ts(5), cap_seconds=3600, now=_ts(90)
    )
    assert got == _ts(60)


def test_never_past_now():
    got = capped_credit(
        _ts(90), anchor=_ts(0), engagement_start=_ts(5), cap_seconds=7200, now=_ts(80)
    )
    assert got == _ts(80)


def test_anchor_none_falls_back_to_engagement_start():
    got = capped_credit(
        _ts(90), anchor=None, engagement_start=_ts(0), cap_seconds=3600, now=_ts(90)
    )
    assert got == _ts(60)


def test_no_anchor_at_all_returns_none():
    assert (
        capped_credit(
            _ts(10), anchor=None, engagement_start=None, cap_seconds=3600, now=_ts(10)
        )
        is None
    )


def test_floor_no_phantom_credit_before_engagement_started():
    # Cap expired BEFORE the engagement began: last real input 9:00, a
    # calendar-auto-launched meeting at 14:00 (zero input), cap 4h. The bare
    # cap end (13:00) is an instant with provably no engagement and no input —
    # returning it would inject phantom active time into the billed stream.
    last_input = _ts(-300)  # 12:00 base → 9:00-ish; use minutes for clarity
    meeting_start = _ts(0)
    got = capped_credit(
        _ts(10),  # engaged_until (meeting running)
        anchor=last_input,
        engagement_start=meeting_start,
        cap_seconds=4 * 3600,  # anchor+cap = _ts(-60) < meeting_start
        now=_ts(10),
    )
    assert got is None


def test_floor_boundary_exactly_at_engagement_start_is_allowed():
    got = capped_credit(
        _ts(10),
        anchor=_ts(-60),
        engagement_start=_ts(0),
        cap_seconds=3600,  # anchor+cap == engagement_start exactly
        now=_ts(10),
    )
    assert got == _ts(0)
