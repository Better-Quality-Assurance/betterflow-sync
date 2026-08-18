"""The no_capture alert cannot tell a user who is away from a dead tracker (#195).

The agent has always known: get_system_idle_seconds() reads HIDIdleTime on
macOS and GetLastInputInfo on Windows. It just never left the device, because
HEARTBEAT_HEALTH_KEYS is a hard allowlist and the key was not in it.

Both halves are asserted here on purpose. A producer-only test passes while the
allowlist silently drops the field, which is the exact failure the tuple's own
comment warns about. The mirror also holds: an allowlist entry with no producer
is a field no device will ever send, so the assembler is driven for real below.
"""

import threading
from unittest.mock import MagicMock, patch

from src.main import SyncCoordinator
from src.sync.bf_client import BetterFlowClient


# ── The wire boundary ───────────────────────────────────────────────────

def test_the_allowlist_carries_os_idle_seconds():
    assert "os_idle_seconds" in BetterFlowClient.HEARTBEAT_HEALTH_KEYS


def test_a_supplied_os_idle_reading_reaches_the_request_body():
    client = BetterFlowClient.__new__(BetterFlowClient)
    captured = {}

    def _fake_request(method, path, data=None, **kw):
        captured.update(data or {})
        return {"data": {}}

    client._request = _fake_request
    client._detect_timezone = lambda: "Europe/Bucharest"

    client.heartbeat(agent_version="1.5.125", health={"os_idle_seconds": 12})

    assert captured["os_idle_seconds"] == 12


def test_an_unreadable_idle_clock_is_omitted_not_zeroed():
    """None must not become 0. Zero means 'the user is at the keyboard right
    now', which is the opposite of 'we could not tell' — and it is the reading
    the alert would act on."""
    client = BetterFlowClient.__new__(BetterFlowClient)
    captured = {}
    client._request = lambda method, path, data=None, **kw: (captured.update(data or {}), {"data": {}})[1]
    client._detect_timezone = lambda: "UTC"

    client.heartbeat(agent_version="1.5.125", health={})

    assert "os_idle_seconds" not in captured


# ── The producer ────────────────────────────────────────────────────────
#
# Witnessed separately from the allowlist on purpose. An allowlist entry with
# no producer forwards a field nothing ever supplies, and every assertion above
# would still pass. The stub drives the REAL _build_health_telemetry unbound,
# so a payload builder that nothing calls cannot satisfy these (Phantom 3).

def _telemetry_from_real_app() -> dict:
    stub = MagicMock()
    stub._idle_tracker_warn_lock = threading.Lock()
    stub._blind_tracker_window = 0
    stub._consecutive_sync_failures = 0
    stub._last_successful_sync = None
    stub.aw_manager.health_snapshot.return_value = {}
    return SyncCoordinator._build_health_telemetry(stub)


def test_the_assembler_reports_the_os_idle_clock():
    with patch("src.main.get_system_idle_seconds", return_value=903.7):
        telemetry = _telemetry_from_real_app()

    assert telemetry["os_idle_seconds"] == 903, "seconds, truncated to an int"


def test_the_assembler_omits_an_unreadable_idle_clock():
    """Linux, or any platform whose probe returned None: no key at all.

    The adversarial value is None rather than a large number — a producer that
    coerced it would report 0, i.e. "at the keyboard this instant", which is the
    strongest possible claim of presence built out of an unknown.
    """
    with patch("src.main.get_system_idle_seconds", return_value=None):
        telemetry = _telemetry_from_real_app()

    assert "os_idle_seconds" not in telemetry


def test_a_failing_idle_probe_never_costs_the_heartbeat():
    with patch("src.main.get_system_idle_seconds", side_effect=OSError("ioreg gone")):
        telemetry = _telemetry_from_real_app()

    assert isinstance(telemetry, dict)
    assert "os_idle_seconds" not in telemetry
    assert "consecutive_sync_failures" in telemetry, "the rest of the payload survived"
