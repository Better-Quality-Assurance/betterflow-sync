"""Login management and authentication flow."""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional, Callable

from .browser_auth import BrowserAuthFlow
from .keychain import KeychainManager, StoredCredentials

try:
    from ..sync.bf_client import (
        BetterFlowClient,
        DeviceInfo,
        BetterFlowClientError,
        BetterFlowAuthError,
    )
except ImportError:
    from sync.bf_client import (
        BetterFlowClient,
        DeviceInfo,
        BetterFlowClientError,
        BetterFlowAuthError,
    )

__all__ = ["LoginManager", "LoginState"]

logger = logging.getLogger(__name__)


@dataclass
class LoginState:
    """Current login state."""

    logged_in: bool = False
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    user_role: Optional[str] = None
    device_id: Optional[str] = None
    error: Optional[str] = None
    # True when login failed for a TRANSIENT reason (server unreachable —
    # timeout / 5xx / connection drop) with the stored credentials left
    # intact. The caller must NOT prompt re-auth in this case (the session is
    # still valid; a re-auth flow can't even complete while the server is
    # down) — it should retry auto-login in the background. False for a
    # genuine logged-out state (no credentials, or credentials cleared after
    # a definitive auth failure).
    transient: bool = False


class LoginManager:
    """Manages authentication flow."""

    def __init__(
        self,
        bf_client: BetterFlowClient,
        keychain: Optional[KeychainManager] = None,
    ):
        """Initialize login manager.

        Args:
            bf_client: BetterFlow API client
            keychain: Keychain manager (creates default if None)
        """
        self.bf = bf_client
        self.keychain = keychain or KeychainManager()
        self._on_login_callback: Optional[Callable[[LoginState], None]] = None
        self._on_logout_callback: Optional[Callable[[], None]] = None
        self._active_flow: Optional[BrowserAuthFlow] = None
        self._flow_lock = threading.Lock()

    def set_login_callback(self, callback: Callable[[LoginState], None]) -> None:
        """Set callback for login state changes."""
        self._on_login_callback = callback

    def set_logout_callback(self, callback: Callable[[], None]) -> None:
        """Set callback for logout."""
        self._on_logout_callback = callback

    def try_auto_login(self) -> LoginState:
        """Try to log in with stored credentials.

        Returns:
            LoginState with result
        """
        credentials = self.keychain.load()
        if not credentials:
            return LoginState(logged_in=False)

        # Set credentials on client
        self.bf.set_credentials(credentials.api_token, credentials.device_id)

        # Verify credentials are still valid. A single 401 is often transient
        # (the agent started mid-deploy, or a momentary backend token-lookup
        # blip) — clearing credentials on the first failure forces a needless
        # full re-login. Retry a few times with backoff and only treat the token
        # as genuinely invalid (and clear it) after the failures persist.
        auth_attempts = 3
        auth_backoff = [2.0, 4.0]  # waits between the attempts
        for attempt in range(auth_attempts):
            try:
                self.bf.get_status()
                state = LoginState(
                    logged_in=True,
                    user_email=credentials.user_email,
                    user_name=credentials.user_name or None,
                    user_role=credentials.user_role,
                    device_id=credentials.device_id,
                )
                # device_id, not email — the log is uploaded on logs_requested;
                # keep PII out of the uploaded tail (privacy F5).
                logger.info(f"Auto-login successful for device {credentials.device_id}")
                if self._on_login_callback:
                    self._on_login_callback(state)
                return state
            except BetterFlowAuthError as e:
                if attempt < auth_attempts - 1:
                    logger.warning(
                        "Auto-login auth error (attempt %d/%d): %s — retrying",
                        attempt + 1, auth_attempts, e,
                    )
                    time.sleep(auth_backoff[attempt])
                    continue
                logger.warning(f"Auto-login failed (auth) after {auth_attempts} attempts: {e}")
                self.bf.clear_credentials()
                return LoginState(logged_in=False, error="Stored credentials are invalid")
            except BetterFlowClientError as e:
                logger.warning(f"Auto-login failed (network): {e}")
                # Don't clear credentials on network error - might be temporary.
                # transient=True tells the caller to retry in the background
                # rather than kick the (still-authenticated) user to a re-auth
                # prompt that can't complete while the server is unreachable.
                return LoginState(
                    logged_in=False,
                    transient=True,
                    error="Network error - check your connection",
                )

    def login_via_browser(self) -> LoginState:
        """Log in via browser-based OAuth flow.

        Opens the browser to BetterFlow authorize page, waits for callback,
        then exchanges the code for a Sanctum token.

        Security: Uses state parameter (CSRF) and PKCE for secure auth flow.

        Returns:
            LoginState with result
        """
        authorize_url = f"{self.bf.web_base_url}/sync/auth/authorize"
        if not authorize_url.startswith("https://"):
            return LoginState(
                logged_in=False,
                error="Refusing to start OAuth flow over non-HTTPS connection",
            )
        logger.info(f"Using authorize URL: {authorize_url}")
        flow = BrowserAuthFlow(authorize_url)
        with self._flow_lock:
            if self._active_flow is not None:
                return LoginState(logged_in=False, error="Login already in progress")
            self._active_flow = flow

        logger.info("Starting browser auth flow (with PKCE)...")
        try:
            auth_result = flow.start()
        finally:
            with self._flow_lock:
                self._active_flow = None

        if not auth_result.success:
            return LoginState(
                logged_in=False,
                error=auth_result.error or "Authorization was cancelled or timed out",
            )

        # Exchange code for token (with PKCE code_verifier)
        device_info = DeviceInfo.collect()
        device_name = f"sync:{device_info.machine_id[:12]}"
        result = self.bf.exchange_code(
            auth_result.code, device_name, auth_result.code_verifier,
            device_info=device_info,
        )

        if not result.success:
            return LoginState(logged_in=False, error=result.error)

        # Store credentials in keychain
        user_email = result.user_email or device_name
        user_name = result.user_name or user_email
        user_role = result.user_role or "user"
        credentials = StoredCredentials(
            api_token=result.api_token,
            device_id=result.device_id,
            user_email=user_email,
            user_name=user_name,
            user_role=user_role,
        )

        if not self.keychain.store(credentials):
            logger.warning("Failed to store credentials in keychain")

        # Set credentials on client
        self.bf.set_credentials(result.api_token, result.device_id)

        state = LoginState(
            logged_in=True,
            user_email=user_email,
            user_name=user_name,
            user_role=user_role,
            device_id=result.device_id,
        )
        logger.info("Browser auth login successful")

        if self._on_login_callback:
            self._on_login_callback(state)

        return state

    def logout(self) -> bool:
        """Log out and revoke device token.

        Returns:
            True if server-side revocation succeeded. Local credentials
            are always cleared regardless — but False means the token
            may still be valid server-side.
        """
        revoked = False
        try:
            self.bf.revoke()
            revoked = True
        except Exception as e:
            logger.warning(
                "Failed to revoke token server-side — token may remain "
                "valid until it expires. Revoke manually from the web "
                "interface if needed. Error: %s", e,
            )

        # Always clear local credentials even if revocation failed
        self.bf.clear_credentials()
        self.keychain.delete()

        logger.info("Logged out (server revoked: %s)", revoked)

        if self._on_logout_callback:
            self._on_logout_callback()

        return revoked

    def cancel_login(self) -> None:
        """Cancel any in-progress browser login flow."""
        with self._flow_lock:
            flow = self._active_flow
        if flow is not None:
            flow.cancel()

    def is_logged_in(self) -> bool:
        """Check if user is logged in."""
        return self.bf.token is not None

    def get_current_user(self) -> Optional[str]:
        """Get current user's email."""
        credentials = self.keychain.load()
        return credentials.user_email if credentials else None
