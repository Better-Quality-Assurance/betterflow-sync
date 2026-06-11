"""Targeted test for the monotonic-after-sleep edge case the audit flagged.

The concern: `time.monotonic()` does not always advance through laptop
sleep on macOS. If `_last_liveness_heartbeat` was set before sleep, and
the lid is closed for an arbitrary period, the throttle comparison
`now - last < 300` could behave in two failure modes:

1. monotonic FREEZES during sleep → after wake, `now ≈ last` regardless
   of wall-clock time elapsed → next heartbeat blocked for 5 more min of
   wake-time, which is the documented design. This is actually GOOD: the
   throttle protects against burst from rapid lid-flap events.

2. monotonic ADVANCES during sleep → after wake, `now >> last` → next
   heartbeat fires immediately. Also fine for our purposes: we want it
   to fire after a long pause.

The audit's specific worry was: "lid open/close 3x within 5 min →
3 heartbeats instead of 1." Test that explicitly.
"""

import time
from unittest.mock import MagicMock, patch

from src.main import SyncCoordinator


def _make_coordinator() -> SyncCoordinator:
    tray = MagicMock()
    tray.model = MagicMock()
    coord = SyncCoordinator(
        config=MagicMock(),
        aw=MagicMock(),
        bf=MagicMock(),
        queue=MagicMock(),
        sync_engine=MagicMock(),
        tray=tray,
        aw_manager=MagicMock(),
    )
    coord.logged_in = True
    coord.paused_by_network = False
    coord.sync_engine.is_paused = True  # on break
    coord.sync_engine.is_private = False
    coord.sync_engine.send_heartbeat_now.return_value = None
    coord.break_mgr = MagicMock()
    coord.break_mgr.is_on_break = False
    return coord


def test_lid_flap_within_throttle_window_only_fires_once():
    """The audit's scenario: 3 lid-flaps within 5 min.

    We model 3 ticks, each at monotonic times 1s, 2s, 3s (well inside the
    300s throttle). Expected: 1 heartbeat (first tick), 2 throttled.
    """
    coord = _make_coordinator()

    base = 1000.0
    with patch("src.main.time.monotonic") as mock_mono:
        mock_mono.side_effect = [base, base + 1, base + 2, base + 3]

        # First tick after launch — no prior heartbeat, fires.
        coord._liveness_heartbeat()
        # Three more ticks shortly after — all within the throttle window.
        coord._liveness_heartbeat()
        coord._liveness_heartbeat()
        coord._liveness_heartbeat()

    assert coord.sync_engine.send_heartbeat_now.call_count == 1, (
        f"Expected exactly 1 heartbeat in a 3s window; got "
        f"{coord.sync_engine.send_heartbeat_now.call_count}. "
        "If this is 3, the throttle is bypassed and the audit was right."
    )


def test_throttle_releases_after_interval_passes():
    """Sanity-check the throttle releases: after 300s+ elapsed, a new
    heartbeat fires. Otherwise we'd block forever."""
    coord = _make_coordinator()

    base = 1000.0
    with patch("src.main.time.monotonic") as mock_mono:
        # First tick at t=base, second at base+301 (just past interval).
        mock_mono.side_effect = [base, base + 301]

        coord._liveness_heartbeat()
        coord._liveness_heartbeat()

    assert coord.sync_engine.send_heartbeat_now.call_count == 2


def test_monotonic_does_not_regress_across_calls():
    """If `time.monotonic()` ever produced a smaller value than the last
    stored heartbeat time (e.g. some pathological clock bug), the
    `now - last < 300` comparison would yield a NEGATIVE number which IS
    less than 300 — so the throttle still holds. Verify."""
    coord = _make_coordinator()

    base = 1000.0
    with patch("src.main.time.monotonic") as mock_mono:
        # First tick records monotonic=1000. Second tick reports monotonic=500
        # (which shouldn't happen in real life but let's prove the math).
        mock_mono.side_effect = [base, base - 500]

        coord._liveness_heartbeat()
        coord._liveness_heartbeat()

    # The throttle: (500 - 1000) = -500, which IS < 300, so the second
    # heartbeat IS suppressed. Defensive but documented.
    assert coord.sync_engine.send_heartbeat_now.call_count == 1, (
        "A backwards-jumping monotonic clock must not trigger a flood of "
        "heartbeats. The negative-delta math holds the throttle."
    )
