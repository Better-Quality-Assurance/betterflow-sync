"""Smoke tests for agent-health telemetry riding the heartbeat (option B).

The heartbeat is the channel that lets the backend mark a device
tracking_degraded while it still reports "Active" — the Martin/Sachi
2026-06-17 "idle but bucket has events" case. These pin that the health dict
reaches the wire, that the payload stays backward-compatible without it, and
that only whitelisted keys are forwarded.
"""

import json

import responses

from src.sync.bf_client import BetterFlowClient


def _make_client():
    return BetterFlowClient(
        api_url="https://betterflow.eu/api/agent",
        token="test-token",
        device_id="test-device",
    )


def _last_heartbeat_body():
    # responses records each call; the heartbeat is the only POST here.
    return json.loads(responses.calls[-1].request.body)


@responses.activate
def test_heartbeat_forwards_health_telemetry():
    responses.add(
        responses.POST,
        "https://betterflow.eu/api/agent/heartbeat",
        json={"status": "active", "commands": []},
        status=200,
    )
    client = _make_client()
    try:
        # "idle but bucket has events": AFK silent while window fresh.
        client.heartbeat(health={
            "idle_tracker_stale_restarts": 12,
            "idle_tracker_blind": True,
            "inproc_afk": True,
            "afk_event_age_seconds": 300,
            "window_event_age_seconds": 5,
            "consecutive_sync_failures": 0,
        })
    finally:
        client.close()

    body = _last_heartbeat_body()
    assert body["agent_version"]  # base field still present
    assert body["idle_tracker_stale_restarts"] == 12
    assert body["idle_tracker_blind"] is True
    assert body["inproc_afk"] is True
    assert body["afk_event_age_seconds"] == 300
    assert body["window_event_age_seconds"] == 5
    assert body["consecutive_sync_failures"] == 0


@responses.activate
def test_heartbeat_without_health_is_backward_compatible():
    responses.add(
        responses.POST,
        "https://betterflow.eu/api/agent/heartbeat",
        json={"status": "active"},
        status=200,
    )
    client = _make_client()
    try:
        client.heartbeat()
    finally:
        client.close()

    body = _last_heartbeat_body()
    assert set(body.keys()) == {"agent_version", "timezone"}


@responses.activate
def test_heartbeat_drops_unknown_health_keys():
    responses.add(
        responses.POST,
        "https://betterflow.eu/api/agent/heartbeat",
        json={"status": "active"},
        status=200,
    )
    client = _make_client()
    try:
        client.heartbeat(health={
            "afk_event_age_seconds": 300,
            "evil": "DROP TABLE agent_devices",  # not whitelisted
        })
    finally:
        client.close()

    body = _last_heartbeat_body()
    assert body["afk_event_age_seconds"] == 300
    assert "evil" not in body


# ── input_capture_state: the producer side (_build_health_telemetry) ─────────
#
# Drives the REAL SyncCoordinator._build_health_telemetry unbound against a
# stub, the pattern tests/test_os_idle_heartbeat.py uses (this file had no app
# fixture of its own).

import threading  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

from src.main import SyncCoordinator  # noqa: E402


class _FakeInputSource:
    def __init__(self, state):
        self.state = state
        self.restarts = 0
        self.stops = 0
        self.starts = 0
        self.idle_seen = []

    def capture_state(self, os_idle_seconds, now=None):
        self.idle_seen.append(os_idle_seconds)
        return self.state

    def restart(self, timeout=2.0):
        self.restarts += 1
        return True

    def stop(self):
        self.stops += 1

    def start(self):
        self.starts += 1
        return True


def _coordinator_stub(input_source, *, in_process_input=True, capture_allowed=True):
    stub = MagicMock()
    stub._idle_tracker_warn_lock = threading.Lock()
    stub._blind_tracker_window = 0
    stub._consecutive_sync_failures = 0
    stub._last_successful_sync = None
    stub._last_input_reinstall_mono = float("-inf")
    stub.aw_manager.health_snapshot.return_value = {}
    stub.config.sync.in_process_input = in_process_input
    stub.config.working_hours.allows.return_value = capture_allowed
    stub.sync_engine.input_source = input_source
    return stub


def _telemetry(stub, idle=5):
    with patch("src.main.get_system_idle_seconds", return_value=idle):
        return SyncCoordinator._build_health_telemetry(stub)


def test_silent_input_hook_is_reported_and_reinstalled_once():
    src = _FakeInputSource("silent")
    stub = _coordinator_stub(src)

    telemetry = _telemetry(stub)

    assert telemetry["input_capture_state"] == "silent"
    assert src.idle_seen == [5], "the verdict is judged against the OS idle clock"
    assert src.restarts == 1
    assert (src.stops, src.starts) == (0, 0), "restart(), never a bare stop/start"


def test_silent_reinstall_is_rate_limited_to_once_per_600s():
    src = _FakeInputSource("silent")
    stub = _coordinator_stub(src)

    _telemetry(stub)
    telemetry = _telemetry(stub)

    assert telemetry["input_capture_state"] == "silent", "still reported every heartbeat"
    assert src.restarts == 1


def test_ok_input_hook_is_reported_and_not_touched():
    src = _FakeInputSource("ok")
    telemetry = _telemetry(_coordinator_stub(src))

    assert telemetry["input_capture_state"] == "ok"
    assert src.restarts == 0


def test_input_capture_state_absent_when_in_process_input_off():
    src = _FakeInputSource("silent")
    telemetry = _telemetry(_coordinator_stub(src, in_process_input=False))

    assert "input_capture_state" not in telemetry
    assert src.restarts == 0


def test_input_capture_state_absent_when_no_backend():
    telemetry = _telemetry(_coordinator_stub(_FakeInputSource(None)))

    assert "input_capture_state" not in telemetry


def test_silent_hook_is_not_reinstalled_outside_the_capture_window():
    """A reinstall is a start(): outside working hours nothing may record, so
    the heartbeat must never be the path that resurrects the hook."""
    src = _FakeInputSource("silent")
    telemetry = _telemetry(_coordinator_stub(src, capture_allowed=False))

    assert telemetry["input_capture_state"] == "silent"
    assert src.restarts == 0


def test_a_raising_input_source_never_costs_the_heartbeat():
    src = _FakeInputSource("silent")
    src.capture_state = MagicMock(side_effect=RuntimeError("boom"))
    telemetry = _telemetry(_coordinator_stub(src))

    assert "input_capture_state" not in telemetry
    assert "consecutive_sync_failures" in telemetry


@responses.activate
def test_heartbeat_forwards_input_capture_state():
    responses.add(
        responses.POST,
        "https://betterflow.eu/api/agent/heartbeat",
        json={"status": "active"},
        status=200,
    )
    client = _make_client()
    try:
        client.heartbeat(health={"input_capture_state": "silent"})
    finally:
        client.close()

    assert _last_heartbeat_body()["input_capture_state"] == "silent"
