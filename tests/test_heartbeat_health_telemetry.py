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

    def restart(self, timeout=2.0, may_start=None):
        self.restarts += 1
        self.may_start = may_start
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
    assert src.may_start is not None and src.may_start() is True, (
        "the reinstall must carry the capture predicate for its re-check"
    )


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
    the heartbeat must never be the path that resurrects the hook. Reachable
    shape: a live, silent hook (a stop that did not take) after the close —
    reported as "off", never reinstalled."""
    b = _AllInputBackend()
    src = _real_source(b)
    src.mark_started(now=0.0)  # long past the grace; never saw an event
    stub = _coordinator_stub(src, capture_allowed=False)
    with patch("src.sync.input_source.time.monotonic", return_value=10_000.0):
        telemetry = _telemetry(stub)

    assert telemetry["input_capture_state"] == "off"
    assert (b.stops, b.starts) == (0, 0)


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


# ── F3: "off" (working-hours policy) is not "unavailable" (OS refusal) ───────
#
# Real InputSource + fake backend, so the state comes from the real
# capture_state() — not from a fake that returns whatever the test says.

from src.sync.input_source import InputSource  # noqa: E402


class _AllInputBackend:
    """Like the Windows LL hooks: sees all input, has a listener thread."""

    observes_all_input = True

    def __init__(self):
        self.ok = True
        self.alive = False
        self.starts = 0
        self.stops = 0

    def available(self):
        return self.ok and self.alive

    def start(self):
        self.starts += 1
        self.alive = True
        return True

    def stop(self):
        self.stops += 1
        self.alive = False

    def join(self, timeout):
        return not self.alive


def _real_source(backend):
    s = InputSource(hostname="host", backend=backend, frontmost_app_getter=None)
    if backend is not None:
        backend.alive = True  # running unless the test stops it
    return s


def test_stopped_backend_outside_hours_reports_off():
    b = _AllInputBackend()
    src = _real_source(b)
    src.stop()  # what _stop_watchers does at the close
    telemetry = _telemetry(_coordinator_stub(src, capture_allowed=False))

    assert telemetry["input_capture_state"] == "off"
    assert b.starts == 0


def test_never_started_outside_hours_reports_off():
    """A device launched in the evening: the hook was never installed. It
    used to report "ok" for a sensor that does not exist."""
    b = _AllInputBackend()
    src = InputSource(hostname="host", backend=b, frontmost_app_getter=None)
    b.alive = False
    b.ok = True
    # available() on the real Windows backend is True before any start (a
    # platform probe); model that.
    b.available = lambda: True
    telemetry = _telemetry(_coordinator_stub(src, capture_allowed=False))

    assert telemetry["input_capture_state"] == "off"
    assert b.starts == 0


def test_refused_inside_hours_reports_unavailable():
    b = _AllInputBackend()
    src = _real_source(b)
    b.ok = False  # the OS refused the hook
    telemetry = _telemetry(_coordinator_stub(src, capture_allowed=True))

    assert telemetry["input_capture_state"] == "unavailable"


def test_off_and_unavailable_are_distinct_values():
    stopped = _real_source(_AllInputBackend())
    stopped.stop()
    refused_backend = _AllInputBackend()
    refused = _real_source(refused_backend)
    refused_backend.ok = False

    off = _telemetry(_coordinator_stub(stopped, capture_allowed=False))
    unavailable = _telemetry(_coordinator_stub(refused, capture_allowed=True))

    assert off["input_capture_state"] != unavailable["input_capture_state"]


# ── F4: the reinstall re-checks the capture window right before start() ─────


def test_reinstall_does_not_start_a_hook_after_the_close_during_the_join():
    """The silent verdict and the reinstall decision happen at 17:59:59; the
    join takes up to 2 s; 18:00:00 strikes inside it. No hook may be
    installed past the close."""
    b = _AllInputBackend()
    src = _real_source(b)
    src.mark_started(now=0.0)
    stub = _coordinator_stub(src)
    allowed = {"now": True}
    stub.config.working_hours.allows.side_effect = lambda when: allowed["now"]

    real_join = b.join

    def join_across_the_close(timeout):
        allowed["now"] = False
        return real_join(timeout)

    b.join = join_across_the_close

    with patch("src.sync.input_source.time.monotonic", return_value=10_000.0):
        telemetry = _telemetry(stub)

    assert telemetry["input_capture_state"] == "silent"
    assert b.stops == 1, "the reinstall did run"
    assert b.starts == 0, "a hook was installed after working hours closed"


# ── F6: the 600 s reinstall rate limit, on the clock ─────────────────────────


def test_second_reinstall_only_after_600_seconds():
    src = _FakeInputSource("silent")
    stub = _coordinator_stub(src)

    for t, expected in ((1_000.0, 1), (1_599.0, 1), (1_600.0, 2)):
        with patch("src.main.time.monotonic", return_value=t):
            _telemetry(stub)
        assert src.restarts == expected, (t, src.restarts)
