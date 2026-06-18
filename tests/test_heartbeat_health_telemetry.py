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
            "afk_event_age_seconds": 300,
            "window_event_age_seconds": 5,
            "consecutive_sync_failures": 0,
        })
    finally:
        client.close()

    body = _last_heartbeat_body()
    assert body["agent_version"]  # base field still present
    assert body["idle_tracker_stale_restarts"] == 12
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
