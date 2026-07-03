"""SyncCoordinator._heartbeat_floor — the 60s-tick heartbeat floor.

The normal heartbeat rides _do_sync (every 5th sync cycle). It goes dormant in
two states:

- PAUSED (break / lock / manual / private): _do_sync early-returns before its
  heartbeat, so a break longer than the server's ~30-min stale-session cleanup
  gets the session marked "crashed" and tracking doesn't resume on return
  (the #28 keep-alive case).
- IDLE: _do_sync runs on the 300s reduced interval, so its every-5th-cycle
  heartbeat lands only ~every 25 min — and every server->agent command
  (pause / deregister / min-version / logs_requested) rides the heartbeat, so
  those take ~25 min to reach an idle device.

Both reduce to one condition: the last heartbeat (any path) has aged past the
floor. The engine stamps every heartbeat, so seconds_since_last_heartbeat() is
the single source of truth — no separate throttle and no paused/idle special
case here. Active devices heartbeat ~every 150s via the cadence path, keeping
the stamp fresh, so the floor never fires for them in steady state.

Consolidates the former test_liveness_heartbeat.py + test_liveness_heartbeat_
monotonic.py + test_heartbeat_command_floor.py (the monotonic hardening moved
to the engine stamp's home in test_sync_engine.py). A SyncCoordinator with
mocked deps, exercising the one 60s-tick helper directly.
"""

from unittest.mock import MagicMock

from src.main import SyncCoordinator
from src.sync.http_client import BetterFlowAuthError


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
    coord.sync_engine.send_heartbeat_now.return_value = None
    coord.break_mgr = MagicMock()
    coord.break_mgr.is_on_break = False
    return coord


def test_fires_when_stamp_stale():
    """Last heartbeat older than the floor (idle device on the 300s interval)
    -> beat now so pending commands land."""
    coord = _make_coordinator()
    coord.sync_engine.seconds_since_last_heartbeat.return_value = 301.0
    coord._heartbeat_floor()
    coord.sync_engine.send_heartbeat_now.assert_called_once()


def test_fires_when_no_heartbeat_yet():
    """None = no heartbeat this process = maximally stale -> fire. Covers a
    device idle since startup, before the sync loop's first heartbeat."""
    coord = _make_coordinator()
    coord.sync_engine.seconds_since_last_heartbeat.return_value = None
    coord._heartbeat_floor()
    coord.sync_engine.send_heartbeat_now.assert_called_once()


def test_no_fire_when_stamp_fresh():
    """Active device: the cadence path heartbeats ~every 150s, so the stamp
    stays under the floor and the 60s tick adds no extra heartbeat load."""
    coord = _make_coordinator()
    coord.sync_engine.seconds_since_last_heartbeat.return_value = 120.0
    coord._heartbeat_floor()
    coord.sync_engine.send_heartbeat_now.assert_not_called()


def test_self_throttles_via_stamp_reset():
    """One beat per interval: the floor's own beat resets the engine stamp, so
    repeated 60s ticks don't flood. Simulate the real stamp reset by having
    send_heartbeat_now drop the reported age to fresh."""
    coord = _make_coordinator()
    coord.sync_engine.seconds_since_last_heartbeat.return_value = 999.0

    def _reset_stamp(*_a, **_k):
        coord.sync_engine.seconds_since_last_heartbeat.return_value = 0.0
        return None

    coord.sync_engine.send_heartbeat_now.side_effect = _reset_stamp
    coord._heartbeat_floor()
    coord._heartbeat_floor()
    coord._heartbeat_floor()
    coord.sync_engine.send_heartbeat_now.assert_called_once()


def test_paused_device_kept_alive_when_stamp_stale():
    """The #28 keep-alive intent survives: a paused device's cadence heartbeat
    is dormant, so its stamp goes stale and the floor beats to keep the session
    alive — without the floor needing to special-case the paused flags."""
    coord = _make_coordinator()
    coord.sync_engine.is_paused = True
    coord.sync_engine.seconds_since_last_heartbeat.return_value = 999.0
    coord._heartbeat_floor()
    coord.sync_engine.send_heartbeat_now.assert_called_once()


def test_offline_skips():
    """Stale but offline: no server to reach, don't try; resume on reconnect."""
    coord = _make_coordinator()
    coord.sync_engine.seconds_since_last_heartbeat.return_value = 999.0
    coord.paused_by_network = True
    coord._heartbeat_floor()
    coord.sync_engine.send_heartbeat_now.assert_not_called()


def test_not_logged_in_skips():
    """Logged out: nothing to keep alive."""
    coord = _make_coordinator()
    coord.sync_engine.seconds_since_last_heartbeat.return_value = 999.0
    coord.logged_in = False
    coord._heartbeat_floor()
    coord.sync_engine.send_heartbeat_now.assert_not_called()


def test_auth_error_routes_through_handler_and_is_tolerated():
    """A single auth error from the floor beat is routed to _handle_auth_error,
    which TOLERATES one transient failure rather than logging out immediately
    (see test_auth_tolerance for the threshold)."""
    coord = _make_coordinator()
    coord.sync_engine.seconds_since_last_heartbeat.return_value = 999.0
    coord.sync_engine.send_heartbeat_now.return_value = BetterFlowAuthError("401")
    coord._heartbeat_floor()
    coord.sync_engine.send_heartbeat_now.assert_called_once()
    assert coord.logged_in is True  # tolerated, not an immediate logout
    assert coord._consecutive_auth_failures == 1
