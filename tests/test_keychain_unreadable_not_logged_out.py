"""A keychain we cannot READ must never be reported as a user who is not LOGGED IN.

2026-08-04: three people were sent back through a browser login while their
tokens were still valid server-side — verified in prod, the old device kept
syncing events for half an hour after the "logout". Nothing server-side had
revoked, paused or deleted them.

The mechanism is entirely local. ``keyring``'s macOS backend maps
``errSecAuthFailed`` (-25293) to an exception whose own message is "make sure
executable is signed with codesign util" (``keyring/backends/macOS/api.py``),
i.e. a code-signature change on the app bundle — a re-signed build, an ad-hoc
CI build, the MDM .pkg — makes the stored item unreadable. That surfaces as
``KeyringError``, which ``KeychainManager.load()`` swallowed into ``None``:
the same value it returns for a user who has never logged in. Every caller then
concluded "no credentials" and opened a browser login.

So the property under test is NOT "the keychain works". It is that the two
states stay DISTINGUISHABLE all the way up to the decision that prompts a user.
"""

import pytest
from unittest.mock import MagicMock, patch

from keyring.errors import KeyringError, KeyringLocked

from src.auth.keychain import KeychainManager, KeychainUnavailableError
from src.auth.login import LoginManager
from src.sync.http_client import BetterFlowAuthError, BetterFlowClientError


# The verbatim shape keyring raises when the bundle's signature no longer
# matches the item's ACL. Pinned as a fixture so the test reproduces the real
# failure rather than a generic exception.
_SIGNATURE_DENIED = KeyringError(
    "Can't get password from keychain: (-25293, 'Security Auth Failure: make "
    "sure executable is signed with codesign util')"
)
_KEYCHAIN_LOCKED = KeyringLocked("Can't get password from keychain: Keychain Access Denied")


class TestReadFailureIsDistinctFromAbsence:
    """The bottom of the stack: load() must not answer both questions the same way."""

    @pytest.mark.parametrize("err", [_SIGNATURE_DENIED, _KEYCHAIN_LOCKED])
    def test_unreadable_keychain_raises(self, err):
        with patch("src.auth.keychain.keyring.get_password", side_effect=err):
            with pytest.raises(KeychainUnavailableError):
                KeychainManager().load()

    @pytest.mark.parametrize(
        "err",
        [
            RuntimeError("No such interface 'org.freedesktop.Secret.Service'"),
            OSError("Windows Credential Manager unavailable"),
        ],
    )
    def test_a_non_keyringerror_backend_failure_is_still_unreadable(self, err):
        """Backends leak their own exception types — a missing D-Bus raises a
        bare RuntimeError, the Windows backend raises OSError. Those are read
        failures too, and if they escape load() they kill the startup thread
        instead of routing to the retry path (try_auto_login only handles
        KeychainUnavailableError)."""
        with patch("src.auth.keychain.keyring.get_password", side_effect=err):
            with pytest.raises(KeychainUnavailableError):
                KeychainManager().load()

    def test_absent_item_still_returns_none(self):
        # Pins the correct existing behaviour: a user who never logged in.
        with patch("src.auth.keychain.keyring.get_password", return_value=None):
            assert KeychainManager().load() is None

    def test_corrupt_payload_still_returns_none(self):
        # We READ it fine, it is simply not usable — re-login is the right
        # answer here, unlike the unreadable case.
        with patch("src.auth.keychain.keyring.get_password", return_value="{not json"):
            assert KeychainManager().load() is None


class TestAutoLoginDoesNotTreatItAsLoggedOut:
    def _mgr(self, *, load_raises=None, load_returns=None):
        bf = MagicMock()
        keychain = MagicMock()
        if load_raises is not None:
            keychain.load.side_effect = load_raises
        else:
            keychain.load.return_value = load_returns
        return LoginManager(bf_client=bf, keychain=keychain), bf, keychain

    def test_unreadable_keychain_is_transient(self):
        mgr, bf, keychain = self._mgr(load_raises=KeychainUnavailableError("denied"))
        state = mgr.try_auto_login()
        assert state.logged_in is False
        # transient=True is what routes the caller to "retry in the background"
        # instead of "open a browser login" (main._apply_startup_login_state).
        assert state.transient is True
        assert state.credentials_unreadable is True

    def test_unreadable_keychain_never_destroys_the_credential(self):
        mgr, bf, keychain = self._mgr(load_raises=KeychainUnavailableError("denied"))
        mgr.try_auto_login()
        bf.clear_credentials.assert_not_called()
        keychain.delete.assert_not_called()

    def test_absent_credentials_stay_definitive(self):
        # The genuine logged-out case must still prompt — this is the control
        # that proves the fix did not simply make everything transient.
        mgr, bf, _ = self._mgr(load_returns=None)
        state = mgr.try_auto_login()
        assert state.logged_in is False
        assert state.transient is False
        assert state.credentials_unreadable is False

    def test_sustained_unreadable_keychain_eventually_prompts(self):
        """A signature change is permanent: retrying forever would leave the
        user in "reconnecting" with no tracking and no way to act. After the
        same window the running agent uses for auth failures, escalate."""
        clock = _FakeClock()
        mgr, bf, keychain = self._mgr(load_raises=KeychainUnavailableError("denied"))
        mgr._time_source = clock

        first = mgr.try_auto_login()
        assert first.transient is True

        clock.advance(LoginManager.AUTH_TOLERANCE_SECONDS + 1)
        later = mgr.try_auto_login()
        assert later.transient is False
        assert later.credentials_unreadable is True
        # Escalating means "ask the user", never "delete the thing we could not read".
        keychain.delete.assert_not_called()
        bf.clear_credentials.assert_not_called()

    def test_a_readable_keychain_resets_the_escalation_clock(self):
        clock = _FakeClock()
        bf = MagicMock()
        keychain = MagicMock()
        creds = _creds()
        keychain.load.side_effect = [KeychainUnavailableError("denied"), creds, creds]
        mgr = LoginManager(bf_client=bf, keychain=keychain)
        mgr._time_source = clock

        assert mgr.try_auto_login().transient is True
        clock.advance(60)
        assert mgr.try_auto_login().logged_in is True  # recovered

        # Long after the original failure, a NEW failure starts a fresh window
        # rather than inheriting the old one and escalating immediately.
        keychain.load.side_effect = KeychainUnavailableError("denied")
        clock.advance(LoginManager.AUTH_TOLERANCE_SECONDS * 5)
        assert mgr.try_auto_login().transient is True


class TestFirstRunWizardIsNotShownToAnOnboardedUser:
    """The most visible form of the bug: an unreadable keychain plus a
    setup_complete flag knocked false across a self-update relaunch (a known
    behaviour, documented at the callsite) put an already-onboarded user
    through the FIRST-RUN WIZARD."""

    def test_unreadable_keychain_does_not_trigger_the_wizard(self):
        from src.main import should_show_setup_wizard

        assert should_show_setup_wizard(
            setup_complete=False, has_credentials=False, credentials_readable=False
        ) is False

    def test_genuinely_new_user_still_gets_the_wizard(self):
        from src.main import should_show_setup_wizard

        assert should_show_setup_wizard(
            setup_complete=False, has_credentials=False, credentials_readable=True
        ) is True

    def test_onboarded_user_never_gets_the_wizard(self):
        from src.main import should_show_setup_wizard

        assert should_show_setup_wizard(
            setup_complete=True, has_credentials=False, credentials_readable=True
        ) is False
        assert should_show_setup_wizard(
            setup_complete=False, has_credentials=True, credentials_readable=True
        ) is False


class TestStartupRoutesTheStateCorrectly:
    """`_apply_startup_login_state` is the fork in the road: one branch retries
    quietly, the other opens a browser. Pin which state takes which."""

    def _app(self):
        import src.main as main

        app = object.__new__(main.BetterFlowApp)
        app.tray = MagicMock()
        app.coordinator = MagicMock()
        app._start_reconnect_retry = MagicMock()
        app._ensure_update_checks_started = MagicMock()
        app._finish_logged_in_startup = MagicMock()
        return app

    def test_unreadable_keychain_retries_instead_of_prompting(self):
        from src.auth.login import LoginState
        import src.main as main

        app = self._app()
        state = LoginState(logged_in=False, transient=True, credentials_unreadable=True)
        main.BetterFlowApp._apply_startup_login_state(app, state)

        app._start_reconnect_retry.assert_called_once()
        app.coordinator._maybe_warn_login_required.assert_not_called()

    def test_genuinely_logged_out_still_prompts(self):
        from src.auth.login import LoginState
        import src.main as main

        app = self._app()
        main.BetterFlowApp._apply_startup_login_state(app, LoginState(logged_in=False))

        app.coordinator._maybe_warn_login_required.assert_called_once()
        app._start_reconnect_retry.assert_not_called()


class _FakeClock:
    """A monotonic clock the test drives. Every clock read in these scenarios
    goes through it — a partially-injected clock is its own bug class."""

    def __init__(self, now: float = 1000.0):
        self._now = now

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _creds():
    creds = MagicMock()
    creds.api_token = "tok"
    creds.device_id = "dev"
    creds.user_email = "a@b.co"
    creds.user_name = "A"
    creds.user_role = "user"
    return creds
