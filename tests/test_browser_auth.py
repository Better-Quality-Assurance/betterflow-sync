"""Tests for browser-based OAuth authorization flow."""

import base64
import hashlib
import threading
import pytest
from unittest.mock import Mock, patch, MagicMock
from http.server import HTTPServer

from src.auth.browser_auth import (
    BrowserAuthFlow,
    AuthFlowResult,
    _CallbackHandler,
)
from src.auth.pkce import generate_pkce_pair


class TestPKCEGeneration:
    """Tests for PKCE code generation."""

    def test_generate_pkce_pair_returns_tuple(self):
        """Test PKCE pair generation returns verifier and challenge."""
        verifier, challenge = generate_pkce_pair()

        assert verifier is not None
        assert challenge is not None
        assert len(verifier) > 0
        assert len(challenge) > 0

    def test_generate_pkce_pair_verifier_length(self):
        """Test verifier has expected length (43 chars base64url)."""
        verifier, _ = _generate_pkce_pair()

        # secrets.token_urlsafe(32) produces 43 characters
        assert len(verifier) == 43

    def test_generate_pkce_pair_challenge_is_sha256(self):
        """Test challenge is SHA-256 hash of verifier."""
        verifier, challenge = generate_pkce_pair()

        # Manually compute expected challenge
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

        assert challenge == expected

    def test_generate_pkce_pair_is_unique(self):
        """Test each call generates unique values."""
        pair1 = generate_pkce_pair()
        pair2 = generate_pkce_pair()

        assert pair1[0] != pair2[0]  # Different verifiers
        assert pair1[1] != pair2[1]  # Different challenges

    def test_generate_pkce_pair_url_safe(self):
        """Test verifier and challenge are URL-safe."""
        verifier, challenge = generate_pkce_pair()

        # URL-safe characters only (no +, /, or =)
        for char in ["+", "/", "="]:
            assert char not in verifier
            assert char not in challenge


class TestAuthFlowResult:
    """Tests for AuthFlowResult dataclass."""

    def test_success_result(self):
        """Test successful result."""
        result = AuthFlowResult(
            success=True,
            code="auth-code-123",
            code_verifier="verifier-456",
        )

        assert result.success is True
        assert result.code == "auth-code-123"
        assert result.code_verifier == "verifier-456"
        assert result.error is None

    def test_failure_result(self):
        """Test failure result."""
        result = AuthFlowResult(
            success=False,
            error="timeout",
        )

        assert result.success is False
        assert result.code is None
        assert result.error == "timeout"


class TestCallbackHandler:
    """Tests for the HTTP callback handler."""

    def test_callback_extracts_code(self):
        """Test handler extracts authorization code."""
        # Create mock server with expected state
        mock_server = MagicMock()
        mock_server.expected_state = "test-state-123"
        mock_server.auth_code = None
        mock_server.auth_error = None
        mock_server.callback_received = threading.Event()
        mock_server.lock = threading.Lock()

        # Create mock request
        handler = _CallbackHandler.__new__(_CallbackHandler)
        handler.server = mock_server
        handler.path = "/callback?code=auth-code-456&state=test-state-123"
        handler.requestline = "GET /callback HTTP/1.1"
        handler.client_address = ("127.0.0.1", 12345)
        handler.request_version = "HTTP/1.1"
        handler.headers = {}

        # Mock response methods
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = MagicMock()

        handler.do_GET()

        assert mock_server.auth_code == "auth-code-456"
        assert mock_server.callback_received.is_set()

    def test_callback_rejects_wrong_state(self):
        """Test handler rejects mismatched state (CSRF protection)."""
        mock_server = MagicMock()
        mock_server.expected_state = "correct-state"
        mock_server.auth_code = None
        mock_server.auth_error = None
        mock_server.callback_received = threading.Event()
        mock_server.lock = threading.Lock()

        handler = _CallbackHandler.__new__(_CallbackHandler)
        handler.server = mock_server
        handler.path = "/callback?code=auth-code&state=wrong-state"
        handler.requestline = "GET /callback HTTP/1.1"
        handler.client_address = ("127.0.0.1", 12345)
        handler.request_version = "HTTP/1.1"
        handler.headers = {}

        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = MagicMock()

        handler.do_GET()

        assert mock_server.auth_code is None
        assert mock_server.auth_error == "state_mismatch"
        handler.send_response.assert_called_with(400)

    def test_callback_handles_error_response(self):
        """Test handler captures error from OAuth provider."""
        mock_server = MagicMock()
        mock_server.expected_state = "test-state"
        mock_server.auth_code = None
        mock_server.auth_error = None
        mock_server.callback_received = threading.Event()
        mock_server.lock = threading.Lock()

        handler = _CallbackHandler.__new__(_CallbackHandler)
        handler.server = mock_server
        handler.path = "/callback?error=access_denied&state=test-state"
        handler.requestline = "GET /callback HTTP/1.1"
        handler.client_address = ("127.0.0.1", 12345)
        handler.request_version = "HTTP/1.1"
        handler.headers = {}

        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = MagicMock()

        handler.do_GET()

        assert mock_server.auth_code is None
        assert mock_server.auth_error == "access_denied"

    def test_callback_404_for_wrong_path(self):
        """Test handler returns 404 for non-callback paths."""
        mock_server = MagicMock()
        mock_server.callback_received = threading.Event()

        handler = _CallbackHandler.__new__(_CallbackHandler)
        handler.server = mock_server
        handler.path = "/other-path"
        handler.requestline = "GET /other-path HTTP/1.1"
        handler.client_address = ("127.0.0.1", 12345)
        handler.request_version = "HTTP/1.1"
        handler.headers = {}

        handler.send_response = Mock()
        handler.end_headers = Mock()

        handler.do_GET()

        handler.send_response.assert_called_with(404)


class TestBrowserAuthFlow:
    """Tests for BrowserAuthFlow."""

    def test_init(self):
        """Test flow initialization."""
        flow = BrowserAuthFlow("https://betterflow.eu/sync/auth/authorize")

        assert flow._authorize_url_base == "https://betterflow.eu/sync/auth/authorize"

    @patch("webbrowser.open")
    def test_start_opens_browser_with_pkce(self, mock_browser):
        """Test flow opens browser with PKCE parameters."""
        flow = BrowserAuthFlow("https://betterflow.eu/sync/auth/authorize")
        flow.TIMEOUT_SECONDS = 0.1  # Short timeout for test

        # Run in thread to allow quick timeout
        result = flow.start()

        # Browser should have been opened
        assert mock_browser.called
        url = mock_browser.call_args[0][0]

        # URL should contain PKCE parameters
        assert "code_challenge=" in url
        assert "code_challenge_method=S256" in url
        assert "state=" in url
        assert "callback_port=" in url

    @patch("webbrowser.open")
    def test_start_timeout_returns_failure(self, mock_browser):
        """Test timeout returns failure result."""
        flow = BrowserAuthFlow("https://betterflow.eu/sync/auth/authorize")
        flow.TIMEOUT_SECONDS = 0.1

        result = flow.start()

        assert result.success is False
        assert result.error == "timeout"

    @patch("webbrowser.open")
    def test_start_returns_code_and_verifier(self, mock_browser):
        """Test successful flow returns code and verifier."""
        flow = BrowserAuthFlow("https://betterflow.eu/sync/auth/authorize")
        flow.TIMEOUT_SECONDS = 2

        def simulate_callback():
            """Simulate browser callback after short delay."""
            import time
            import requests

            time.sleep(0.1)
            # Extract port from browser URL
            url = mock_browser.call_args[0][0]
            port = url.split("callback_port=")[1].split("&")[0]
            state = url.split("state=")[1].split("&")[0]

            # Make callback request
            try:
                requests.get(
                    f"http://127.0.0.1:{port}/callback",
                    params={"code": "test-auth-code", "state": state},
                    timeout=1,
                )
            except Exception:
                pass  # Response doesn't matter for test

        # Start callback simulator in background
        callback_thread = threading.Thread(target=simulate_callback, daemon=True)
        callback_thread.start()

        result = flow.start()

        assert result.success is True
        assert result.code == "test-auth-code"
        assert result.code_verifier is not None
        assert len(result.code_verifier) == 43  # PKCE verifier length

    @patch("webbrowser.open")
    def test_start_state_mismatch_returns_failure(self, mock_browser):
        """Test state mismatch returns failure."""
        flow = BrowserAuthFlow("https://betterflow.eu/sync/auth/authorize")
        flow.TIMEOUT_SECONDS = 2

        def simulate_wrong_state_callback():
            """Simulate callback with wrong state."""
            import time
            import requests

            time.sleep(0.1)
            url = mock_browser.call_args[0][0]
            port = url.split("callback_port=")[1].split("&")[0]

            try:
                requests.get(
                    f"http://127.0.0.1:{port}/callback",
                    params={"code": "test-code", "state": "wrong-state"},
                    timeout=1,
                )
            except Exception:
                pass

        callback_thread = threading.Thread(target=simulate_wrong_state_callback, daemon=True)
        callback_thread.start()

        result = flow.start()

        assert result.success is False
        assert result.error == "state_mismatch"

    @patch("webbrowser.open")
    def test_start_error_callback_returns_failure(self, mock_browser):
        """Test error callback returns failure with error message."""
        flow = BrowserAuthFlow("https://betterflow.eu/sync/auth/authorize")
        flow.TIMEOUT_SECONDS = 2

        def simulate_error_callback():
            """Simulate OAuth error callback."""
            import time
            import requests

            time.sleep(0.1)
            url = mock_browser.call_args[0][0]
            port = url.split("callback_port=")[1].split("&")[0]
            state = url.split("state=")[1].split("&")[0]

            try:
                requests.get(
                    f"http://127.0.0.1:{port}/callback",
                    params={"error": "access_denied", "state": state},
                    timeout=1,
                )
            except Exception:
                pass

        callback_thread = threading.Thread(target=simulate_error_callback, daemon=True)
        callback_thread.start()

        result = flow.start()

        assert result.success is False
        assert result.error == "access_denied"
