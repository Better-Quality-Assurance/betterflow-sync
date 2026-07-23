"""Hardware serial probe + its trip to the wire.

The serial is the join key between the agent fleet and the MDM asset
inventory (docs/superpowers/specs/2026-07-22-hardware-serial-reporting-design.md).

``None`` is a first-class value here — a VM, a container, a locked-down Linux
box and a failed probe all legitimately have no serial — so these tests pin
that ``None`` travels all the way to the request body rather than being
silently dropped at the heartbeat whitelist (the #152 defect).
"""

import json
import subprocess
import sys

import pytest
import responses

from src import hardware_serial as hw
from src.sync.bf_client import BetterFlowClient


@pytest.fixture(autouse=True)
def _clear_serial_cache():
    hw.reset_cache_for_tests()
    yield
    hw.reset_cache_for_tests()


# ── The probe ───────────────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform != "darwin", reason="macOS IOKit probe")
def test_macos_probe_matches_ioreg():
    """Control: the IOKit read must equal what ioreg reports.

    Without this cross-check the probe could return a plausible-looking string
    from the wrong registry key and nothing would notice.
    """
    serial = hw.get_hardware_serial()
    assert serial, "expected a real serial on a physical Mac"

    out = subprocess.run(
        ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
        capture_output=True, text=True, timeout=15,
    ).stdout
    line = next(
        row for row in out.splitlines() if "IOPlatformSerialNumber" in row
    )
    expected = line.split('=', 1)[1].strip().strip('"')

    assert serial == expected


def test_failed_probe_yields_none_and_is_cached_not_retried(monkeypatch):
    """A raising probe degrades to None, caches it, and does NOT re-probe."""
    calls = []

    def boom():
        calls.append(1)
        raise OSError("no such registry entry")

    monkeypatch.setattr(hw, "_probe_serial", boom)

    assert hw.get_hardware_serial() is None
    assert hw.get_hardware_serial() is None
    assert hw.get_hardware_serial() is None
    assert len(calls) == 1, f"probe re-ran {len(calls)}× — cache is not holding None"


def test_successful_probe_is_cached(monkeypatch):
    calls = []

    def probe():
        calls.append(1)
        return "C02Z60U3LVCJ"

    monkeypatch.setattr(hw, "_probe_serial", probe)

    assert hw.get_hardware_serial() == "C02Z60U3LVCJ"
    assert hw.get_hardware_serial() == "C02Z60U3LVCJ"
    assert len(calls) == 1


def test_blank_and_placeholder_serials_normalise_to_none(monkeypatch):
    """VMs and unconfigured DMI report junk; that is a None, not a serial."""
    for raw in ("", "   ", "To Be Filled By O.E.M.", "Default string", "None", "0"):
        hw.reset_cache_for_tests()
        monkeypatch.setattr(hw, "_probe_serial", lambda raw=raw: raw)
        assert hw.get_hardware_serial() is None, f"{raw!r} should normalise to None"


# ── The wire ────────────────────────────────────────────────────────────

def _make_client():
    return BetterFlowClient(
        api_url="https://betterflow.eu/api/agent",
        token="test-token",
        device_id="test-device",
    )


def test_hardware_serial_is_in_the_heartbeat_whitelist():
    """Membership, not truthiness — a key absent here is dropped at the wire."""
    assert "hardware_serial" in BetterFlowClient.HEARTBEAT_HEALTH_KEYS


@responses.activate
def test_serial_reaches_the_request_body():
    responses.add(
        responses.POST,
        "https://betterflow.eu/api/agent/heartbeat",
        json={"status": "active"}, status=200,
    )
    client = _make_client()
    try:
        client.heartbeat(health={"hardware_serial": "C02Z60U3LVCJ"})
    finally:
        client.close()

    body = json.loads(responses.calls[-1].request.body)
    assert body["hardware_serial"] == "C02Z60U3LVCJ"


def test_health_telemetry_carries_the_serial(monkeypatch):
    """The builder must actually put the field on the heartbeat dict."""
    import threading
    import types

    from src.main import SyncCoordinator

    monkeypatch.setattr(hw, "_probe_serial", lambda: "C02Z60U3LVCJ")

    stub = types.SimpleNamespace(
        _idle_tracker_warn_lock=threading.Lock(),
        _blind_tracker_window=0,
        _consecutive_sync_failures=0,
        _last_successful_sync=None,
        aw_manager=types.SimpleNamespace(health_snapshot=lambda: {}),
    )
    telemetry = SyncCoordinator._build_health_telemetry(stub)
    assert telemetry["hardware_serial"] == "C02Z60U3LVCJ"


@responses.activate
def test_none_serial_survives_the_heartbeat_envelope():
    """The #152 trap: a falsy value must still be forwarded.

    A ``None`` serial is meaningful (this device has no readable serial); if the
    whitelist filtered on truthiness the server could never tell "not reported"
    from "reported as absent".
    """
    responses.add(
        responses.POST,
        "https://betterflow.eu/api/agent/heartbeat",
        json={"status": "active"}, status=200,
    )
    client = _make_client()
    try:
        client.heartbeat(health={"hardware_serial": None})
    finally:
        client.close()

    body = json.loads(responses.calls[-1].request.body)
    assert "hardware_serial" in body, "None serial was dropped at the wire boundary"
    assert body["hardware_serial"] is None
