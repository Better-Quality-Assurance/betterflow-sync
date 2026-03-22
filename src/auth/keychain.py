"""Secure credential storage using system keychain with file fallback."""

import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import keyring
from keyring.errors import KeyringError

try:
    from .config_access import get_config_dir
except ImportError:
    from config_access import get_config_dir

__all__ = ["KeychainManager", "StoredCredentials"]

logger = logging.getLogger(__name__)

SERVICE_NAME = "BetterFlow"
ACCOUNT_NAME = "api_credentials"


@dataclass
class StoredCredentials:
    """Credentials stored in keychain."""

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

    @classmethod
    def from_json(cls, data: str) -> "StoredCredentials":
        parsed = json.loads(data)
        return cls(
            api_token=parsed["api_token"],
            device_id=parsed["device_id"],
            user_email=parsed["user_email"],
            user_name=parsed.get("user_name", ""),
            user_role=parsed.get("user_role", "user"),
        )


def _credentials_file() -> Path:
    """Get path to file-based credential storage (owner-only permissions)."""
    return get_config_dir() / ".credentials"


def _store_to_file(credentials: StoredCredentials) -> bool:
    """Store credentials to a file with restrictive permissions."""
    try:
        cred_file = _credentials_file()
        cred_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(cred_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, credentials.to_json().encode())
        finally:
            os.close(fd)
        logger.warning(
            "Credentials stored UNENCRYPTED to %s — configure system keychain to avoid this",
            cred_file,
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to store credentials to file: {e}")
        return False


def _load_from_file() -> Optional[StoredCredentials]:
    """Load credentials from file."""
    try:
        cred_file = _credentials_file()
        if cred_file.exists():
            data = cred_file.read_text()
            return StoredCredentials.from_json(data)
        return None
    except Exception as e:
        logger.warning(f"Failed to load credentials from file: {e}")
        return None


def _delete_file() -> bool:
    """Delete credential file."""
    try:
        cred_file = _credentials_file()
        if cred_file.exists():
            cred_file.unlink()
        return True
    except Exception as e:
        logger.warning(f"Failed to delete credential file: {e}")
        return False


class KeychainManager:
    """Manages secure credential storage with file fallback."""

    def __init__(self, service_name: str = SERVICE_NAME):
        self.service_name = service_name

    def store(self, credentials: StoredCredentials) -> bool:
        """Store credentials in keychain, falling back to file."""
        try:
            keyring.set_password(
                self.service_name, ACCOUNT_NAME, credentials.to_json()
            )
            logger.info(f"Credentials stored for {credentials.user_email}")
            _delete_file()  # Clean up file fallback if keychain works
            return True
        except (KeyringError, Exception) as e:
            logger.warning(f"Keychain unavailable ({e}), using file fallback")
            return _store_to_file(credentials)

    def load(self) -> Optional[StoredCredentials]:
        """Load credentials from keychain, falling back to file."""
        try:
            data = keyring.get_password(self.service_name, ACCOUNT_NAME)
            if data:
                try:
                    return StoredCredentials.from_json(data)
                except (json.JSONDecodeError, KeyError, TypeError) as e:
                    logger.warning(f"Invalid credential format in keychain: {e}")
        except (KeyringError, Exception) as e:
            logger.debug(f"Keychain read failed: {e}")

        # Try file fallback
        return _load_from_file()

    def delete(self) -> bool:
        """Delete stored credentials from both keychain and file."""
        ok = True
        try:
            keyring.delete_password(self.service_name, ACCOUNT_NAME)
            logger.info("Credentials deleted from keychain")
        except keyring.errors.PasswordDeleteError:
            pass
        except (KeyringError, Exception):
            pass

        if not _delete_file():
            ok = False

        return ok

    def has_credentials(self) -> bool:
        """Check if credentials are stored."""
        return self.load() is not None
