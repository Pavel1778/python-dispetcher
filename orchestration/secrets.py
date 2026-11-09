"""Server-side secret encryption for stored provider credentials.

One place, one key: every credential this application persists (a GitHub PAT, an
OpenHands API key) is encrypted with Fernet built from ``NEXUS_SECRET_KEY``. The
key is read from the environment and never returned, logged or put in an
exception message.

A missing key is a hard refusal, not a silent plaintext write: the caller that
wants to store a credential raises :class:`SecretsError` and the operation does
not happen. This is what keeps a misconfigured deployment from writing secrets in
the clear.
"""
from __future__ import annotations

import base64
import hashlib
import os

_ENV_SECRET = "NEXUS_SECRET_KEY"


class SecretsError(RuntimeError):
    """The encryption key is missing or unusable."""


def _fernet():
    """A Fernet built from ``NEXUS_SECRET_KEY``. Refuses to run without one."""
    raw = os.environ.get(_ENV_SECRET, "").strip()
    if not raw:
        raise SecretsError(f"{_ENV_SECRET} is not set; refusing to store a credential")
    try:
        from cryptography.fernet import Fernet
    except ImportError as e:  # pragma: no cover - cryptography is a declared dep
        raise SecretsError("cryptography is not installed") from e
    # Accept either a real Fernet key or an arbitrary passphrase (derive a key),
    # so an operator can set a long random string without running a key generator.
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError):
        digest = hashlib.sha256(raw.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str) -> bytes:
    """Encrypt a credential for storage. Raises without a key."""
    return _fernet().encrypt(plaintext.encode())


def decrypt(ciphertext: bytes | memoryview) -> str:
    """Decrypt a stored credential. Raises on a wrong key or tampering."""
    return _fernet().decrypt(bytes(ciphertext)).decode()
