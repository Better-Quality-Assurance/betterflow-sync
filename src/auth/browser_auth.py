"""Browser-based OAuth authorization flow.

Opens the user's browser to the BetterFlow authorize page.
A local HTTP server receives the callback with the authorization code.
"""

import logging
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

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

        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]

        if code:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_SUCCESS_HTML.encode())
            # Store the code on the server instance
            self.server.auth_code = code
        else:
            error = params.get("error", ["unknown"])[0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_ERROR_HTML.encode())
            self.server.auth_error = error

        # Signal that we received a response
        self.server.callback_received.set()

    def log_message(self, format, *args):
        """Suppress default HTTP server logs."""
        logger.debug(f"Callback server: {format % args}")


class BrowserAuthFlow:
    """Manages the browser-based authorization flow.

    1. Starts a local HTTP server on a random port
    2. Opens the browser to the authorize URL
    3. Waits for the callback with the authorization code
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

    def start(self) -> Optional[str]:
        """Run the full auth flow and return the authorization code.

        Returns:
            Authorization code string on success, None on failure/timeout.
        """
        # Create server on random port
        self._server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        self._server.auth_code = None
        self._server.auth_error = None
        self._server.callback_received = threading.Event()

        port = self._server.server_address[1]
        logger.info(f"Callback server listening on port {port}")

        # Start server in daemon thread
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        # Open browser
        authorize_url = f"{self._authorize_url_base}?callback_port={port}"
        logger.info(f"Opening browser for authorization: {authorize_url}")
        webbrowser.open(authorize_url)

        # Wait for callback
        got_response = self._server.callback_received.wait(timeout=self.TIMEOUT_SECONDS)

        # Shut down server
        self._server.shutdown()

        if not got_response:
            logger.warning("Authorization timed out (no callback received)")
            return None

        if self._server.auth_code:
            logger.info("Authorization code received")
            return self._server.auth_code

        logger.warning(f"Authorization failed: {self._server.auth_error}")
        return None
