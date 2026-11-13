"""Two audit findings on what reaches the cross-tenant ops ingest.

1. #307's native crash log reported a HANDLED Windows SEH exception as a fatal
   crash. faulthandler's Windows handler is a vectored exception handler: it
   runs before any __try/__except frame, so an exception a library catches
   (ctypes turns an access violation into OSError; COM raises and handles RPC
   exceptions) still writes "Windows fatal exception: ..." plus every stack,
   and the process carries on. The next launch found the file non-empty and
   sent it as level=fatal. Repeats appended without bound for the whole run.
   The text of a handled dump is identical to a fatal one; the discriminator
   is that the process OUTLIVED it, which the owner can observe on its tick
   and at clean shutdown.

2. A failing 60s tick sub-task sent ``"last_error": str(exc)``. A requests
   exception's str() carries the request URL with its query string, so a
   ``?token=`` rode to ops. Same class: the reconnect report, both
   excepthooks (message AND the stack's last line) and the log-upload report.

Fixtures: the "handled SEH" dumps are written by faulthandler itself
(dump_traceback into the owned file) under the exact header CPython's Windows
handler writes, because macOS/Linux cannot produce a survivable SEH. The
requests exception in (c) is a REAL one, raised by requests against a closed
loopback port. The installed requests/urllib3 do NOT put a proxy's
``user:pass`` in str(exc) (measured 2026-11-11), so the both-secrets fixture
is constructed and labelled as such: it pins the property for any producer
that formats a proxy URL into a ConnectionError.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
import requests

import src.main as main
from src import error_reporter, native_crash_log
from src.auth.login import LoginState
from src.native_crash_log import CRASH_FILE, PREVIOUS_FILE

HANDLED_HEADER = "Windows fatal exception: access violation\n\n"


@pytest.fixture(autouse=True)
def _isolate_owned_file():
    """enable() records the owned file module-wide; never leak it across tests."""
    yield
    fh = getattr(native_crash_log, "_owned_file", None)
    native_crash_log._owned_file = None
    native_crash_log._settle_size = 0
    if fh is not None and not fh.closed:
        faulthandler.disable()
        fh.close()


def _handled_seh(fh) -> None:
    """What CPython's Windows handler writes for an exception the process then
    survives: the header, then every thread's stack, via faulthandler."""
    fh.write(HANDLED_HEADER)
    fh.flush()
    faulthandler.dump_traceback(file=fh, all_threads=True)
    fh.write("\n")
    fh.flush()


def _end_this_run(log_dir: Path) -> None:
    """What the OS does when the owning process exits: every handle it held is
    closed. The next launch is a NEW process, so it must not find this run's
    crash-file handle (or its owner lock) still open. On Windows that matters:
    a file Python opened is not shared for delete, so the next launch's rotate
    (os.replace) fails while it is open, enable() returns None and nothing is
    reported. That is a same-process artifact; a dead process holds nothing."""
    fh = native_crash_log._owned_file
    native_crash_log._owned_file = None
    native_crash_log._settle_size = 0
    if fh is not None and not fh.closed:
        faulthandler.disable()
        fh.close()
    lock = native_crash_log._owner_locks.pop(str(Path(log_dir) / native_crash_log.LOCK_FILE), None)
    if lock is not None:
        if sys.platform == "win32":
            import msvcrt

            # Unlock explicitly: Windows releases a closed handle's locks "when
            # resources allow", not necessarily before the next enable().
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        lock.close()


def _next_launch_report(log_dir: Path) -> Mock:
    """The previous process ends; the next one takes ownership, rotates, and
    reports what the last run left."""
    _end_this_run(log_dir)
    # Fail loudly if the next launch could not take over: otherwise every
    # "not reported" assertion below passes vacuously.
    assert native_crash_log.enable(log_dir) is not None, "next launch could not take ownership"
    reporter = Mock()
    native_crash_log.report_previous(log_dir, reporter)
    return reporter


def _coordinator() -> main.SyncCoordinator:
    """A SyncCoordinator with only what _tick_60s touches (as in
    test_capture_policy_integration)."""
    c = object.__new__(main.SyncCoordinator)
    c._tick_failure_counts = {}
    c._tick_failure_reported = set()
    c.error_reporter = None
    c.reminder_manager = None
    c.apply_capture_policy = None
    c.tray = Mock()
    for name in (
        "_self_heal_capture_boundary", "_reconcile_inproc_afk_flag",
        "_check_idle_status", "_heartbeat_floor", "_refresh_hours_today",
        "_check_permissions", "_check_auth_warn", "_check_idle_tracker_health",
        "_check_private_auto_end",
    ):
        setattr(c, name, Mock())
    return c


def _shutdown_app() -> main.BetterFlowApp:
    """A BetterFlowApp with only what _shutdown touches."""
    app = object.__new__(main.BetterFlowApp)
    app._shutdown_lock = threading.Lock()
    app._shutdown_done = False
    app._shutdown_event = threading.Event()
    for name in ("coordinator", "sync_engine", "aw", "bf", "queue", "aw_manager"):
        setattr(app, name, Mock())
    app.window_watcher = app.input_watcher = app.input_source = None
    app.display_tracker = app.browser_tracker = None
    return app


# --------------------------------------------------------------------------- #
# (a) a handled SEH is not reported as a crash                                #
# --------------------------------------------------------------------------- #


def test_handled_seh_survived_across_ticks_is_not_reported_next_launch(tmp_path):
    fh = native_crash_log.enable(tmp_path)
    _handled_seh(fh)
    c = _coordinator()
    c._tick_60s()  # wrote in the last interval: not judged yet
    c._tick_60s()  # still alive a whole interval later: it was handled
    assert (tmp_path / CRASH_FILE).stat().st_size == 0
    # ...then the process is killed (no shutdown) and relaunched.
    reporter = _next_launch_report(tmp_path)
    reporter.capture.assert_not_called()


def test_handled_seh_then_clean_shutdown_is_not_reported_next_launch(tmp_path):
    fh = native_crash_log.enable(tmp_path)
    _handled_seh(fh)
    with patch.object(main, "clear_notifications"), patch(
        "src.system_events.cleanup_observers", create=True
    ):
        _shutdown_app()._shutdown()
    reporter = _next_launch_report(tmp_path)
    reporter.capture.assert_not_called()


def test_a_dump_the_process_did_not_outlive_is_still_reported_as_fatal(tmp_path):
    """Control: the discriminator must not swallow a real crash. One tick saw
    the dump arrive, then the process died before the next one."""
    fh = native_crash_log.enable(tmp_path)
    c = _coordinator()
    c._tick_60s()
    fh.write("Fatal Python error: Segmentation fault\n\n")
    fh.flush()
    faulthandler.dump_traceback(file=fh, all_threads=True)
    c._tick_60s()  # fresh write: must NOT be consumed
    reporter = _next_launch_report(tmp_path)
    reporter.capture.assert_called_once()
    kwargs = reporter.capture.call_args.kwargs
    assert kwargs["level"] == "fatal"
    assert "Segmentation fault" in kwargs["context"]["faulthandler"]
    assert not (tmp_path / PREVIOUS_FILE).exists()


def test_truncation_does_not_leave_a_hole_before_the_next_dump(tmp_path):
    """faulthandler keeps writing to the same descriptor after checkpoint()
    truncates. Without O_APPEND the next write lands at the old offset and the
    file is NULs up to there."""
    fh = native_crash_log.enable(tmp_path)
    _handled_seh(fh)
    assert native_crash_log.checkpoint(final=True) > 0
    # Write the way faulthandler does: raw bytes on the descriptor. fh.write()
    # would go through text mode and become "\r\n" on Windows, which
    # faulthandler never produces.
    os.write(fh.fileno(), b"Fatal Python error: Aborted\n")
    data = (tmp_path / CRASH_FILE).read_bytes()
    assert data == b"Fatal Python error: Aborted\n"


def _write_during_checkpoint_read(monkeypatch, fh, header: str) -> None:
    """A dump arrives while checkpoint() is reading the file it is about to
    truncate (faulthandler writes from any thread, at any moment)."""
    real_open = open

    def racing_open(path, mode="r", *a, **kw):
        if "r" in mode and str(path) == fh.name:
            fh.write(header)
            fh.flush()
            faulthandler.dump_traceback(file=fh, all_threads=True)
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr(native_crash_log, "open", racing_open, raising=False)


def test_a_dump_written_while_checkpoint_reads_is_not_erased(tmp_path, monkeypatch):
    fh = native_crash_log.enable(tmp_path)
    _handled_seh(fh)
    native_crash_log.checkpoint()  # first sight
    _write_during_checkpoint_read(monkeypatch, fh, "Fatal Python error: Segmentation fault\n\n")
    native_crash_log.checkpoint()  # quiet for an interval, but a write lands mid-look
    monkeypatch.undo()
    reporter = _next_launch_report(tmp_path)
    reporter.capture.assert_called_once()
    assert "Segmentation fault" in reporter.capture.call_args.kwargs["context"]["faulthandler"]


def test_a_storm_writing_during_every_checkpoint_is_still_capped(tmp_path, monkeypatch):
    fh = native_crash_log.enable(tmp_path)
    crash = tmp_path / CRASH_FILE
    while crash.stat().st_size <= 64 * 1024:
        _handled_seh(fh)
    _write_during_checkpoint_read(monkeypatch, fh, HANDLED_HEADER)
    native_crash_log.checkpoint()
    assert crash.stat().st_size <= 64 * 1024


@pytest.mark.skipif(sys.platform != "win32", reason="a survivable SEH exists only on Windows")
def test_a_real_handled_access_violation_is_not_reported(tmp_path):
    """The production shape: ctypes catches the access violation (OSError) after
    faulthandler's vectored handler has already dumped it."""
    import ctypes

    native_crash_log.enable(tmp_path)
    with pytest.raises(OSError):
        ctypes.string_at(0)
    assert "Windows fatal exception" in (tmp_path / CRASH_FILE).read_text(errors="replace")
    native_crash_log.checkpoint(final=True)
    reporter = _next_launch_report(tmp_path)
    reporter.capture.assert_not_called()


# --------------------------------------------------------------------------- #
# (b) repeated handled dumps stop at the in-run cap                           #
# --------------------------------------------------------------------------- #


def test_repeated_handled_dumps_stop_at_the_in_run_cap(tmp_path):
    """A handled exception every few seconds never leaves the file quiet for a
    whole interval, so only the byte cap bounds it."""
    fh = native_crash_log.enable(tmp_path)
    crash = tmp_path / CRASH_FILE
    c = _coordinator()
    sizes, written = [], 0
    for _ in range(40):  # 40 ticks, each with fresh handled dumps since the last
        before = crash.stat().st_size
        for _ in range(5):
            _handled_seh(fh)
        written += crash.stat().st_size - before
        c._tick_60s()
        sizes.append(crash.stat().st_size)
    # The requirement, stated as a number rather than read from the module: a
    # few dumps' worth on disk, never the run's whole history.
    cap = 64 * 1024
    # The cap must be what bounded it: far more was written than it allows.
    assert written > 3 * cap, written
    assert max(sizes) <= cap, sizes
    assert cap >= native_crash_log.MAX_INRUN_BYTES


# --------------------------------------------------------------------------- #
# home prefix never leaves the machine                                        #
# --------------------------------------------------------------------------- #


def test_native_dump_frame_paths_lose_the_home_prefix_on_the_wire(tmp_path, monkeypatch):
    home = tmp_path / "Users" / "sachi.popescu"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    frame = f'  File "{home}/AppData/Local/BetterFlow/_internal/pystray/_win32.py", line 42 in _run\n'
    (tmp_path / CRASH_FILE).write_text(
        "Windows fatal exception: access violation\n\n" + frame, encoding="utf-8"
    )
    native_crash_log.enable(tmp_path)
    reporter = error_reporter.ErrorReporter(
        endpoint="https://bot.example/notify/error", dsn="dsn-x", release="t"
    )
    with patch.object(reporter, "_post") as post, patch.object(
        error_reporter.threading, "Thread"
    ) as thread:
        thread.side_effect = lambda target, args, **kw: Mock(start=lambda: target(*args))
        native_crash_log.report_previous(tmp_path, reporter)
    sent = post.call_args.args[0]["context"]["faulthandler"]
    assert "pystray/_win32.py" in sent, "the frame itself must survive"
    assert "sachi.popescu" not in sent
    assert '"~/AppData/Local/BetterFlow' in sent


def test_excepthook_stack_frames_lose_the_home_prefix(monkeypatch):
    home = Path(__file__).resolve().parent.parent  # the frames below live here
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    try:
        raise RuntimeError("x")
    except RuntimeError as e:
        payload = error_reporter.ErrorReporter(
            endpoint="https://e", dsn="d", release="t"
        )._build_payload("m", "fatal", e, None, None)
    assert str(home) not in payload["stack"]
    # Frame paths use the platform separator ("~\\tests\\..." on Windows).
    expected = os.path.join("~", "tests", "test_crashlog_handled_seh_and_error_egress.py")
    assert expected in payload["stack"]


def test_a_longer_sibling_login_is_not_mangled(monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/al")))
    monkeypatch.setenv("HOME", "/Users/al")
    assert error_reporter.strip_home("/Users/alice/x /Users/al/y") == "/Users/alice/x ~/y"


@pytest.mark.parametrize(
    "homes",
    [["/Users/al", "\\Users\\al"], ["\\Users\\al", "/Users/al"]],
    ids=["slash-first", "backslash-first"],
)
def test_sibling_login_boundary_holds_for_every_home_spelling_order(monkeypatch, homes):
    # Equal-length spellings of one home (Path.home() vs $HOME on Windows) come
    # back from a set in hash order. The boundary lookahead must apply to EACH
    # alternative, not just the last one in the pattern.
    monkeypatch.setattr(error_reporter, "_home_dirs", lambda: list(homes))
    assert error_reporter.strip_home("/Users/alice/x /Users/al/y") == "/Users/alice/x ~/y"
    assert error_reporter.strip_home("\\Users\\alice\\x \\Users\\al\\y") == "\\Users\\alice\\x ~\\y"


# --------------------------------------------------------------------------- #
# (c) last_error never carries the URL's query string or proxy credentials    #
# --------------------------------------------------------------------------- #


def _real_requests_connection_error() -> requests.exceptions.ConnectionError:
    # Port 9 (discard) on loopback is closed: a real refused connect, no network.
    try:
        requests.get("http://127.0.0.1:9/api/agent/x?token=SECRETTOKEN123", timeout=5)
    except requests.exceptions.ConnectionError as e:
        return e
    raise AssertionError("expected a refused connect on 127.0.0.1:9")


def _constructed_proxy_error() -> requests.exceptions.ProxyError:
    # Constructed: see module docstring. Shape of requests' own message, with
    # the proxy URL (and its credentials) formatted in.
    return requests.exceptions.ProxyError(
        "HTTPSConnectionPool(host='app.betterflow.eu', port=443): Max retries "
        "exceeded with url: /api/agent/sync?token=SECRETTOKEN123 (Caused by "
        "ProxyError('Unable to connect to proxy http://alice:hunter2@10.0.0.5:3128'))"
    )


def _tick_last_error(exc: BaseException) -> str:
    c = _coordinator()
    c.error_reporter = Mock()

    def boom():
        raise exc

    for _ in range(main.SyncCoordinator._TICK_FAILURE_ESCALATE_THRESHOLD):
        c._run_tick_task(boom, "hours_refresh")
    c.error_reporter.capture.assert_called_once()
    return c.error_reporter.capture.call_args.kwargs["context"]["last_error"]


def test_tick_last_error_from_a_real_requests_error_drops_the_token():
    exc = _real_requests_connection_error()
    assert "SECRETTOKEN123" in str(exc)  # positive control: the raw text leaks
    last_error = _tick_last_error(exc)
    assert "SECRETTOKEN123" not in last_error
    assert "token=" not in last_error
    assert last_error == "ConnectionError [connect-failed:refused]"


def test_tick_last_error_drops_query_token_and_proxy_credentials():
    exc = _constructed_proxy_error()
    raw = str(exc)
    assert "SECRETTOKEN123" in raw and "alice:hunter2@" in raw  # positive control
    last_error = _tick_last_error(exc)
    for secret in ("SECRETTOKEN123", "token=", "alice", "hunter2", "10.0.0.5"):
        assert secret not in last_error, (secret, last_error)
    assert last_error == "ProxyError [connect-failed:proxy]"


def test_tick_last_error_for_an_unclassified_exception_is_the_class_only():
    assert _tick_last_error(ValueError("title: Salary review 2026.xlsx")) == "ValueError"


def test_reconnect_report_drops_the_token():
    app = object.__new__(main.BetterFlowApp)
    app._shutdown_event = threading.Event()
    app._login_lock = threading.Lock()
    app._RECONNECT_RETRY_INTERVAL_S = 0
    app.coordinator = MagicMock(logged_in=False)
    app.tray = MagicMock()
    app.error_reporter = MagicMock()
    app._finish_logged_in_startup = MagicMock()
    app._announce_session = MagicMock()
    app.login_manager = MagicMock()
    app.login_manager.try_auto_login.side_effect = [
        _constructed_proxy_error(),
        LoginState(logged_in=True),
    ]
    app._reconnect_loop()
    message = app.error_reporter.capture.call_args.args[0]
    assert "SECRETTOKEN123" not in message and "hunter2" not in message
    assert message.endswith("ProxyError [connect-failed:proxy]")


def test_uncaught_exception_report_carries_neither_secret():
    exc = _constructed_proxy_error()
    sent = {}
    reporter = error_reporter.ErrorReporter(endpoint="https://e", dsn="d", release="t")
    reporter._post = lambda payload: sent.update(payload)
    original = sys.excepthook
    sys.excepthook = lambda *a: None
    try:
        restore = error_reporter.install_crash_hooks(reporter)
        try:
            raise exc
        except requests.exceptions.ProxyError as e:
            sys.excepthook(type(e), e, e.__traceback__)
        restore()
    finally:
        sys.excepthook = original
    wire = sent["message"] + sent["stack"]
    for secret in ("SECRETTOKEN123", "hunter2"):
        assert secret not in wire, secret
    assert sent["message"] == "Unhandled exception: ProxyError [connect-failed:proxy]"


def test_ops_exception_summary_names_a_server_status_not_its_body():
    from src.sync.http_client import BetterFlowClientError
    from src.sync.sync_engine import ops_exception_summary

    exc = BetterFlowClientError(
        "API error (422): title 'Salary review' is too long", status_code=422
    )
    assert ops_exception_summary(exc) == "BetterFlowClientError [server status 422]"


def test_every_ops_site_routes_exception_text_through_the_summary():
    """Callsite guard: the tick, reconnect and log-upload reports must name an
    exception via ops_exception_summary, never str()/f-string of it."""
    root = Path(__file__).resolve().parent.parent / "src"
    main_src = (root / "main.py").read_text(encoding="utf-8")
    assert '"last_error": str(exc)' not in main_src
    assert '"last_error": ops_exception_summary(exc)' in main_src
    assert 'f"Reconnect attempt raised unexpectedly: {e}"' not in main_src
    engine_src = (root / "sync" / "sync_engine.py").read_text(encoding="utf-8")
    assert 'f"upload POST failed: {e}"' not in engine_src
    reporter_src = (root / "error_reporter.py").read_text(encoding="utf-8")
    assert "{exc_value}" not in reporter_src and "{args.exc_value}" not in reporter_src
    assert "traceback.format_exception(" not in reporter_src
