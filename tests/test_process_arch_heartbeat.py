"""#184: "who is sitting on the Intel BUILD?" is unanswerable from the wire.

``machine_arch`` resolves *through* Rosetta on purpose — a translated x86_64
process reports ``arm64``, because the question that field answers is "what
silicon is this?". The consequence is that a native Apple Silicon install and an
Intel build running under Rosetta are **byte-identical on the heartbeat**.

Measured on real Apple Silicon (Darwin 25.5.0), running this repo's own helpers
under both personalities:

    native                  process=arm64    translated=False  true_machine_arch=arm64
    arch -x86_64 (Rosetta)  process=x86_64   translated=True   true_machine_arch=arm64
                                    ^^^^^^                                     ^^^^^
                            differs                                    IDENTICAL

``true_machine_arch()`` is the same in both rows, so no amount of reading it
answers this issue's title. ``platform.machine()`` — the architecture of the
running PROCESS, i.e. of the installed build — is the only value that differs,
which is what makes it the discriminator and why it ships as a second field
rather than as a change to the first.

The pair is the answer, not either half:

    machine_arch=arm64  process_arch=arm64   native Apple Silicon build   fine
    machine_arch=arm64  process_arch=x86_64  Intel build under Rosetta    THIS
    machine_arch=x86_64 process_arch=x86_64  a genuine Intel Mac          fine

Every architecture in here is INJECTED, never read from the runner — the PR gate
runs on ubuntu and only the tag build sees macOS, so a test that needs Apple
Silicon to be meaningful runs in no PR-gating job at all, and a skip reads as
coverage while providing none. Same rule as ``test_arch_heartbeat.py``.
"""

import threading
from unittest.mock import MagicMock, patch

from src.disclosure_baseline import HEARTBEAT_HEALTH_KEYS as BASELINE_KEYS
from src.machine_arch import process_arch, true_machine_arch
from src.main import SyncCoordinator
from src.sync.bf_client import BetterFlowClient

# ── The resolver ────────────────────────────────────────────────────────


def test_process_arch_reports_the_process_not_the_hardware():
    """THE defect this field exists to close.

    Under Rosetta the two answers must DIVERGE. An implementation that quietly
    delegates to ``true_machine_arch()`` — the obvious wrong fix, since it is
    the arch helper already in the file — passes every other test here and
    leaves the fleet exactly as blind as it is today.
    """
    machine = true_machine_arch(system="Darwin", machine="x86_64", translated=True)
    process = process_arch(system="Darwin", machine="x86_64", translated=True)

    assert machine == "arm64", "unchanged: machine_arch answers 'what silicon?'"
    assert process == "x86_64", "process_arch answers 'which build?'"
    assert process != machine, (
        "if these agree under translation the pair carries no information and "
        "#184's question stays unanswerable"
    )


def test_the_pair_agrees_on_a_native_apple_silicon_mac():
    """Control. Divergence must mean something, so it must not be constant."""
    assert process_arch(system="Darwin", machine="arm64", translated=False) == "arm64"
    assert true_machine_arch(system="Darwin", machine="arm64", translated=False) == "arm64"


def test_the_pair_agrees_on_a_genuine_intel_mac():
    """Control, and the one that matters most: a real Intel Mac must NOT be
    reported as being on the wrong build. Confusing it with a translated
    process would send its owner to an arm64 DMG that cannot run at all —
    there is no reverse Rosetta."""
    assert process_arch(system="Darwin", machine="x86_64", translated=False) == "x86_64"
    assert true_machine_arch(system="Darwin", machine="x86_64", translated=False) == "x86_64"


def test_process_arch_is_untouched_off_darwin():
    """Rosetta is macOS-only. Windows-on-ARM emulation is a different mechanism
    that ``machine_arch`` deliberately does not model, so an x64 build under it
    genuinely IS executing x86-64 and reports so."""
    assert process_arch(system="Windows", machine="AMD64") == "AMD64"
    assert process_arch(system="Linux", machine="aarch64") == "aarch64"


def test_process_arch_never_consults_rosetta(monkeypatch):
    """It is ``platform.machine()`` by definition — the process's own ISA, which
    needs no probe. Forking sysctl to answer it would be both wasteful and a
    route to the empty-string 'undetermined' state, which this field cannot
    have: a running process always has an architecture."""
    import subprocess

    def explode(*a, **k):
        raise AssertionError("process_arch forked a subprocess")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)

    assert process_arch(system="Darwin", machine="arm64") == "arm64"


# ── The wire boundary ───────────────────────────────────────────────────


def test_the_allowlist_carries_process_arch():
    assert "process_arch" in BetterFlowClient.HEARTBEAT_HEALTH_KEYS


def test_the_disclosure_baseline_declares_it_too():
    """``tests/test_disclosure_baseline.py`` already fails on any divergence
    between the two tuples. Asserted here as well so this file states the whole
    contract it is adding rather than depending on a sibling to notice half of
    it."""
    assert "process_arch" in BASELINE_KEYS


def test_a_real_process_arch_reaches_the_request_body():
    client = BetterFlowClient.__new__(BetterFlowClient)
    captured = {}
    client._request = lambda method, path, data=None, **kw: (
        captured.update(data or {}), {"data": {}}
    )[1]
    client._detect_timezone = lambda: "Europe/Bucharest"

    client.heartbeat(
        agent_version="1.5.125",
        health={"machine_arch": "arm64", "process_arch": "x86_64"},
    )

    assert captured["process_arch"] == "x86_64"
    assert captured["machine_arch"] == "arm64", "the existing field is unharmed"


# ── The producer ────────────────────────────────────────────────────────
#
# Witnessed separately from the allowlist: an allowlist entry with no producer
# forwards a field no device ever supplies, and every assertion above still
# passes.


def _telemetry_from_real_app() -> dict:
    stub = MagicMock()
    stub._idle_tracker_warn_lock = threading.Lock()
    stub._blind_tracker_window = 0
    stub._consecutive_sync_failures = 0
    stub._last_successful_sync = None
    stub.aw_manager.health_snapshot.return_value = {}
    return SyncCoordinator._build_health_telemetry(stub)


def test_the_assembler_reports_the_process_arch():
    with patch("src.main.process_arch", return_value="x86_64"):
        telemetry = _telemetry_from_real_app()

    assert telemetry["process_arch"] == "x86_64"


def test_the_assembler_carries_the_pair_that_identifies_the_wrong_build():
    """The reading the whole issue exists to enumerate, asserted as a PAIR.

    Neither field alone says "Intel build on Apple Silicon"; only both together
    do, and only if both survive the same assembly.
    """
    with patch("src.main.process_arch", return_value="x86_64"), \
            patch("src.main.true_machine_arch", return_value="arm64"):
        telemetry = _telemetry_from_real_app()

    assert telemetry["process_arch"] == "x86_64"
    assert telemetry["machine_arch"] == "arm64"


def test_a_failing_process_arch_probe_never_costs_the_heartbeat():
    with patch("src.main.process_arch", side_effect=OSError("denied")):
        telemetry = _telemetry_from_real_app()

    assert isinstance(telemetry, dict)
    assert "process_arch" not in telemetry
    assert "consecutive_sync_failures" in telemetry, "the rest of the payload survived"


def test_a_failing_process_arch_probe_does_not_take_machine_arch_with_it():
    """The two live in adjacent try/excepts on purpose. Sharing one would let a
    failure in the new field silently delete the field that already ships."""
    with patch("src.main.process_arch", side_effect=OSError("denied")), \
            patch("src.main.true_machine_arch", return_value="arm64"):
        telemetry = _telemetry_from_real_app()

    assert telemetry["machine_arch"] == "arm64"
