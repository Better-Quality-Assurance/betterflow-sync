"""Tests for the betterqa-bot failure reporter.

These cover the contract that matters operationally: it stays silent unless a
DSN is configured, it builds the payload + Bearer auth the ingest endpoint
expects, it de-duplicates a failure within the cooldown window so a flapping
agent can't flood the logs channel, and it never raises into the caller.
"""
from __future__ import annotations

import sys
import threading
from unittest.mock import MagicMock, patch

from src.error_reporter import PROJECT, ErrorReporter, from_env, install_crash_hooks


def _reporter(**kw) -> ErrorReporter:
    defaults = {
        "endpoint": "https://bot.example/notify/error",
        "dsn": "dsn-token-1234567890",
        "release": "1.5.31",
    }
    defaults.update(kw)
    return ErrorReporter(**defaults)


class TestDisabled:
    def test_no_dsn_is_disabled_and_never_posts(self) -> None:
        r = _reporter(dsn=None)
        assert r.enabled is False
        with patch("src.error_reporter.requests.post") as post:
            r.capture("boom", block=True)
            post.assert_not_called()

    def test_no_endpoint_is_disabled(self) -> None:
        assert _reporter(endpoint="").enabled is False

    def test_from_env_off_without_dsn(self, monkeypatch) -> None:
        monkeypatch.delenv("BETTERFLOW_ERROR_DSN", raising=False)
        assert from_env(release="1.0.0").enabled is False


class TestEndpointScheme:
    """The DSN rides in `Authorization: Bearer ...` on every report, so a
    non-HTTPS endpoint must be refused (it would send the token in clear).
    Loopback is exempt for local dev. Pre-fix, _post sent over any scheme."""

    def test_http_endpoint_is_refused(self) -> None:
        r = _reporter(endpoint="http://bot.example/notify/error")
        with patch("src.error_reporter.requests.post") as post:
            r.capture("boom", block=True)
            post.assert_not_called()

    def test_loopback_http_is_allowed_for_dev(self) -> None:
        r = _reporter(endpoint="http://127.0.0.1:8080/notify/error")
        with patch("src.error_reporter.requests.post") as post:
            r.capture("boom", block=True)
            post.assert_called_once()

    def test_https_endpoint_posts(self) -> None:
        r = _reporter(endpoint="https://bot.example/notify/error")
        with patch("src.error_reporter.requests.post") as post:
            r.capture("boom", block=True)
            post.assert_called_once()


class TestPayload:
    def test_builds_expected_payload_and_auth(self) -> None:
        r = _reporter(
            environment="production",
            context_provider=lambda: {"user_email": "u@betterqa.co", "device_id": "dev-1"},
        )
        with patch("src.error_reporter.requests.post") as post:
            post.return_value = MagicMock(status_code=200, text="ok")
            r.capture(
                "Sync failing repeatedly",
                level="error",
                tags={"component": "sync"},
                context={"consecutive_failures": 3},
                block=True,
            )
            post.assert_called_once()
            _, kwargs = post.call_args
            assert kwargs["headers"]["Authorization"] == "Bearer dsn-token-1234567890"
            body = kwargs["json"]
            assert body["project"] == PROJECT
            assert body["environment"] == "production"
            assert body["level"] == "error"
            assert body["release"] == "1.5.31"
            assert body["tags"]["component"] == "sync"
            assert body["tags"]["platform"]  # filled from platform.system()
            # user/device context is merged so the report names who failed
            assert body["context"]["user_email"] == "u@betterqa.co"
            assert body["context"]["device_id"] == "dev-1"
            assert body["context"]["consecutive_failures"] == 3

    def test_includes_stack_from_exception(self) -> None:
        r = _reporter()
        try:
            raise ValueError("kaboom")
        except ValueError as e:
            with patch("src.error_reporter.requests.post") as post:
                post.return_value = MagicMock(status_code=200, text="ok")
                r.capture("crashed", level="fatal", exc=e, block=True)
                body = post.call_args.kwargs["json"]
                assert "ValueError: kaboom" in body["stack"]
                assert "Traceback" in body["stack"]


class TestDedup:
    def test_same_fingerprint_sent_once_within_window(self) -> None:
        r = _reporter(dedup_window=10_000)
        with patch("src.error_reporter.requests.post") as post:
            post.return_value = MagicMock(status_code=200, text="ok")
            r.capture("repeat", fingerprint="same", block=True)
            r.capture("repeat", fingerprint="same", block=True)
            assert post.call_count == 1

    def test_distinct_fingerprints_both_sent(self) -> None:
        r = _reporter(dedup_window=10_000)
        with patch("src.error_reporter.requests.post") as post:
            post.return_value = MagicMock(status_code=200, text="ok")
            r.capture("a", fingerprint="fp-a", block=True)
            r.capture("b", fingerprint="fp-b", block=True)
            assert post.call_count == 2


class TestNeverRaises:
    def test_transport_error_is_swallowed(self) -> None:
        r = _reporter()
        with patch("src.error_reporter.requests.post", side_effect=OSError("network down")):
            # Must not propagate — reporting can never take down the agent.
            r.capture("boom", block=True)

    def test_rejected_status_is_swallowed(self) -> None:
        r = _reporter()
        with patch("src.error_reporter.requests.post") as post:
            post.return_value = MagicMock(status_code=401, text="unauthorized")
            r.capture("boom", block=True)  # should log, not raise

    def test_bad_context_provider_does_not_break_capture(self) -> None:
        def boom() -> dict:
            raise RuntimeError("provider exploded")

        r = _reporter(context_provider=boom)
        with patch("src.error_reporter.requests.post") as post:
            post.return_value = MagicMock(status_code=200, text="ok")
            r.capture("still sends", block=True)
            post.assert_called_once()


class TestCrashHooks:
    """install_crash_hooks must report fatal, chain to the prior hook, and
    restore cleanly so it never leaks global state across tests."""

    def test_sys_excepthook_reports_chains_and_restores(self) -> None:
        reporter = MagicMock()
        chained = MagicMock()
        original = sys.excepthook
        sys.excepthook = chained  # quiet, observable previous hook
        try:
            restore = install_crash_hooks(reporter)
            assert sys.excepthook is not chained
            sys.excepthook(RuntimeError, RuntimeError("boom"), None)
            reporter.capture.assert_called_once()
            assert reporter.capture.call_args.kwargs["level"] == "fatal"
            assert reporter.capture.call_args.kwargs["block"] is True
            chained.assert_called_once()  # previous hook still runs
            restore()
            assert sys.excepthook is chained
        finally:
            sys.excepthook = original

    def test_thread_excepthook_reports_with_thread_name(self) -> None:
        reporter = MagicMock()
        chained = MagicMock()
        original = threading.excepthook
        threading.excepthook = chained
        try:
            restore = install_crash_hooks(reporter)

            class _Args:
                exc_type = RuntimeError
                exc_value = RuntimeError("thread boom")
                exc_traceback = None
                thread = threading.current_thread()

            threading.excepthook(_Args())
            reporter.capture.assert_called_once()
            assert reporter.capture.call_args.kwargs["tags"]["component"] == "uncaught-thread"
            chained.assert_called_once()
            restore()
            assert threading.excepthook is chained
        finally:
            threading.excepthook = original

    def test_keyboardinterrupt_is_not_reported(self) -> None:
        reporter = MagicMock()
        original = sys.excepthook
        sys.excepthook = MagicMock()
        try:
            restore = install_crash_hooks(reporter)
            sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
            reporter.capture.assert_not_called()  # a Ctrl-C is not a crash
            restore()
        finally:
            sys.excepthook = original


class TestNonBlocking:
    def test_non_blocking_capture_joins_eventually(self) -> None:
        r = _reporter()
        with patch("src.error_reporter.requests.post") as post:
            post.return_value = MagicMock(status_code=200, text="ok")
            r.capture("async", block=False)
            # Drain the daemon sender thread.
            import threading

            for t in threading.enumerate():
                if t.name == "error-report":
                    t.join(timeout=5)
            post.assert_called_once()
