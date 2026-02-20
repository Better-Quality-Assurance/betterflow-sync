"""Secure credential storage using system keychain."""

import json
import logging
from dataclasses import dataclass
from typing import Optional

import keyring
from keyring.errors import KeyringError

__all__ = ["KeychainManager", "StoredCredentials"]

logger = logging.getLogger(__name__)

SERVICE_NAME = "BetterFlow Sync"
ACCOUNT_NAME = "api_credentials"


@dataclass
class StoredCredentials:
    """Credentials stored in keychain."""

    api_token: str
    device_id: str
    user_email: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "api_token": self.api_token,
                "device_id": self.device_id,
                "user_email": self.user_email,
            }
        )

    @classmethod
    def from_json(cls, data: str) -> "StoredCredentials":
        parsed = json.loads(data)
        return cls(
            api_token=parsed["api_token"],
            device_id=parsed["device_id"],
            user_email=parsed["user_email"],
        )


class KeychainManager:
    """Manages secure credential storage."""

    def __init__(self, service_name: str = SERVICE_NAME):
        """Initialize keychain manager.

        Args:
            service_name: Service name for keychain entries
        """
        self.service_name = service_name

    def store(self, credentials: StoredCredentials) -> bool:
        """Store credentials in keychain.

        Args:
            credentials: Credentials to store

        Returns:
            True if stored successfully
        """
        try:
            keyring.set_password(
                self.service_name, ACCOUNT_NAME, credentials.to_json()
            )
            logger.info(f"Credentials stored for {credentials.user_email}")
            return True
        except KeyringError as e:
            logger.error(f"Failed to store credentials: {e}")
            return False

    def load(self) -> Optional[StoredCredentials]:
        """Load credentials from keychain.

        Returns:
            StoredCredentials if found, None otherwise
        """
        try:
            data = keyring.get_password(self.service_name, ACCOUNT_NAME)
            if data:
                return StoredCredentials.from_json(data)
            return None
        except KeyringError as e:
            logger.error(f"Failed to load credentials: {e}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"Invalid credential format: {e}")
            return None

    def delete(self) -> bool:
        """Delete stored credentials.

        Returns:
            True if deleted (or didn't exist)
        """
        try:
            keyring.delete_password(self.service_name, ACCOUNT_NAME)
            logger.info("Credentials deleted")
            return True
        except keyring.errors.PasswordDeleteError:
            # Password didn't exist
            return True
        except KeyringError as e:
            logger.error(f"Failed to delete credentials: {e}")
            return False

    def has_credentials(self) -> bool:
        """Check if credentials are stored."""
        return self.load() is not None
