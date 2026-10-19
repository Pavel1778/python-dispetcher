"""Owner login, server-side sessions and CSRF for the web control panel.

Design choices, and why
-----------------------
* **Password hashing** uses :func:`hashlib.pbkdf2_hmac` with a per-password
  random salt and a high iteration count. It is in the standard library, so no
  new dependency is needed, and it is a deliberately slow KDF suitable for
  passwords. The stored format is ``pbkdf2_sha256$<iters>$<salt>$<hash>`` so the
  parameters travel with the hash and can be raised later without breaking old
  values.

* **Sessions are server-side**, in PostgreSQL, not in a signed cookie. A signed
  cookie cannot be revoked before it expires without extra state; a row can be
  deleted, which makes logout and "revoke all" immediate. Only
  ``HMAC-SHA256(AUTH_SESSION_SECRET, token)`` is stored, so a database dump does
  not yield usable cookies.

* **CSRF** is double-submit: a random token is stored with the session and must
  be echoed in the ``X-CSRF-Token`` header on state-changing requests. An
  attacker's page cannot read the token (it is not exposed to script from
  another origin), so it cannot forge the header. ``SameSite=Strict`` on the
  cookie is the second layer.

* **Login rate limiting** is per (username, client IP), stored as a hash so the
  table holds no readable identifiers. Failures lock for a cooldown after a
  threshold.

* **No user enumeration**: a wrong username and a wrong password produce the
  same message and the same timing (a dummy hash is verified when the user is
  unknown, so the response time does not leak whether the account exists).

Nothing here logs or returns a password, token, hash or secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("web_auth")

#: Session cookie name. HttpOnly + SameSite=Strict + Secure (on HTTPS).
SESSION_COOKIE = "dispatcher_session"
#: Header that must echo the per-session CSRF token on writes.
CSRF_HEADER = "X-CSRF-Token"

#: PBKDF2 parameters. 600k is the OWASP 2023 recommendation for PBKDF2-HMAC-SHA256.
PBKDF2_ITERATIONS = 600_000
PBKDF2_ALGO = "pbkdf2_sha256"
_SALT_BYTES = 16

#: Default session lifetime and the lockout policy.
SESSION_TTL_SECONDS = 12 * 3600
MAX_LOGIN_FAILURES = 5
LOCKOUT_SECONDS = 900


class AuthError(RuntimeError):
    """Raised for an authentication failure that must not leak its reason."""


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Return a storable hash of *password* (salt and params included)."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "$".join(
        [
            PBKDF2_ALGO,
            str(iterations),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of *password* against a stored hash.

    Returns ``False`` for any malformed value instead of raising, so a corrupt
    setting cannot turn into an authentication bypass.
    """
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
        if algo != PBKDF2_ALGO:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        iterations = int(iters)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(digest, expected)


#: A fixed hash used to spend the same time on an unknown user as on a known
#: one, so response timing does not reveal whether the account exists.
_DUMMY_HASH = hash_password("dummy-password-for-timing")


def _burn_time(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


@dataclass(frozen=True)
class Session:
    token: str
    csrf_token: str
    username: str


class AuthService:
    """Login, session lifecycle and CSRF, backed by the store's SQL methods."""

    def __init__(
        self,
        store: Any,
        *,
        username: str | None = None,
        password_hash: str | None = None,
        session_secret: str | None = None,
        session_ttl: int = SESSION_TTL_SECONDS,
        max_failures: int = MAX_LOGIN_FAILURES,
        lockout_seconds: int = LOCKOUT_SECONDS,
    ):
        self.store = store
        self.username = username if username is not None else os.environ.get("AUTH_USERNAME")
        self.password_hash = (
            password_hash if password_hash is not None
            else os.environ.get("AUTH_PASSWORD_HASH")
        )
        self.session_secret = (
            session_secret if session_secret is not None
            else os.environ.get("AUTH_SESSION_SECRET")
        )
        self.session_ttl = session_ttl
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds

    # -------------------------------------------------------------- config
    @property
    def configured(self) -> bool:
        """True when login can work at all (all three settings present)."""
        return bool(self.username and self.password_hash and self.session_secret)

    def missing_settings(self) -> list[str]:
        missing = []
        if not self.username:
            missing.append("AUTH_USERNAME")
        if not self.password_hash:
            missing.append("AUTH_PASSWORD_HASH")
        if not self.session_secret:
            missing.append("AUTH_SESSION_SECRET")
        return missing

    # --------------------------------------------------------------- login
    def login(self, username: str, password: str, *, client_ip: str) -> Session | None:
        """Verify credentials and open a session, or return ``None``.

        Applies the failure lockout first, then verifies in constant time. The
        caller must not distinguish "unknown user" from "bad password": both
        return ``None``.
        """
        if not self.configured:
            raise AuthError("login is not configured")
        key = self._attempt_key(username, client_ip)
        if self.store.login_locked(key):
            log.warning("login blocked by rate limit")
            return None
        # Constant-time-ish comparison: always verify *something*.
        if username == self.username:
            ok = verify_password(password, self.password_hash)
        else:
            _burn_time(password)
            ok = False
        if not ok:
            self.store.record_login_failure(
                key, max_failures=self.max_failures, lockout_seconds=self.lockout_seconds
            )
            return None
        self.store.clear_login_failures(key)
        return self._open_session(username)

    def _open_session(self, username: str) -> Session:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        self.store.create_session(
            token_hash=self._token_hash(token),
            username=username,
            csrf_token=csrf,
            ttl_seconds=self.session_ttl,
        )
        return Session(token=token, csrf_token=csrf, username=username)

    # ------------------------------------------------------------- session
    def resolve(self, token: str | None) -> Session | None:
        """Return the live session for *token*, or ``None`` if absent/expired."""
        if not token or not self.session_secret:
            return None
        row = self.store.get_session(self._token_hash(token))
        if row is None:
            return None
        return Session(token=token, csrf_token=row["csrf_token"], username=row["username"])

    def logout(self, token: str | None) -> None:
        """Revoke a session. Idempotent; safe to call with a stale token."""
        if token:
            self.store.delete_session(self._token_hash(token))

    def csrf_ok(self, session: Session, provided: str | None) -> bool:
        """Constant-time CSRF check for a state-changing request."""
        if not provided:
            return False
        return hmac.compare_digest(provided, session.csrf_token)

    # -------------------------------------------------------------- crypto
    def _token_hash(self, token: str) -> str:
        return hmac.new(
            self.session_secret.encode(), token.encode(), hashlib.sha256
        ).hexdigest()

    def _attempt_key(self, username: str, client_ip: str) -> str:
        return hmac.new(
            self.session_secret.encode(),
            f"{username}\x00{client_ip}".encode(),
            hashlib.sha256,
        ).hexdigest()


def parse_cookie(header: str | None, name: str) -> str | None:
    """Extract one cookie value from a ``Cookie`` header, or ``None``."""
    if not header:
        return None
    for part in header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


def session_cookie_header(
    token: str, *, ttl: int = SESSION_TTL_SECONDS, secure: bool = True
) -> str:
    """Build the ``Set-Cookie`` value with the required attributes."""
    attrs = [
        f"{SESSION_COOKIE}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        f"Max-Age={ttl}",
    ]
    if secure:
        attrs.append("Secure")
    return "; ".join(attrs)


def cleared_cookie_header(*, secure: bool = True) -> str:
    """Build a ``Set-Cookie`` that removes the session cookie."""
    attrs = [
        f"{SESSION_COOKIE}=",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        "Max-Age=0",
    ]
    if secure:
        attrs.append("Secure")
    return "; ".join(attrs)


def _now() -> float:
    return time.time()


def _cli() -> int:
    """Generate the secrets the deployment needs, without printing the password.

    Usage::

        python -m orchestration.web_auth hash           # prompts, prints the hash
        python -m orchestration.web_auth secret         # prints a session secret

    The password is read from a prompt (never from argv), so it does not land in
    the shell history.
    """
    import getpass
    import sys

    if len(sys.argv) != 2 or sys.argv[1] not in ("hash", "secret"):
        print("usage: python -m orchestration.web_auth {hash|secret}", file=sys.stderr)
        return 2
    if sys.argv[1] == "secret":
        print(secrets.token_urlsafe(48))
        return 0
    pw = getpass.getpass("password: ")
    again = getpass.getpass("repeat:   ")
    if pw != again:
        print("passwords do not match", file=sys.stderr)
        return 1
    if len(pw) < 12:
        print("choose a password of at least 12 characters", file=sys.stderr)
        return 1
    print(hash_password(pw))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())

