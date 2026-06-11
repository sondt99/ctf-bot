from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def encrypt_token(token: str | None, fernet_key: str | None) -> str | None:
    """Encrypt a token using Fernet symmetric encryption.

    If fernet_key is None, the token is returned as-is (backward compat).
    """
    if token is None:
        return None
    if not fernet_key:
        return token
    try:
        from cryptography.fernet import Fernet

        f = Fernet(fernet_key.encode())
        return f.encrypt(token.encode()).decode()
    except Exception as exc:
        log.warning("Failed to encrypt token: %s", exc)
        return token


def decrypt_token(token: str | None, fernet_key: str | None) -> str | None:
    """Decrypt a Fernet-encrypted token.

    If the token was stored before encryption was enabled, returns it as-is
    (backward compat: InvalidToken is caught and the raw value is returned).
    """
    if token is None:
        return None
    if not fernet_key:
        return token
    try:
        from cryptography.fernet import Fernet

        f = Fernet(fernet_key.encode())
        return f.decrypt(token.encode()).decode()
    except Exception:
        # Token was stored before encryption was configured — return plaintext
        return token
