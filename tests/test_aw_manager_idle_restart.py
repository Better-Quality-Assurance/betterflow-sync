"""Watchdog restarts a HUNG idle/AFK tracker (Alexandru, 2026-06-12).

bf-idle-tracker can stay alive (process running) while emitting no new AFK
events — e.g. it hangs. The window + input watchers keep running, so the user
goes on working, but "Active time" (computed from the AFK not-afk signal)
freezes. The existing stale-check only covered bf-window-tracker; these tests
pin that a hung idle tracker is detected (AFK stale while window fresh) and
restarted, and that we DON'T churn-restart it during legitimate idle/sleep.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from urllib.error import HTTPError

from src.aw_manager import STALE_THRESHOLD, AWManager


def _make_manager(afk_age, window_age, idle_alive=True):
    """An AWManager with mocked watchers and event-age probes."""
    mgr = AWManager()
    mgr._using_external = False

    server = MagicMock()
    server.poll.return_value = None  # alive
    window = MagicMock()
    window.poll.return_value = None  # alive
    idle = MagicMock()
    idle.poll.return_value = None if idle_alive else 1
    mgr._processes = {
        "bf-data-service": server,
        "bf-window-tracker": window,
        "bf-idle-tracker": idle,
    }

    mgr._get_binaries_dir = MagicMock(return_value="/tmp/bin")
    mgr._start_component = MagicMock()
    mgr.check_health = MagicMock(return_value=True)
    mgr._get_latest_afk_event_age = MagicMock(return_value=afk_age)
    mgr._get_latest_window_event_age = MagicMock(return_value=window_age)
    return mgr, idle


def _restarted(mgr, idle_proc):
    return (
        idle_proc.terminate.called
        and (
            ("bf-idle-tracker",) in [c.args[:1] for c in mgr._start_component.call_args_list]
        )
    )


def test_restarts_idle_tracker_when_afk_stale_but_window_fresh():
    # Alexandru's exact signature: AFK silent for 30 min, window fresh.
    mgr, idle = _make_manager(afk_age=1800, window_age=5)

    mgr.restart_if_needed()

    assert _restarted(mgr, idle), "a hung idle tracker (AFK stale, user active) must be restarted"


def test_does_not_restart_when_both_watchers_are_stale():
    # Legitimate full-system idle / sleep: both paused → don't churn-restart.
    mgr, idle = _make_manager(afk_age=1800, window_age=1800)

    mgr.restart_if_needed()

    assert not idle.terminate.called, "no restart when the user is genuinely idle"


def test_does_not_restart_when_afk_is_fresh():
    mgr, idle = _make_manager(afk_age=5, window_age=5)

    mgr.restart_if_needed()

    assert not idle.terminate.called, "healthy AFK watcher is left alone"


def test_does_not_restart_disabled_idle_tracker():
    mgr, idle = _make_manager(afk_age=1800, window_age=5)
    mgr._disabled_components.add("bf-idle-tracker")

    mgr.restart_if_needed()

    assert not idle.terminate.called, "a disabled component is never restarted"


def test_boundary_just_over_threshold_restarts():
    mgr, idle = _make_manager(afk_age=STALE_THRESHOLD + 1, window_age=STALE_THRESHOLD)

    mgr.restart_if_needed()

    assert _restarted(mgr, idle), "AFK age just over threshold while window at/under it → restart"


def test_afk_age_prefers_discovered_betterflow_bucket_over_stale_legacy(monkeypatch):
    """The branded bucket id can be aw-watcher-afk_bf-idle-tracker_<host>.

    The watchdog used to probe only bf-idle-tracker_<host>, then fall back to
    aw-watcher-afk_<host>. On machines with both a fresh BetterFlow bucket and a
    stale legacy ActivityWatch bucket, that made the watchdog think AFK was
    stale forever and restart bf-idle-tracker every 30s.
    """
    now = datetime.now(timezone.utc)
    fresh_ts = (now - timedelta(seconds=5)).isoformat()
    stale_ts = (now - timedelta(minutes=30)).isoformat()

    buckets = {
        "aw-watcher-afk_host": {
            "name": "aw-watcher-afk",
            "type": "aw-watcher-afk",
            "client": "aw-watcher-afk",
        },
        "aw-watcher-afk_bf-idle-tracker_host": {
            "name": "bf-idle-tracker",
            "type": "afkstatus",
            "client": "bf-idle-tracker",
        },
    }
    events = {
        "aw-watcher-afk_host": [
            {"timestamp": stale_ts, "duration": 0, "data": {"status": "afk"}}
        ],
        "aw-watcher-afk_bf-idle-tracker_host": [
            {"timestamp": fresh_ts, "duration": 0, "data": {"status": "not-afk"}}
        ],
    }

    class _Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def fake_urlopen(req, timeout=3):
        url = req.full_url
        if url.endswith("/api/0/buckets/"):
            return _Response(buckets)
        marker = "/api/0/buckets/"
        if marker in url and url.endswith("/events?limit=1"):
            bucket_id = url.split(marker, 1)[1].split("/events", 1)[0]
            if bucket_id in events:
                return _Response(events[bucket_id])
        raise HTTPError(url, 404, "not found", hdrs=None, fp=None)

    monkeypatch.setattr("src.aw_manager.urllib.request.urlopen", fake_urlopen)

    mgr = AWManager()
    age = mgr._get_latest_afk_event_age()

    assert age is not None
    assert age < STALE_THRESHOLD


def test_blind_idle_tracker_stops_churning_and_flags_for_reprompt():
    """A tracker that stays stale across repeated restarts is blind (missing
    Input Monitoring), not crashed — restarting can't fix it. After the blind
    threshold we stop churning a restart every tick and flag idle_tracker_blind
    so the app re-prompts for permission (Brad, 2026-06-18: ~200 futile restarts)."""
    from src.aw_manager import IDLE_BLIND_RESTART_THRESHOLD

    mgr, idle = _make_manager(afk_age=1800, window_age=5)  # permanently stale
    for _ in range(IDLE_BLIND_RESTART_THRESHOLD + 5):
        idle.poll.return_value = None  # process re-mocked as alive each tick
        mgr.restart_if_needed()

    assert mgr.idle_tracker_blind is True, "stale-across-restarts must flag blind"
    # Stopped churning: restarted at most THRESHOLD times, not once per tick.
    assert mgr._start_component.call_count == IDLE_BLIND_RESTART_THRESHOLD, (
        f"expected backoff after {IDLE_BLIND_RESTART_THRESHOLD} restarts, "
        f"got {mgr._start_component.call_count}"
    )


def test_idle_stale_restart_bumps_idle_only_counter():
    """A real idle-tracker stale restart increments the idle-specific counter
    that feeds idle_tracker_stale_restarts."""
    mgr, idle = _make_manager(afk_age=1800, window_age=5)  # AFK stale, window fresh

    mgr.restart_if_needed()

    assert _restarted(mgr, idle)
    assert mgr._idle_stale_restart_count == 1


def test_window_stale_restart_leaves_idle_counter_untouched():
    """A flapping window tracker must NOT inflate the idle figure — only the
    shared any-tracker counter moves."""
    mgr, idle = _make_manager(afk_age=5, window_age=1800)  # window stale, AFK fresh

    mgr.restart_if_needed()

    assert not idle.terminate.called, "idle tracker is healthy here"
    assert mgr._idle_stale_restart_count == 0, "window churn must not touch idle count"
    assert mgr._stale_restart_count == 1, "shared any-tracker counter still moves"


def test_blind_clears_when_tracker_recovers():
    """If the tracker starts emitting fresh AFK events again (e.g. permission
    granted), the blind flag and the consecutive counter reset so a future
    genuine stall restarts promptly."""
    from src.aw_manager import IDLE_BLIND_RESTART_THRESHOLD

    mgr, idle = _make_manager(afk_age=1800, window_age=5)
    for _ in range(IDLE_BLIND_RESTART_THRESHOLD):
        idle.poll.return_value = None
        mgr.restart_if_needed()
    assert mgr.idle_tracker_blind is True

    # Tracker recovers — fresh AFK events.
    mgr._get_latest_afk_event_age = MagicMock(return_value=5)
    mgr.restart_if_needed()

    assert mgr.idle_tracker_blind is False
    assert mgr._idle_consecutive_stale == 0


def test_inproc_afk_suppresses_idle_tracker_restart():
    mgr, idle = _make_manager(afk_age=1800, window_age=5)  # would normally restart
    mgr.set_inproc_afk_active(True)

    mgr.restart_if_needed()

    assert not idle.terminate.called, "ignored tracker must not be restarted"
    assert mgr._idle_stale_restart_count == 0
    assert mgr.idle_tracker_blind is False
