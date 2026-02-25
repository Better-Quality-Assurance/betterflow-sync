"""Browser-based OAuth authorization flow.

Opens the user's browser to the BetterFlow authorize page.
A local HTTP server receives the callback with the authorization code.

Security features:
- State parameter (CSRF protection)
- PKCE (Proof Key for Code Exchange) for public client security
"""

import logging
import secrets
import threading
import webbrowser
from dataclasses import dataclass
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import urlparse, parse_qs, quote

from .pkce import generate_pkce_pair

__all__ = ["BrowserAuthFlow", "AuthFlowResult"]

logger = logging.getLogger(__name__)


@dataclass
class AuthFlowResult:
    """Result of browser auth flow."""

    success: bool
    code: Optional[str] = None
    code_verifier: Optional[str] = None  # For PKCE token exchange
    error: Optional[str] = None

_SUCCESS_HTML = """\
<!DOCTYPE html>
<html>
<head>
    <title>BetterFlow Sync - Authorized</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; background: #f3f4f6; }
        .card { background: white; border-radius: 12px; padding: 40px; text-align: center; box-shadow: 0 4px 12px rgba(0,0,0,.1); max-width: 400px; }
        .icon { width: 64px; height: 64px; background: #dcfce7; border-radius: 50%; display: flex; align-items: center; justify-content: center; margin: 0 auto 20px; }
        .icon svg { width: 32px; height: 32px; color: #22c55e; }
        h1 { font-size: 22px; color: #111827; margin: 0 0 8px; }
        p { color: #6b7280; margin: 0; }
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">
            <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
        </div>
        <h1>Authorization Successful</h1>
        <p>You can close this tab and return to BetterFlow Sync.</p>
    </div>
</body>
</html>
"""

_ERROR_HTML = """\
<!DOCTYPE html>
<html>
<head>
    <title>BetterFlow Sync - Error</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; background: #f3f4f6; }
        .card { background: white; border-radius: 12px; padding: 40px; text-align: center; box-shadow: 0 4px 12px rgba(0,0,0,.1); max-width: 400px; }
        .icon { width: 64px; height: 64px; background: #fee2e2; border-radius: 50%; display: flex; align-items: center; justify-content: center; margin: 0 auto 20px; }
        .icon svg { width: 32px; height: 32px; color: #ef4444; }
        h1 { font-size: 22px; color: #111827; margin: 0 0 8px; }
        p { color: #6b7280; margin: 0; }
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">
            <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
        </div>
        <h1>Authorization Failed</h1>
        <p>Something went wrong. Please try again from the app.</p>
    </div>
</body>
</html>
"""


class _CallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler that captures the authorization callback."""

    def do_GET(self):  # noqa: N802 – required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)

        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        # Only process the first callback; ignore subsequent requests.
        with self.server.lock:
            if self.server.callback_received.is_set():
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(_SUCCESS_HTML.encode())
                return

            params = parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            state = params.get("state", [None])[0]

            # Verify state parameter (CSRF protection)
            if not state or state != self.server.expected_state:
                logger.warning("State parameter mismatch - possible CSRF attempt")
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(_ERROR_HTML.encode())
                self.server.auth_error = "state_mismatch"
                self.server.callback_received.set()
                return

            if code:
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(_SUCCESS_HTML.encode())
                self.server.auth_code = code
            else:
                error = params.get("error", ["unknown"])[0]
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(_ERROR_HTML.encode())
                self.server.auth_error = error

            self.server.callback_received.set()

    def log_message(self, format, *args):
        """Suppress default HTTP server logs."""
        logger.debug(f"Callback server: {format % args}")


class BrowserAuthFlow:
    """Manages the browser-based authorization flow.

    Security features:
    - State parameter for CSRF protection
    - PKCE (Proof Key for Code Exchange) for public client security

    Flow:
    1. Generate state and PKCE code_verifier/code_challenge
    2. Start a local HTTP server on a random port
    3. Open the browser to the authorize URL with state and code_challenge
    4. Wait for the callback with the authorization code
    5. Verify state parameter matches
    6. Return code and code_verifier for token exchange
    """

    TIMEOUT_SECONDS = 300  # 5 minutes

    def __init__(self, authorize_url_base: str):
        """Initialize browser auth flow.

        Args:
            authorize_url_base: Base URL for the authorize page,
                e.g. "https://betterflow.eu/sync/auth/authorize"
        """
        self._authorize_url_base = authorize_url_base
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def cancel(self) -> None:
        """Cancel a running auth flow, unblocking start() immediately."""
        if self._server is not None:
            self._server.callback_received.set()

    def start(self) -> AuthFlowResult:
        """Run the full auth flow and return the authorization code.

        Returns:
            AuthFlowResult with code and code_verifier on success.
        """
        # Generate security tokens
        state = secrets.token_urlsafe(32)
        code_verifier, code_challenge = generate_pkce_pair()

        # Create server on random port
        self._server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        self._server.lock = threading.Lock()
        self._server.auth_code = None
        self._server.auth_error = None
        self._server.expected_state = state
        self._server.callback_received = threading.Event()

        port = self._server.server_address[1]
        logger.info(f"Callback server listening on port {port}")

        # Start server in daemon thread
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        try:
            # Open browser with state and PKCE challenge (URL-encoded for safety)
            authorize_url = (
                f"{self._authorize_url_base}"
                f"?callback_port={port}"
                f"&state={quote(state, safe='')}"
                f"&code_challenge={quote(code_challenge, safe='')}"
                f"&code_challenge_method=S256"
            )
            logger.info("Opening browser for authorization")
            webbrowser.open(authorize_url)

            # Wait for callback
            got_response = self._server.callback_received.wait(timeout=self.TIMEOUT_SECONDS)

            if not got_response:
                logger.warning("Authorization timed out (no callback received)")
                return AuthFlowResult(success=False, error="timeout")

            if self._server.auth_code:
                logger.info("Authorization code received (state verified)")
                return AuthFlowResult(
                    success=True,
                    code=self._server.auth_code,
                    code_verifier=code_verifier,
                )

            logger.warning(f"Authorization failed: {self._server.auth_error}")
            return AuthFlowResult(success=False, error=self._server.auth_error)
        finally:
            # Always shut down server and clean up thread
            self._server.shutdown()
            if self._thread is not None:
                self._thread.join(timeout=2.0)  # Wait up to 2s for thread to finish
