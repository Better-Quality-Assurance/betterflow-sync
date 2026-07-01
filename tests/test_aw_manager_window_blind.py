"""Watchdog restarts a BLIND window tracker — one alive but emitting ZERO events.

The window-stale check only restarted when _get_latest_window_event_age() returned
a number greater than the threshold. But that helper returns None when the window
bucket is empty — i.e. a tracker that's alive but has never emitted anything (blind
from launch). So a totally-dead window tracker was never restarted: window data
stayed frozen while AFK/input kept flowing (Sachi, win32, 2026-06-24 — the server
flagged window_ingest_stalled with no window events at all).

Fix: treat "no events, well past a launch grace, while AW is reachable" as stale
too. Guards: the launch grace avoids restarting a just-launched tracker before it
emits; the reachability check avoids mistaking an AW outage (also age None) for a
blind tracker.
"""

import time
from unittest.mock import MagicMock

from src.aw_manager import STALE_THRESHOLD, WINDOW_BLIND_GRACE, AWManager


def _mgr(window_age, running_for, reachable=True):
    mgr = AWManager()
    mgr._using_external = False
    server = MagicMock()
    server.poll.return_value = None
    window = MagicMock()
    window.poll.return_value = None
    idle = MagicMock()
    idle.poll.return_value = None
    mgr._processes = {
        "bf-data-service": server,
        "bf-window-tracker": window,
        "bf-idle-tracker": idle,
    }
    mgr._get_binaries_dir = MagicMock(return_value="/tmp/bin")
    mgr._start_component = MagicMock()
    mgr._get_latest_window_event_age = MagicMock(return_value=window_age)
    mgr._get_latest_afk_event_age = MagicMock(return_value=5)  # afk fresh → idle block no-op
    mgr._port_in_use = MagicMock(return_value=reachable)
    mgr._component_started_at = (
        {} if running_for is None
        else {"bf-window-tracker": time.monotonic() - running_for}
    )
    return mgr, window


def _restarted(mgr, window):
    return window.terminate.called and (
        ("bf-window-tracker",) in [c.args[:1] for c in mgr._start_component.call_args_list]
    )


# --- pure decision logic ---------------------------------------------------- #

def test_is_window_tracker_stale_decision():
    m = AWManager()
    assert m._is_window_tracker_stale(age=5, running_for=600, reachable=True) is False
    assert m._is_window_tracker_stale(age=STALE_THRESHOLD + 1, running_for=600, reachable=True) is True
    # blind: no events, past grace, AW reachable
    assert m._is_window_tracker_stale(age=None, running_for=WINDOW_BLIND_GRACE + 1, reachable=True) is True
    # just launched: no events yet, within grace
    assert m._is_window_tracker_stale(age=None, running_for=10, reachable=True) is False
    # AW unreachable: None may be an outage, not a blind tracker
    assert m._is_window_tracker_stale(age=None, running_for=600, reachable=False) is False
    # unknown launch time → can't claim blind
    assert m._is_window_tracker_stale(age=None, running_for=None, reachable=True) is False


# --- watchdog integration --------------------------------------------------- #

def test_blind_window_tracker_is_restarted():
    mgr, window = _mgr(window_age=None, running_for=WINDOW_BLIND_GRACE + 120, reachable=True)
    mgr.restart_if_needed()
    assert _restarted(mgr, window), "a blind window tracker (zero events past grace) must be restarted"


def test_blind_within_launch_grace_not_restarted():
    mgr, window = _mgr(window_age=None, running_for=10, reachable=True)
    mgr.restart_if_needed()
    assert not window.terminate.called, "a just-launched tracker must get its grace before restart"


def test_no_events_but_aw_unreachable_not_restarted():
    mgr, window = _mgr(window_age=None, running_for=WINDOW_BLIND_GRACE + 120, reachable=False)
    mgr.restart_if_needed()
    assert not window.terminate.called, "age None during an AW outage is not a blind tracker"


def test_frozen_window_tracker_still_restarted():
    # Pre-existing behavior must be preserved: events exist but stale.
    mgr, window = _mgr(window_age=STALE_THRESHOLD + 10, running_for=WINDOW_BLIND_GRACE + 120)
    mgr.restart_if_needed()
    assert _restarted(mgr, window)


def test_fresh_window_tracker_not_restarted():
    mgr, window = _mgr(window_age=5, running_for=WINDOW_BLIND_GRACE + 120)
    mgr.restart_if_needed()
    assert not window.terminate.called


def test_blind_window_tracker_stops_churning_and_flags_blind():
    """A window tracker that stays stale across repeated restarts is blind — a
    wedged/blocked capture source a restart can't fix, not a crash. After the
    threshold we stop kill+relaunching every tick and flag window_tracker_blind
    (Sachi, win32, 2026-06-30: restart #1->#5 every 30s while the event age
    climbed 148->268s, never recovering). Pre-fix this restarted every tick."""
    from src.aw_manager import WINDOW_BLIND_RESTART_THRESHOLD

    # Frozen tracker: an old event whose age stays > threshold every tick.
    mgr, window = _mgr(window_age=STALE_THRESHOLD + 80, running_for=WINDOW_BLIND_GRACE + 120)
    for _ in range(WINDOW_BLIND_RESTART_THRESHOLD + 5):
        window.poll.return_value = None  # re-mocked alive each tick
        mgr.restart_if_needed()

    assert mgr.window_tracker_blind is True, "stale-across-restarts must flag blind"
    assert mgr._start_component.call_count == WINDOW_BLIND_RESTART_THRESHOLD, (
        f"expected backoff after {WINDOW_BLIND_RESTART_THRESHOLD} restarts, got "
        f"{mgr._start_component.call_count} (pre-fix: one per tick)"
    )


def test_window_blind_clears_after_sustained_recovery():
    """The blind flag clears only after the tracker emits fresh events across
    WINDOW_BLIND_CLEAR_HEALTHY_CYCLES consecutive health-checks — sustained
    recovery, not a single stray event (see flapping test below). Once cleared,
    the consecutive counter resets so a future genuine stall restarts promptly."""
    from src.aw_manager import (
        WINDOW_BLIND_CLEAR_HEALTHY_CYCLES,
        WINDOW_BLIND_RESTART_THRESHOLD,
    )

    mgr, window = _mgr(window_age=STALE_THRESHOLD + 80, running_for=WINDOW_BLIND_GRACE + 120)
    for _ in range(WINDOW_BLIND_RESTART_THRESHOLD):
        window.poll.return_value = None
        mgr.restart_if_needed()
    assert mgr.window_tracker_blind is True

    # Recovers: fresh window events, sustained across the required cycles.
    mgr._get_latest_window_event_age = MagicMock(return_value=5)
    for i in range(WINDOW_BLIND_CLEAR_HEALTHY_CYCLES):
        assert mgr.window_tracker_blind is True, (
            f"must stay latched until {WINDOW_BLIND_CLEAR_HEALTHY_CYCLES} healthy "
            f"cycles (cleared early at cycle {i})"
        )
        mgr.restart_if_needed()

    assert mgr.window_tracker_blind is False
    assert mgr._window_consecutive_stale == 0


def test_flapping_window_tracker_stays_backed_off():
    """A win32 capture source that FLAPS — a lone fresh event between blind
    spells — must not re-enter a full restart burst. Clearing blind on the first
    stray event (pre-fix) reset the counter and let a second 5-restart burst run
    ~30s later, defeating the retry-interval backoff (Sachi, win32, 2026-07-01:
    blind at 09:50, a lone event ~09:55 cleared it, then a 4-restart burst at
    09:57). Once blind, a single healthy cycle followed by staleness again must
    stay backed off (no new restart) until sustained recovery clears the flag."""
    from src.aw_manager import WINDOW_BLIND_RESTART_THRESHOLD

    mgr, window = _mgr(window_age=STALE_THRESHOLD + 80, running_for=WINDOW_BLIND_GRACE + 120)
    for _ in range(WINDOW_BLIND_RESTART_THRESHOLD):
        window.poll.return_value = None
        mgr.restart_if_needed()
    assert mgr.window_tracker_blind is True
    restarts_at_blind = mgr._start_component.call_count
    assert restarts_at_blind == WINDOW_BLIND_RESTART_THRESHOLD

    # One stray fresh event (the flap) — NOT sustained recovery.
    mgr._get_latest_window_event_age = MagicMock(return_value=5)
    mgr.restart_if_needed()

    # Source dies again immediately. Because it's still blind and within the
    # retry interval, the watchdog must back off, not launch a fresh burst.
    mgr._get_latest_window_event_age = MagicMock(return_value=STALE_THRESHOLD + 80)
    for _ in range(WINDOW_BLIND_RESTART_THRESHOLD + 2):
        window.poll.return_value = None
        mgr.restart_if_needed()

    assert mgr.window_tracker_blind is True, "a flap must not unlatch the blind flag"
    assert mgr._start_component.call_count == restarts_at_blind, (
        "a flapping source must stay backed off — no new restart burst "
        f"(expected {restarts_at_blind} restarts, got {mgr._start_component.call_count})"
    )
