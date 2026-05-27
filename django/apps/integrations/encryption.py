"""
django/apps/integrations/encryption.py

Symmetric encryption utilities for storing GitHub PATs and webhook secrets.

Uses the `cryptography` library's Fernet (AES-128-CBC + HMAC-SHA256).
The shared key is loaded from settings.FERNET_ENCRYPTION_KEY and must be
the same across both the Django and FastAPI services so that FastAPI can
decrypt PATs when it needs to authenticate against GitHub at index time.

Key generation (run once, store in .env.docker):
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Architecture reference: NeuralOps Technical Documentation — Section 20 (Security)
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


# ── Module-level Fernet singleton ─────────────────────────────────────────────
# Initialised lazily on first use so that import-time errors are surfaced
# with a clear message rather than a generic AttributeError.

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    """
    Return the module-level Fernet cipher instance.

    Creates the instance on first call using settings.FERNET_ENCRYPTION_KEY.

    Raises:
        ImproperlyConfigured — if the key is absent or malformed.
    """
    global _fernet

    if _fernet is not None:
        return _fernet

    raw_key: str | None = getattr(settings, "FERNET_ENCRYPTION_KEY", None)

    if not raw_key:
        raise ImproperlyConfigured(
            "FERNET_ENCRYPTION_KEY is not set. "
            "Generate a key with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\" "
            "and add it to your environment / .env.docker file."
        )

    try:
        _fernet = Fernet(raw_key.encode() if isinstance(raw_key, str) else raw_key)
    except (ValueError, Exception) as exc:
        raise ImproperlyConfigured(
            f"FERNET_ENCRYPTION_KEY is invalid: {exc}. "
            "Ensure it is a valid 32-byte URL-safe base64-encoded key."
        ) from exc

    return _fernet


# ── Public API ─────────────────────────────────────────────────────────────────

def encrypt_secret(plain_text: str) -> str:
    """
    Encrypt a plain-text secret (PAT, webhook secret, etc.) using Fernet
    symmetric encryption.

    Args:
        plain_text: The secret string to encrypt.

    Returns:
        A URL-safe base64-encoded ciphertext string that can be safely
        stored in the database.

    Raises:
        ImproperlyConfigured: If FERNET_ENCRYPTION_KEY is missing / invalid.
        ValueError: If plain_text is empty.
    """
    if not plain_text:
        raise ValueError("plain_text must not be empty.")

    fernet = _get_fernet()
    token: bytes = fernet.encrypt(plain_text.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_secret(cipher_text: str) -> str:
    """
    Decrypt a Fernet-encrypted ciphertext back to its plain-text form.

    Args:
        cipher_text: The base64-encoded ciphertext produced by encrypt_secret().

    Returns:
        The original plain-text string.

    Raises:
        ImproperlyConfigured: If FERNET_ENCRYPTION_KEY is missing / invalid.
        ValueError: If cipher_text is empty or the token is invalid / tampered.
    """
    if not cipher_text:
        raise ValueError("cipher_text must not be empty.")

    fernet = _get_fernet()

    try:
        plain_bytes: bytes = fernet.decrypt(cipher_text.encode("utf-8"))
    except InvalidToken as exc:
        raise ValueError(
            "Failed to decrypt secret. The ciphertext is invalid, tampered, "
            "or was encrypted with a different key."
        ) from exc

    return plain_bytes.decode("utf-8")