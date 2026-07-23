"""Delivery: the notice actually reaches every platform, once, without gating.

The disclosure gap this fixes is platform-shaped. macOS never runs
``_show_consent`` — Macs only ever see the Input Monitoring gate — so hanging
the notice off the consent screen would have re-shipped the same bug to the
platform that matters most. It therefore lives in ``BetterFlowApp.run``, which
every platform executes on every launch.

Where in ``run`` is itself a requirement, not a detail. It sits AFTER the
background startup thread has started (so syncing, tracking and billing are
already running while the window is open) and BEFORE ``tray.run_blocking``
(the last point the main thread is still free, since Tk is not safe off it on
macOS). The ordering test pins that; without it "never blocks work" is prose.

The behavioural tests drive the REAL production method unbound against a stub,
so nothing here can pass on a helper the app does not call.
"""

import inspect
from unittest.mock import MagicMock, patch

from src import privacy_notice as pn
from src.config import Config
from src.main import BetterFlowApp


def _stub_app(config):
    stub = MagicMock()
    stub.config = config
    return stub


def _run_notice(config, *, shows=True, raises=None):
    """Call the production method with the CHILD PROCESS replaced.

    The notice now renders in a separate process (spawned via
    ``subprocess.run``) so Tk never touches the agent's own NSApplication. We
    drive the real production method against a stubbed ``subprocess.run`` whose
    return code encodes the child's acknowledge/dismiss decision:
    ``shows=True`` -> exit 0 (acknowledged), ``shows=False`` -> exit 2
    (dismissed). ``raises`` simulates the spawn itself failing.

    Returns the ``subprocess.run`` mock so callers can assert whether — and with
    what argv — the child was launched.
    """
    returncode = 0 if shows else 2
    run = MagicMock(return_value=MagicMock(returncode=returncode))
    if raises is not None:
        run.side_effect = raises
    with patch("src.main.subprocess.run", run):
        BetterFlowApp._show_privacy_notice_if_needed(_stub_app(config))
    return run


# ── Shown once, then stopped ────────────────────────────────────────────

def test_a_device_that_never_acknowledged_sees_it():
    config = Config()
    run = _run_notice(config)
    run.assert_called_once()
    # It must launch the isolated child, not render Tk inline.
    argv = run.call_args.args[0]
    assert "--privacy-notice" in argv, (
        "the notice was not spawned in a separate process — Tk would run in the "
        "agent's own NSApplication and reintroduce the ghost-process crash (#158)"
    )
    assert pn.needs_acknowledgement(config) is False, "the acknowledgement was not recorded"


def test_it_does_not_come_back_on_the_next_launch():
    """The "then stops" half. A notice that reappears every launch is dismissed
    reflexively and the acknowledgement stops meaning anything."""
    config = Config()
    _run_notice(config)

    second = _run_notice(Config.load())
    second.assert_not_called()


def test_a_new_text_version_shows_it_again(monkeypatch):
    config = Config()
    _run_notice(config)

    monkeypatch.setattr(pn, "NOTICE_VERSION", pn.NOTICE_VERSION + "-next")
    again = _run_notice(Config.load())
    again.assert_called_once()


def test_dismissing_without_acknowledging_records_nothing():
    config = Config()
    _run_notice(config, shows=False)
    assert pn.needs_acknowledgement(config) is True, (
        "closing the window banked an acknowledgement nobody made"
    )


# ── Never blocks work ───────────────────────────────────────────────────

def test_a_broken_window_does_not_stop_the_agent():
    """No display, a Tk that will not initialise, a hostile WM — all of these
    must cost the notice, never the tracking."""
    config = Config()
    _run_notice(config, raises=RuntimeError("no display name and no $DISPLAY"))
    # Unrecorded, so it retries next launch — but the app carried on.
    assert pn.needs_acknowledgement(config) is True


def test_a_failed_save_does_not_stop_the_agent(monkeypatch):
    config = Config()
    monkeypatch.setattr(
        Config, "save", lambda self: (_ for _ in ()).throw(OSError("read-only disk"))
    )
    _run_notice(config)  # must not raise


# ── The callsite guards ─────────────────────────────────────────────────

def test_run_shows_the_notice_on_every_platform():
    """Fails if the call is dropped from run(), not only if the logic breaks."""
    source = inspect.getsource(BetterFlowApp.run)
    assert "_show_privacy_notice_if_needed()" in source


def test_the_notice_runs_after_startup_and_before_the_tray():
    """Ordering IS the "never blocks work" guarantee.

    Before ``_startup_thread.start()`` the notice would gate syncing; after
    ``tray.run_blocking()`` it would never run at all, and moving it off the
    main thread is unsafe for Tk on macOS.
    """
    source = inspect.getsource(BetterFlowApp.run)
    startup = source.index("self._startup_thread.start()")
    notice = source.index("self._show_privacy_notice_if_needed()")
    tray = source.index("self.tray.run_blocking()")
    assert startup < notice < tray, (
        "the notice moved out of the window between background startup and the "
        "tray loop — it now either gates tracking or never runs"
    )


def test_the_notice_is_not_hidden_behind_the_consent_screen():
    """macOS never runs _show_consent. Gating on it would re-ship the bug."""
    source = inspect.getsource(BetterFlowApp.run)
    notice = source.index("self._show_privacy_notice_if_needed()")
    # It must not sit inside the `if not self.config.setup_complete:` block.
    setup_gate = source.index("if not self.config.setup_complete:")
    tray = source.index("self.tray.run_blocking()")
    assert setup_gate < notice < tray
    line = [
        line for line in source.splitlines()
        if "self._show_privacy_notice_if_needed()" in line
    ][0]
    # Top level of run(): 8 spaces of indentation, not nested in a branch.
    assert len(line) - len(line.lstrip()) == 8, (
        "the notice is nested inside a conditional in run() — some platform or "
        "some state will not see it"
    )


def test_the_handler_delegates_to_the_shared_policy():
    source = inspect.getsource(BetterFlowApp._show_privacy_notice_if_needed)
    doc = BetterFlowApp._show_privacy_notice_if_needed.__doc__
    if doc:
        source = source.replace(doc, "")
    assert "needs_acknowledgement(" in source
    assert "record_acknowledgement(" in source
    assert "NOTICE_VERSION" not in source, "version comparison re-rolled at the callsite"
    assert "privacy_notice_ack_version" not in source, (
        "the handler reads the config field directly instead of asking the policy"
    )


def test_the_handler_spawns_a_child_and_never_runs_tk_inline():
    """The whole point of the fix: Tk must NOT run in the agent's process.

    If ``show_privacy_notice`` is imported/called inside the handler, Tk's
    mainloop runs in the process that then calls ``tray.run_blocking()`` — the
    exact sequence that killed the tray on macOS (#158). The handler must spawn
    a subprocess instead.
    """
    source = inspect.getsource(BetterFlowApp._show_privacy_notice_if_needed)
    # Drop the def line — the method's own name contains "show_privacy_notice".
    body = "\n".join(source.splitlines()[1:])
    assert "subprocess.run(" in body, "the notice is no longer spawned out of process"
    assert "_privacy_notice_child_argv(" in body
    assert "import show_privacy_notice" not in body, (
        "the window's Tk renderer is imported into the agent process again"
    )
    assert "show_privacy_notice()" not in body, (
        "Tk is being rendered inline in the agent process again — this is the "
        "regression the child-process split exists to prevent"
    )
