"""The 60s-tick heartbeat floor: reach IDLE devices fast, not just paused ones.

The normal heartbeat rides _do_sync. When the user goes idle the sync loop
drops to the 300s reduced interval (idle_manager._IDLE_SYNC_INTERVAL), and the
heartbeat only fires every 5th cycle — so on an idle device it lands roughly
every 25 minutes. Every server->agent command (pause / deregister /
minimum_agent_version / logs_requested) rides that heartbeat, so on an idle
device those take ~25 min to arrive.

_liveness_heartbeat used to fire only while PAUSED. The floor broadens it: fire
whenever the sync-cadence heartbeat has gone stale (age >= the interval),
which covers both paused AND idle-throttled devices, capping command-delivery
latency at ~5 min regardless of sync cadence. Active devices heartbeat every
~150s, so their last-beat age stays under the floor and this never trips —
no added heartbeat load on them.

Mirrors test_liveness_heartbeat.py: a SyncCoordinator with mocked deps,
exercising the one 60s-tick helper directly (no scheduler / AW / tray loop).
"""

from unittest.mock import MagicMock

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
    # NOT paused — this is the idle-but-not-paused case the floor newly covers.
    coord.sync_engine.is_paused = False
    coord.sync_engine.is_private = False
    coord.sync_engine.send_heartbeat_now.return_value = None
    coord.break_mgr = MagicMock()
    coord.break_mgr.is_on_break = False
    return coord


def test_fires_when_idle_and_sync_heartbeat_stale():
    """Not paused, but the sync-cadence heartbeat is older than the floor
    (idle device on the 300s interval) -> beat now so pending commands land."""
    coord = _make_coordinator()
    coord.sync_engine.seconds_since_last_heartbeat.return_value = 301.0
    coord._liveness_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_called_once()


def test_fires_when_no_heartbeat_yet():
    """None = no heartbeat this process = maximally stale -> fire. Covers a
    device that has been idle since startup, before the sync loop's first
    heartbeat would otherwise register it."""
    coord = _make_coordinator()
    coord.sync_engine.seconds_since_last_heartbeat.return_value = None
    coord._liveness_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_called_once()


def test_no_fire_when_sync_heartbeat_fresh():
    """Active device: the sync loop heartbeats every ~150s, so the stamp stays
    under the floor and the 60s tick adds no extra heartbeat load."""
    coord = _make_coordinator()
    coord.sync_engine.seconds_since_last_heartbeat.return_value = 120.0
    coord._liveness_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_not_called()


def test_stale_but_throttled_within_window_fires_once():
    """While the sync heartbeat stays stale, the local throttle still caps the
    floor to one beat per interval (no flood on repeated 60s ticks)."""
    coord = _make_coordinator()
    coord.sync_engine.seconds_since_last_heartbeat.return_value = 999.0
    coord._liveness_heartbeat()
    coord._liveness_heartbeat()
    coord._liveness_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_called_once()


def test_offline_idle_skips():
    """Idle + stale but offline: no server to reach, don't try."""
    coord = _make_coordinator()
    coord.sync_engine.seconds_since_last_heartbeat.return_value = 999.0
    coord.paused_by_network = True
    coord._liveness_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_not_called()


def test_not_logged_in_idle_skips():
    """Logged out: nothing to keep alive."""
    coord = _make_coordinator()
    coord.sync_engine.seconds_since_last_heartbeat.return_value = 999.0
    coord.logged_in = False
    coord._liveness_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_not_called()


def test_paused_still_fires_regardless_of_stamp():
    """The original paused keep-alive is preserved: a paused device beats even
    if the stamp looks fresh (pause -> _do_sync early-returns -> its heartbeat
    is dormant, so the floor must not depend on the stamp when paused)."""
    coord = _make_coordinator()
    coord.sync_engine.is_paused = True
    coord.sync_engine.seconds_since_last_heartbeat.return_value = 5.0
    coord._liveness_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_called_once()
