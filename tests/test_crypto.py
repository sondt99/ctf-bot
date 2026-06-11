"""Tests for bot.crypto — Fernet token encryption/decryption."""
from __future__ import annotations

from cryptography.fernet import Fernet

from bot.crypto import decrypt_token, encrypt_token


def _make_key() -> str:
    return Fernet.generate_key().decode()


def test_encrypt_decrypt_roundtrip():
    key = _make_key()
    token = "my-super-secret-api-token"
    encrypted = encrypt_token(token, key)
    assert encrypted is not None
    assert encrypted != token, "Encrypted token must differ from plaintext"
    assert decrypt_token(encrypted, key) == token


def test_none_input_returns_none():
    key = _make_key()
    assert encrypt_token(None, key) is None
    assert decrypt_token(None, key) is None


def test_no_key_passthrough_encrypt():
    """Without a key, encrypt_token returns the value unchanged (no encryption)."""
    assert encrypt_token("token123", None) == "token123"
    assert encrypt_token("token123", "") == "token123"


def test_no_key_passthrough_decrypt():
    """Without a key, decrypt_token returns the value unchanged."""
    assert decrypt_token("token123", None) == "token123"
    assert decrypt_token("token123", "") == "token123"


def test_backward_compat_plaintext_decrypt():
    """Decrypting a plain-text token with a key must not raise — return as-is."""
    key = _make_key()
    plaintext = "old-plaintext-token-not-encrypted"
    result = decrypt_token(plaintext, key)
    assert result == plaintext, "Backward compat: plaintext token returned unchanged"


def test_different_keys_cannot_decrypt():
    """A token encrypted with key A cannot be decrypted with key B."""
    key_a = _make_key()
    key_b = _make_key()
    encrypted = encrypt_token("secret", key_a)
    assert encrypted is not None
    # Should fall back to returning ciphertext as-is (backward compat path)
    result = decrypt_token(encrypted, key_b)
    assert result == encrypted, "Wrong key must return ciphertext unchanged (not raise)"
