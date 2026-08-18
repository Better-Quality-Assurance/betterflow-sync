"""The fleet cannot answer "who is on the Intel build?" (#184).

true_machine_arch() returns the HARDWARE architecture, seeing through Rosetta,
and returns "" when its probe never resolved. That empty string must be sent as
null rather than as an empty string, so the fleet view can distinguish "we asked
and could not tell" from "arm64" without string-matching on emptiness.

Every architecture in here is INJECTED, never read from the runner. The PR gate
runs on ubuntu and only the tag build sees macOS, so a test that needs Apple
Silicon to be meaningful runs in no PR-gating job at all — and a skip reads as
coverage while providing none.
"""

import threading
from unittest.mock import MagicMock, patch

from src.main import SyncCoordinator
from src.sync.bf_client import BetterFlowClient


# ── The wire boundary ───────────────────────────────────────────────────

def test_the_allowlist_carries_machine_arch():
    assert "machine_arch" in BetterFlowClient.HEARTBEAT_HEALTH_KEYS


def test_a_rosetta_translated_mac_reports_arm64_not_x86_64():
    from src.machine_arch import true_machine_arch

    arch = true_machine_arch(system="Darwin", machine="x86_64", translated=True)

    assert arch == "arm64", "the whole point: report the hardware, not the process"


def test_an_undetermined_arch_is_sent_as_null_not_empty_string():
    client = BetterFlowClient.__new__(BetterFlowClient)
    captured = {}
    client._request = lambda method, path, data=None, **kw: (captured.update(data or {}), {"data": {}})[1]
    client._detect_timezone = lambda: "UTC"

    client.heartbeat(agent_version="1.5.125", health={"machine_arch": None})

    assert "machine_arch" in captured, "membership is tested with `in`, so null is a real report"
    assert captured["machine_arch"] is None


def test_a_real_arch_reaches_the_request_body():
    """The null case above passes against a forwarder that sends nothing but
    None, so pin the ordinary reading too."""
    client = BetterFlowClient.__new__(BetterFlowClient)
    captured = {}
    client._request = lambda method, path, data=None, **kw: (captured.update(data or {}), {"data": {}})[1]
    client._detect_timezone = lambda: "Europe/Bucharest"

    client.heartbeat(agent_version="1.5.125", health={"machine_arch": "arm64"})

    assert captured["machine_arch"] == "arm64"


# ── The producer ────────────────────────────────────────────────────────
#
# Witnessed separately from the allowlist. An allowlist entry with no producer
# forwards a field no device ever supplies, and every assertion above would
# still pass. Drives the REAL _build_health_telemetry unbound.

def _telemetry_from_real_app() -> dict:
    stub = MagicMock()
    stub._idle_tracker_warn_lock = threading.Lock()
    stub._blind_tracker_window = 0
    stub._consecutive_sync_failures = 0
    stub._last_successful_sync = None
    stub.aw_manager.health_snapshot.return_value = {}
    return SyncCoordinator._build_health_telemetry(stub)


def test_the_assembler_reports_the_hardware_arch():
    with patch("src.main.true_machine_arch", return_value="arm64"):
        telemetry = _telemetry_from_real_app()

    assert telemetry["machine_arch"] == "arm64"


def test_the_assembler_reports_an_intel_box_as_x86_64():
    """The reading the whole issue exists to enumerate — an Intel build in the
    fleet — must survive the producer, not just the arm64 one."""
    with patch("src.main.true_machine_arch", return_value="x86_64"):
        telemetry = _telemetry_from_real_app()

    assert telemetry["machine_arch"] == "x86_64"


def test_the_assembler_sends_an_unresolved_probe_as_null():
    """true_machine_arch() spells "undetermined" as "", which is falsy AND a
    string. Forwarded verbatim it would be indistinguishable from a real arch
    by any consumer doing a type check, and indistinguishable from "absent" by
    any consumer doing a truthiness check. Null is the only reading that is
    neither.
    """
    with patch("src.main.true_machine_arch", return_value=""):
        telemetry = _telemetry_from_real_app()

    assert "machine_arch" in telemetry, "an undetermined arch is still a report"
    assert telemetry["machine_arch"] is None


def test_a_failing_arch_probe_never_costs_the_heartbeat():
    with patch("src.main.true_machine_arch", side_effect=OSError("sysctl denied")):
        telemetry = _telemetry_from_real_app()

    assert isinstance(telemetry, dict)
    assert "machine_arch" not in telemetry
    assert "consecutive_sync_failures" in telemetry, "the rest of the payload survived"
