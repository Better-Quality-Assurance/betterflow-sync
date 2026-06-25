"""The self-update relaunch helper must VERIFY the app actually started.

Botond (2026-06-25): a 09:18 self-update relaunch logged "open OK" but the
agent never synced for 6.5h (until the next relaunch at 15:35). `open` exiting 0
only means LaunchServices accepted the request — the instance can silently fail
to start, leaving no running process. The helper must confirm a live process
(and retry the open otherwise) instead of trusting `open`'s exit code.
"""

import subprocess

from src.self_updater import _build_macos_relaunch_script


def test_relaunch_script_verifies_a_live_process_after_open():
    script = _build_macos_relaunch_script("/Applications/BetterFlow 2.app", 4242, "/tmp/r.log")

    # Verifies a real process is running (not just that `open` returned 0).
    assert "pgrep -f" in script
    assert "/Contents/MacOS/" in script
    # The success path is gated on the pgrep, not on open's rc alone.
    assert "process alive" in script
    # On open-returns-0-but-no-process it retries rather than exiting.
    assert "NO running process" in script
    # Still waits for the old pid and opens the bundle (unchanged invariants).
    assert "kill -0 4242" in script
    assert "open \"$A\"" in script


def test_relaunch_script_is_valid_sh():
    script = _build_macos_relaunch_script("/Applications/BetterFlow 2.app", 4242, "/tmp/r.log")
    # `sh -n` parses without executing — catches quoting/syntax regressions in
    # the embedded shell (the bundle path contains a space).
    result = subprocess.run(["/bin/sh", "-n", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, f"generated relaunch script is not valid sh: {result.stderr}"


def test_relaunch_script_no_longer_trusts_bare_open_ok():
    """Regression guard: the pre-fix helper logged 'open OK' and exited on rc==0
    alone. The success log line must now require the liveness check."""
    script = _build_macos_relaunch_script("/Applications/BetterFlow.app", 1, "/tmp/r.log")
    # There must be no success/exit that fires purely on `open` rc==0 without a
    # subsequent pgrep gate. Assert the only "exit 0" follows the pgrep branch.
    assert "if [ \"$rc\" -eq 0 ]; then echo \"$(date -u +%FT%TZ) open OK (try $i)\" >> \"$L\"; exit 0; fi" not in script
