"""Secure credential storage using the system keychain."""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

try:
    from .config_access import get_config_dir
except ImportError:
    from config_access import get_config_dir

__all__ = ["KeychainManager", "StoredCredentials", "KeychainUnavailableError"]

logger = logging.getLogger(__name__)

SERVICE_NAME = "BetterFlow"
ACCOUNT_NAME = "api_credentials"


class KeychainUnavailableError(RuntimeError):
    """Raised when the system keychain cannot be written to.

    We deliberately do not fall back to a plaintext file — credentials must
    live in the OS keychain (macOS Keychain / Windows Credential Manager).
    """


@dataclass
class StoredCredentials:
    """Credentials stored in the system keychain."""

    api_token: str
    device_id: str
    user_email: str
    user_name: str = ""
    user_role: str = "user"

    def to_json(self) -> str:
        return json.dumps(
            {
                "api_token": self.api_token,
                "device_id": self.device_id,
                "user_email": self.user_email,
                "user_name": self.user_name,
                "user_role": self.user_role,
            }
        )

    _MAX_TOKEN_LEN = 512

    @classmethod
    def from_json(cls, data: str) -> "StoredCredentials":
        parsed = json.loads(data)
        token = parsed["api_token"]
        if (
            not isinstance(token, str)
            or len(token) > cls._MAX_TOKEN_LEN
            or "\r" in token
            or "\n" in token
        ):
            raise ValueError("api_token failed validation")
        return cls(
            api_token=token,
            device_id=str(parsed["device_id"]),
            user_email=parsed["user_email"],
            user_name=parsed.get("user_name", ""),
            user_role=parsed.get("user_role", "user"),
        )


def _legacy_credentials_file() -> Path:
    """Path to the legacy plaintext credential file (pre-2026-04)."""
    return get_config_dir() / ".credentials"


def _purge_legacy_file() -> None:
    """Remove any leftover plaintext credential file from old installs.

    Uses a single unlink() call instead of exists()-then-unlink() to
    eliminate the TOCTOU window where a symlink could be swapped in.
    """
    legacy = _legacy_credentials_file()
    try:
        if legacy.is_symlink():
            logger.warning("Legacy credential path is a symlink, refusing to unlink: %s", legacy)
            return
        legacy.unlink()
        logger.info("Removed legacy plaintext credential file at %s", legacy)
    except FileNotFoundError:
        pass  # Already gone
    except OSError as e:
        # Log but don't raise — this is best-effort cleanup.
        logger.warning("Failed to remove legacy credential file %s: %s", legacy, e)


class KeychainManager:
    """Manages secure credential storage in the system keychain only."""

    def __init__(self, service_name: str = SERVICE_NAME):
        self.service_name = service_name

    def store(self, credentials: StoredCredentials) -> bool:
        """Store credentials in the system keychain.

        Returns False (and logs) if the keychain is unavailable — the caller
        must surface this to the user. We never write plaintext to disk.
        """
        try:
            keyring.set_password(
                self.service_name, ACCOUNT_NAME, credentials.to_json()
            )
        except KeyringError as e:
            logger.error("Keychain write failed — credentials NOT stored: %s", e)
            return False
        logger.info("Credentials stored for %s", credentials.user_email)
        _purge_legacy_file()
        return True

    def load(self) -> Optional[StoredCredentials]:
        """Load credentials from the system keychain."""
        try:
            data = keyring.get_password(self.service_name, ACCOUNT_NAME)
        except KeyringError as e:
            logger.warning("Keychain read failed: %s", e)
            return None
        if not data:
            return None
        try:
            return StoredCredentials.from_json(data)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Invalid credential format in keychain: %s", e)
            return None

    def delete(self) -> bool:
        """Delete stored credentials from the keychain."""
        try:
            keyring.delete_password(self.service_name, ACCOUNT_NAME)
            logger.info("Credentials deleted from keychain")
        except PasswordDeleteError:
            # Already absent — treat as success.
            pass
        except KeyringError as e:
            logger.warning("Keychain delete failed: %s", e)
            return False
        _purge_legacy_file()
        return True

    def has_credentials(self) -> bool:
        """Check if credentials are stored."""
        return self.load() is not None
