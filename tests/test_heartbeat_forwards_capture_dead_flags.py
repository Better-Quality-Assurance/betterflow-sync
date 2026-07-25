"""The two flags that mean "this device is capturing NOTHING" must reach the server.

aw_manager computes `tracker_download_failed` and `managed_components_unavailable`
specifically to catch a device that looks alive and records nothing (Laszlo Fabian
Raul, device 50, 2026-07-22/23: `[Errno 86] Bad CPU type in executable`, two
zero-second days, `tracking_degraded` still 0). Both were dropped by
HEARTBEAT_HEALTH_KEYS, so the server never saw them and could not grade the device
degraded.

This asserts the wire payload, not the computation — the computation was always
correct; the egress allowlist was the bug.
"""

from unittest.mock import Mock

from src.sync.bf_client import BetterFlowClient


def _client() -> BetterFlowClient:
    c = BetterFlowClient.__new__(BetterFlowClient)
    c._request = Mock(return_value={})
    c._detect_timezone = Mock(return_value="Europe/Bucharest")
    return c


def _sent_payload(health: dict) -> dict:
    c = _client()
    c.heartbeat(agent_version="1.5.117", health=health)
    return c._request.call_args.kwargs["data"]


def test_tracker_download_failed_reaches_the_server():
    data = _sent_payload({"tracker_download_failed": True})
    assert data.get("tracker_download_failed") is True, (
        "a device whose tracker binaries could not be installed is capturing "
        "nothing; the server cannot know that if the flag is filtered out"
    )


def test_managed_components_unavailable_reaches_the_server():
    data = _sent_payload({"managed_components_unavailable": True})
    assert data.get("managed_components_unavailable") is True


def test_false_is_forwarded_too_not_just_true():
    # Membership, not truthiness: a False must reach the server so it can CLEAR a
    # previously-degraded episode. Dropping False would latch the device degraded.
    data = _sent_payload(
        {"tracker_download_failed": False, "managed_components_unavailable": False}
    )
    assert data["tracker_download_failed"] is False
    assert data["managed_components_unavailable"] is False


def test_unknown_keys_are_still_rejected():
    # The allowlist is a privacy gate as well as a schema gate — widening it must
    # not turn it into a passthrough.
    data = _sent_payload({"window_title": "Bank of America — Accounts"})
    assert "window_title" not in data


def test_window_tracker_blind_reaches_the_server():
    # Third field of the same shape. idle_tracker_blind was forwarded and its
    # window counterpart was not, so the backend could see a blind idle tracker
    # but never a blind window tracker.
    data = _sent_payload({"window_tracker_blind": True})
    assert data.get("window_tracker_blind") is True
