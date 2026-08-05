"""Login management and authentication flow."""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional, Callable

from .browser_auth import BrowserAuthFlow
from .keychain import KeychainManager, KeychainUnavailableError, StoredCredentials

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
    # timeout / 5xx / connection drop, an auth blip inside the tolerance
    # window, or a keychain we could not read) with the stored credentials
    # left intact. The caller must NOT prompt re-auth in this case (the
    # session is still valid; a re-auth flow can't even complete while the
    # server is down) — it should retry auto-login in the background. False
    # for a genuine logged-out state (no credentials, or credentials cleared
    # after a sustained auth failure).
    transient: bool = False
    # True when the failure was "the keychain would not open", not "there is
    # no session". The credential is still on the machine and still valid;
    # nothing may delete it on this path. Set on both the tolerated and the
    # escalated outcome, so the user-facing message can say what is actually
    # wrong instead of implying the session expired.
    credentials_unreadable: bool = False


class LoginManager:
    """Manages authentication flow."""

    # How long a restore failure is tolerated before the user is asked to sign
    # in again. Deliberately the SAME window SyncCoordinator applies to a
    # running agent (_AUTH_FAILURE_LOGOUT_MIN_SECONDS): the startup path used
    # to give up after three attempts ~6 seconds apart and delete the token,
    # which is ~150x more trigger-happy than the identical decision made one
    # minute later by the running agent. A six-second backend blip during
    # launch is not evidence that a session is over.
    #
    # The window is measured ACROSS calls, not within one: BetterFlowApp's
    # reconnect loop re-invokes try_auto_login every 30s while a failure is
    # tolerated, so the tolerance accumulates naturally and needs no sleeping
    # here.
    AUTH_TOLERANCE_SECONDS = 15 * 60

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
        # Monotonic timestamp of the first failure in the current streak, or
        # None when the last restore attempt succeeded. Injectable so tests
        # drive the whole window with one clock — a partially-injected clock
        # is a time bomb that passes today and fails on a calendar boundary.
        self._time_source: Callable[[], float] = time.monotonic
        self._first_restore_failure: Optional[float] = None

    def set_login_callback(self, callback: Callable[[LoginState], None]) -> None:
        """Set callback for login state changes."""
        self._on_login_callback = callback

    def set_logout_callback(self, callback: Callable[[], None]) -> None:
        """Set callback for logout."""
        self._on_logout_callback = callback

    def try_auto_login(self) -> LoginState:
        """Try to log in with stored credentials.

        Three distinguishable outcomes, because they need three different
        responses from the caller:

        - logged in.
        - a GENUINE logged-out state — we read the keychain and it holds
          nothing usable. Prompt immediately.
        - a failure we cannot yet attribute to the session being over: the
          keychain would not open, or the server rejected the token. Both are
          reported transient until they have persisted for
          AUTH_TOLERANCE_SECONDS, and the stored credential is left alone
          until then.

        Returns:
            LoginState with result
        """
        try:
            credentials = self.keychain.load()
        except KeychainUnavailableError as e:
            # NOT a logged-out user. The credential is still on this machine;
            # we cannot see it this launch (on macOS, most often because the
            # bundle was re-signed). Deleting it or prompting on the first
            # failure is what put valid sessions through a browser login.
            return self._restore_failed(
                reason=f"Stored credentials could not be read: {e}",
                credentials_unreadable=True,
            )

        if not credentials:
            # The one state that is knowable immediately: the keychain opened
            # and holds nothing usable. Reset the streak so a later real
            # failure starts its own window.
            self._first_restore_failure = None
            return LoginState(logged_in=False)

        # Set credentials on client
        self.bf.set_credentials(credentials.api_token, credentials.device_id)

        # Verify credentials are still valid. A single 401 is often transient
        # (the agent started mid-deploy, or a momentary backend token-lookup
        # blip). These in-call retries just shorten recovery for a blip that
        # clears within seconds; the decision to give up belongs to
        # _restore_failed and its cross-call window.
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
                self._first_restore_failure = None
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
                return self._restore_failed(
                    reason=f"Stored credentials rejected: {e}",
                    clear_credentials=True,
                )
            except BetterFlowClientError as e:
                logger.warning(f"Auto-login failed (network): {e}")
                # Don't clear credentials on network error - might be temporary.
                # transient=True tells the caller to retry in the background
                # rather than kick the (still-authenticated) user to a re-auth
                # prompt that can't complete while the server is unreachable.
                #
                # Deliberately does NOT touch the failure streak in either
                # direction: an unreachable server is no evidence about the
                # credential, so it must neither start the give-up clock nor
                # reset one an auth failure already started.
                return LoginState(
                    logged_in=False,
                    transient=True,
                    error="Network error - check your connection",
                )

    def _restore_failed(
        self,
        *,
        reason: str,
        credentials_unreadable: bool = False,
        clear_credentials: bool = False,
    ) -> LoginState:
        """Decide whether a failed session restore is still worth retrying.

        Tolerated until the streak reaches AUTH_TOLERANCE_SECONDS, then
        escalated to a real prompt — the user has to be able to act eventually,
        because a signature-change keychain denial never clears on its own.

        Escalation asks the user; it never deletes the keychain item. We could
        not read that item, so we know nothing about it, and destroying it
        would turn a recoverable state into a permanent one.
        """
        now = self._time_source()
        if self._first_restore_failure is None:
            self._first_restore_failure = now
        age = now - self._first_restore_failure

        if age < self.AUTH_TOLERANCE_SECONDS:
            logger.warning(
                "Session restore failed (%.0fs/%.0fs tolerated) — keeping the "
                "stored session and retrying: %s",
                age, self.AUTH_TOLERANCE_SECONDS, reason,
            )
            return LoginState(
                logged_in=False,
                transient=True,
                credentials_unreadable=credentials_unreadable,
                error=reason,
            )

        logger.warning(
            "Session restore has failed for %.0fs — asking the user to sign in: %s",
            age, reason,
        )
        if clear_credentials:
            self.bf.clear_credentials()
        return LoginState(
            logged_in=False,
            credentials_unreadable=credentials_unreadable,
            error=reason,
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

        # A fresh session ends any restore-failure streak. Leaving a stale
        # first-failure timestamp here would make the NEXT single 401 look
        # 15 minutes old and clear the token on the first try — the exact
        # trigger-happy behaviour AUTH_TOLERANCE_SECONDS exists to stop.
        self._first_restore_failure = None

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
        """Get current user's email, or None if it cannot be determined.

        Display-only. An unreadable keychain is None here rather than an
        exception — this feeds tray labels, and a menu must not raise.
        """
        try:
            credentials = self.keychain.load()
        except KeychainUnavailableError as e:
            logger.warning("Could not read stored credentials: %s", e)
            return None
        return credentials.user_email if credentials else None
