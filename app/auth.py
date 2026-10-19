"""Password hashing and signed-session primitives for the operator dashboard.

Stdlib only (no ``passlib``/``itsdangerous`` dependency) so the service keeps its
small dependency surface on Render Free.

Two things live here:

* ``hash_password`` / ``verify_password`` - scrypt password hashing with a
  per-password random salt. The stored form is self-describing
  (``scrypt$n$r$p$salt$hash``) so parameters can be raised later without
  invalidating existing hashes.
* ``SessionSigner`` - mints and verifies stateless, HMAC-signed session tokens.
  There is no server-side session table, so a restart does not log everyone out
  and no session state has to be persisted. The token carries the username and
  an expiry; tampering or expiry fails verification.

Nothing here ever logs or returns a password or the signing key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# scrypt parameters. n must be a power of two. These are the values the Python
# docs use for interactive logins and are a reasonable 2024+ default.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16

_PREFIX = "scrypt"


class AuthConfigError(RuntimeError):
    """The dashboard credentials are absent or malformed."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Hash ``password`` into a self-describing ``scrypt$...`` string."""
    if not password:
        raise AuthConfigError("password must not be empty")
    salt = salt or secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"{_PREFIX}${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a stored hash.

    Returns ``False`` (never raises) for an empty password or a malformed hash,
    so a corrupted ``DASHBOARD_PASSWORD_HASH`` cannot be distinguished from a
    wrong password by a caller.
    """
    if not password or not stored:
        return False
    try:
        prefix, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if prefix != _PREFIX:
            return False
        salt = _b64d(salt_b64)
        expected = _b64d(hash_b64)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


@dataclass(frozen=True)
class Session:
    """A verified session: the authenticated username and its expiry."""

    username: str
    expires_at: datetime


class SessionSigner:
    """Mint and verify HMAC-signed session tokens.

    Token layout: ``base64url(payload).base64url(hmac_sha256(secret, payload))``
    where ``payload`` is ``username|expiry_epoch``. Stateless and restart-safe.
    """

    def __init__(self, secret: str, *, ttl_seconds: int) -> None:
        if not secret:
            raise AuthConfigError("session secret must not be empty")
        if ttl_seconds <= 0:
            raise AuthConfigError("session ttl must be positive")
        self._secret = secret.encode("utf-8")
        self._ttl = ttl_seconds

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    def _sign(self, payload: bytes) -> bytes:
        return hmac.new(self._secret, payload, hashlib.sha256).digest()

    def mint(self, username: str, *, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        expiry = int((now + timedelta(seconds=self._ttl)).timestamp())
        payload = f"{username}|{expiry}".encode()
        return f"{_b64e(payload)}.{_b64e(self._sign(payload))}"

    def verify(self, token: str | None, *, now: datetime | None = None) -> Session | None:
        """Return the :class:`Session`, or ``None`` if invalid/expired/tampered."""
        if not token or "." not in token:
            return None
        payload_b64, sig_b64 = token.split(".", 1)
        try:
            payload = _b64d(payload_b64)
            provided_sig = _b64d(sig_b64)
        except (ValueError, TypeError):
            return None
        if not hmac.compare_digest(self._sign(payload), provided_sig):
            return None
        try:
            username, expiry_raw = payload.decode("utf-8").rsplit("|", 1)
            expiry = int(expiry_raw)
        except (ValueError, UnicodeDecodeError):
            return None
        now = now or datetime.now(timezone.utc)
        if expiry <= int(now.timestamp()):
            return None
        return Session(
            username=username, expires_at=datetime.fromtimestamp(expiry, tz=timezone.utc)
        )

    def csrf_token(self, token: str | None) -> str:
        """Derive a CSRF token bound to the current session token.

        Stateless double-submit: the token is recomputed from the session cookie
        on every state-changing request, so no extra cookie or server state is
        needed and a token from another session is rejected.
        """
        if not token:
            return ""
        return _b64e(self._sign(b"csrf:" + token.encode("utf-8")))


def generate_password_hash(password: str) -> str:
    """Convenience for operators: print a hash to paste into the dashboard env."""
    return hash_password(password)
