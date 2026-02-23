"""Login management and authentication flow."""

import logging
import platform
from dataclasses import dataclass
from typing import Optional, Callable

from .browser_auth import BrowserAuthFlow
from .keychain import KeychainManager, StoredCredentials

try:
    from ..sync.bf_client import (
        BetterFlowClient,
        AuthResult,
        BetterFlowClientError,
        BetterFlowAuthError,
    )
except ImportError:
    from sync.bf_client import (
        BetterFlowClient,
        AuthResult,
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
    device_id: Optional[str] = None
    error: Optional[str] = None


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

        # Verify credentials are still valid
        try:
            self.bf.get_status()
            state = LoginState(
                logged_in=True,
                user_email=credentials.user_email,
                device_id=credentials.device_id,
            )
            logger.info(f"Auto-login successful for {credentials.user_email}")
            if self._on_login_callback:
                self._on_login_callback(state)
            return state
        except BetterFlowAuthError as e:
            logger.warning(f"Auto-login failed (auth): {e}")
            self.bf.clear_credentials()
            return LoginState(logged_in=False, error="Stored credentials are invalid")
        except BetterFlowClientError as e:
            logger.warning(f"Auto-login failed (network): {e}")
            # Don't clear credentials on network error - might be temporary
            return LoginState(logged_in=False, error=f"Cannot verify credentials: {e}")

    def login_via_browser(self) -> LoginState:
        """Log in via browser-based OAuth flow.

        Opens the browser to BetterFlow authorize page, waits for callback,
        then exchanges the code for a Sanctum token.

        Security: Uses state parameter (CSRF) and PKCE for secure auth flow.

        Returns:
            LoginState with result
        """
        authorize_url = f"{self.bf.web_base_url}/sync/auth/authorize"
        logger.info(f"Using authorize URL: {authorize_url}")
        flow = BrowserAuthFlow(authorize_url)

        logger.info("Starting browser auth flow (with PKCE)...")
        auth_result = flow.start()

        if not auth_result.success:
            return LoginState(
                logged_in=False,
                error=auth_result.error or "Authorization was cancelled or timed out",
            )

        # Exchange code for token (with PKCE code_verifier)
        device_name = f"sync:{platform.node()}"
        result = self.bf.exchange_code(
            auth_result.code, device_name, auth_result.code_verifier
        )

        if not result.success:
            return LoginState(logged_in=False, error=result.error)

        # Store credentials in keychain
        user_email = result.user_email or device_name
        user_name = result.user_name or user_email
        credentials = StoredCredentials(
            api_token=result.api_token,
            device_id=result.device_id,
            user_email=user_email,
        )

        if not self.keychain.store(credentials):
            logger.warning("Failed to store credentials in keychain")

        # Set credentials on client
        self.bf.set_credentials(result.api_token, result.device_id)

        state = LoginState(
            logged_in=True,
            user_email=user_email,
            user_name=user_name,
            device_id=result.device_id,
        )
        logger.info("Browser auth login successful")

        if self._on_login_callback:
            self._on_login_callback(state)

        return state

    def logout(self) -> bool:
        """Log out and revoke device token.

        Returns:
            True if successful
        """
        # Revoke token on server
        try:
            self.bf.revoke()
        except Exception as e:
            logger.warning(f"Failed to revoke token: {e}")

        # Clear local credentials
        self.bf.clear_credentials()
        self.keychain.delete()

        logger.info("Logged out")

        if self._on_logout_callback:
            self._on_logout_callback()

        return True

    def is_logged_in(self) -> bool:
        """Check if user is logged in."""
        return self.bf.token is not None

    def get_current_user(self) -> Optional[str]:
        """Get current user's email."""
        credentials = self.keychain.load()
        return credentials.user_email if credentials else None
