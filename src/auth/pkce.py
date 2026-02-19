"""PKCE (Proof Key for Code Exchange) utilities.

RFC 7636: https://www.rfc-editor.org/rfc/rfc7636

PKCE protects public clients (like desktop apps) from authorization code
interception attacks by requiring a dynamically created cryptographically
random key called "code_verifier".

Flow:
1. Client generates code_verifier (secret) and code_challenge (derived)
2. Client sends code_challenge with authorization request
3. Server stores code_challenge with the authorization code
4. Client sends code_verifier with token exchange request
5. Server verifies SHA256(code_verifier) == code_challenge
"""

import base64
import hashlib
import secrets

__all__ = ["generate_pkce_pair", "compute_code_challenge"]


def generate_pkce_pair() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge.

    The code_verifier is a cryptographically random string using
    unreserved URI characters (A-Z, a-z, 0-9, -, ., _, ~).

    The code_challenge is derived from code_verifier using SHA256
    and base64url encoding without padding.

    Returns:
        Tuple of (code_verifier, code_challenge)

    Example:
        >>> verifier, challenge = generate_pkce_pair()
        >>> len(verifier)  # 43 characters (32 bytes base64url)
        43
        >>> len(challenge)  # 43 characters (SHA256 -> base64url)
        43
    """
    # Generate random 32-byte code verifier (43 chars base64url)
    code_verifier = secrets.token_urlsafe(32)

    # Create code_challenge from code_verifier
    code_challenge = compute_code_challenge(code_verifier)

    return code_verifier, code_challenge


def compute_code_challenge(code_verifier: str) -> str:
    """Compute code_challenge from code_verifier using S256 method.

    code_challenge = BASE64URL(SHA256(code_verifier))

    Args:
        code_verifier: The PKCE code verifier string

    Returns:
        Base64URL-encoded SHA256 hash without padding

    Example:
        >>> compute_code_challenge("test_verifier")
        'jMT6fjXrOW2ua7Xcpa1R9sE9E5yQHcYf0ZCaCDrGx4k'
    """
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    # Base64URL encoding: replace +/ with -_, remove padding
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
