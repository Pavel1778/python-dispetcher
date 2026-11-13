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

* **A session lasts while it is used.** It ends ``AUTH_SESSION_TTL_SECONDS``
  after the last request made with it (30 days by default) and, used or not,
  ``AUTH_SESSION_MAX_SECONDS`` after the login (90 days). Each use moves the
  expiry forward, and the browser's cookie is sent again with the new lifetime
  - a session the server still honours is of no use once the browser has
  dropped its cookie. The limits in force *now* decide: lowering one ends the
  sessions that are past it at their next request, not weeks later.

  Until 2026-11-12 a session lasted a fixed twelve hours from the login,
  whether it was used or not, and the limit could not be set: an owner who
  opened the studio once a day signed in every day, and one who worked for
  thirteen hours was signed out in the middle of it.

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

#: Session lifetime: how long a login lasts without being used, and how long
#: it lasts at all. The operator sets them with ``AUTH_SESSION_TTL_SECONDS`` and
#: ``AUTH_SESSION_MAX_SECONDS``; see the module docstring.
SESSION_TTL_SECONDS = 30 * 24 * 3600
SESSION_MAX_SECONDS = 90 * 24 * 3600
#: What the operator may set them to: five minutes to 400 days (no browser
#: keeps a cookie for longer than that).
SESSION_SECONDS_FLOOR = 300
SESSION_SECONDS_CEILING = 400 * 24 * 3600
#: A use rewrites the expiry only when that moves it by at least this much (a
#: tenth of the lifetime, when that is less; ``AuthService.renew_step``). A
#: session measured in days gains nothing from being rewritten, cookie and
#: all, on every request of a page that polls.
SESSION_RENEW_STEP_SECONDS = 3600

#: The lockout policy.
MAX_LOGIN_FAILURES = 5
LOCKOUT_SECONDS = 900


class AuthError(RuntimeError):
    """Raised for an authentication failure that must not leak its reason."""


class LoginLocked(AuthError):
    """Raised when the (username, IP) pair is temporarily locked.

    Distinct from a wrong password so the operator sees "too many attempts"
    instead of "wrong login or password", which sent them looking at their
    credentials. It reveals nothing about whether the account exists: the lock
    key is derived from the submitted username and the client IP.
    """

    def __init__(self, retry_after: int):
        self.retry_after = max(0, int(retry_after))
        super().__init__(f"too many failed attempts; retry in {self.retry_after}s")


#: Upper bound on the iteration count read from a stored hash. A corrupt or
#: hostile value must not turn a login into an unbounded CPU burn.
MAX_PBKDF2_ITERATIONS = 10_000_000


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


def _without_whitespace(value: str) -> str:
    """Remove every whitespace character from *value*.

    The hash format has no meaningful whitespace, but a terminal wraps a long
    line and a wrapped paste inserts a newline in the middle; ``.env`` files and
    Windows clipboards add a trailing one. Whitespace in a stored hash is
    therefore always an accident. Only the *stored* value is normalised here —
    the password is never touched, and the salt and digest are still fully
    verified, so this cannot weaken authentication.
    """
    return "".join(value.split())


def parse_hash(stored: str | None) -> tuple[bytes, bytes, int] | None:
    """Return ``(salt, expected_digest, iterations)`` for *stored*, or ``None``.

    Accepts the format :func:`hash_password` produces, ignoring whitespace.
    ``None`` means the value is unusable, which is what a caller needs to tell a
    misconfigured ``AUTH_PASSWORD_HASH`` apart from a genuinely wrong password.
    """
    if not stored or not isinstance(stored, str):
        return None
    parts = _without_whitespace(stored).split("$")
    if len(parts) != 4:
        return None
    algo, iters, salt_b64, hash_b64 = parts
    if algo != PBKDF2_ALGO:
        return None
    try:
        iterations = int(iters)
    except (TypeError, ValueError):
        return None
    if iterations < 1 or iterations > MAX_PBKDF2_ITERATIONS:
        return None
    try:
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
    except (ValueError, TypeError):
        return None
    if not salt or not expected:
        return None
    return salt, expected, iterations


def password_hash_problem(stored: str | None) -> str | None:
    """Describe why *stored* is not a usable hash, or ``None`` if it is fine.

    The text names the variable, the problem and the fix, but never the value, so
    it is safe to log and to show on the login page. A value the generator
    produced is always accepted; anything else is explained specifically, because
    "may be the raw password" sent an operator to regenerate a hash that was
    already correct when the real problem was a shell that ate the ``$``.
    """
    if not stored:
        return "AUTH_PASSWORD_HASH is empty"
    if not isinstance(stored, str):
        return "AUTH_PASSWORD_HASH is not a string"
    if parse_hash(stored) is not None:
        return None

    parts = _without_whitespace(stored).split("$")
    if len(parts) == 1:
        if "pbkdf2" in parts[0].lower():
            return ("AUTH_PASSWORD_HASH lost its '$' separators, which a shell "
                    "consumes; export it in single quotes. Expected "
                    "pbkdf2_sha256$<iterations>$<salt>$<digest>")
        return ("AUTH_PASSWORD_HASH does not look like a hash; it may be the raw "
                "password. Generate one with: "
                "python -m orchestration.web_auth hash")
    if len(parts) != 4:
        return (f"AUTH_PASSWORD_HASH has {len(parts) - 1} '$' separators, expected "
                "3 (pbkdf2_sha256$<iterations>$<salt>$<digest>); a shell or a "
                "paste removed some")

    algo, iters, salt_b64, hash_b64 = parts
    if algo != PBKDF2_ALGO:
        if algo[:1] in ("'", '"') or algo.endswith(("'", '"')):
            return ("AUTH_PASSWORD_HASH is wrapped in quotes; paste the value "
                    "without them")
        return (f"AUTH_PASSWORD_HASH uses an unsupported algorithm ({algo!r}), "
                f"expected {PBKDF2_ALGO!r}; only hashes from "
                "`python -m orchestration.web_auth hash` are accepted")
    try:
        iterations = int(iters)
    except (TypeError, ValueError):
        return "AUTH_PASSWORD_HASH has a non-numeric iteration count"
    if iterations < 1:
        return "AUTH_PASSWORD_HASH has an iteration count below 1"
    if iterations > MAX_PBKDF2_ITERATIONS:
        return "AUTH_PASSWORD_HASH has an implausibly high iteration count"
    for label, segment in (("salt", salt_b64), ("digest", hash_b64)):
        if "-" in segment or "_" in segment:
            return (f"AUTH_PASSWORD_HASH {label} uses the URL-safe base64 alphabet "
                    "(- and _); this format uses standard base64 (+ and /)")
        if len(segment) % 4 != 0:
            return (f"AUTH_PASSWORD_HASH {label} is missing its base64 padding "
                    "(=); the value was truncated")
        try:
            decoded = base64.b64decode(segment, validate=True)
        except (ValueError, TypeError):
            return (f"AUTH_PASSWORD_HASH {label} is not valid base64; the value "
                    "was truncated or altered in transit")
        if not decoded:
            return f"AUTH_PASSWORD_HASH has an empty {label}"
    return "AUTH_PASSWORD_HASH does not match the expected format"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of *password* against a stored hash.

    Returns ``False`` for any malformed value instead of raising, so a corrupt
    setting cannot turn into an authentication bypass or a 500 on the login
    page. The iteration count is validated before use: a hostile or corrupt
    value cannot force an unbounded amount of work.
    """
    parsed = parse_hash(stored)
    if parsed is None:
        return False
    salt, expected, iterations = parsed
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(digest, expected)


#: A fixed hash used to spend the same time on an unknown user as on a known
#: one, so response timing does not reveal whether the account exists.
_DUMMY_HASH = hash_password("dummy-password-for-timing")


def _burn_time(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


def session_seconds(name: str, default: int) -> int:
    """A session limit from the environment variable *name*, or *default*.

    A value that is not a number is reported and the default is used; one
    outside ``SESSION_SECONDS_FLOOR`` .. ``SESSION_SECONDS_CEILING`` is brought
    into that range. Neither a typo nor a zero may leave the owner unable to
    stay signed in for a minute, or signed in for ever. The value itself is not
    logged when it cannot be read: what was pasted there may be something else.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s is not a whole number of seconds; using the default, %s",
                    name, default)
        return default
    limited = min(SESSION_SECONDS_CEILING, max(SESSION_SECONDS_FLOOR, value))
    if limited != value:
        log.warning("%s=%s is outside %s..%s seconds; using %s", name, value,
                    SESSION_SECONDS_FLOOR, SESSION_SECONDS_CEILING, limited)
    return limited


@dataclass(frozen=True)
class Session:
    token: str
    csrf_token: str
    username: str
    #: Set when resolving the session has just moved its expiry: the lifetime,
    #: in seconds, the browser's cookie is to be given again. ``None`` when
    #: there is nothing to tell the browser.
    cookie_ttl: int | None = None


class AuthService:
    """Login, session lifecycle and CSRF, backed by the store's SQL methods."""

    def __init__(
        self,
        store: Any,
        *,
        username: str | None = None,
        password_hash: str | None = None,
        session_secret: str | None = None,
        session_ttl: int | None = None,
        session_max: int | None = None,
        max_failures: int = MAX_LOGIN_FAILURES,
        lockout_seconds: int = LOCKOUT_SECONDS,
    ):
        self.store = store
        # The login form strips the submitted username, so the configured one is
        # stripped too: a stray space in the Render dashboard value would
        # otherwise make every login fail with no visible cause.
        username_env = username if username is not None else os.environ.get("AUTH_USERNAME")
        self.username = username_env.strip() if isinstance(username_env, str) else username_env
        hash_env = (
            password_hash if password_hash is not None
            else os.environ.get("AUTH_PASSWORD_HASH")
        )
        self.password_hash = hash_env.strip() if isinstance(hash_env, str) else hash_env
        secret_env = (
            session_secret if session_secret is not None
            else os.environ.get("AUTH_SESSION_SECRET")
        )
        self.session_secret = secret_env.strip() if isinstance(secret_env, str) else secret_env
        #: The longest a session lasts, used or not, from the login.
        self.session_max = (int(session_max) if session_max is not None
                            else session_seconds("AUTH_SESSION_MAX_SECONDS", SESSION_MAX_SECONDS))
        ttl = (int(session_ttl) if session_ttl is not None
               else session_seconds("AUTH_SESSION_TTL_SECONDS", SESSION_TTL_SECONDS))
        #: How long a session lasts without being used. It cannot stay unused
        #: for longer than it may live at all.
        self.session_ttl = min(ttl, self.session_max)
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds

    # -------------------------------------------------------------- config
    @property
    def configured(self) -> bool:
        """True when login can actually succeed.

        A non-empty but unusable ``AUTH_PASSWORD_HASH`` (the raw password, a
        truncated paste, a hash from another tool) does not count: that state
        rejected every login with "wrong credentials", which hid the real
        problem. ``config_errors`` explains it.
        """
        return not self.config_errors()

    def config_errors(self) -> list[str]:
        """Reasons login cannot work. Never contains a secret value."""
        errors: list[str] = []
        if not self.username:
            errors.append("AUTH_USERNAME is not set")
        problem = password_hash_problem(self.password_hash)
        if problem:
            errors.append(problem)
        if not self.session_secret:
            errors.append("AUTH_SESSION_SECRET is not set")
        return errors

    def missing_settings(self) -> list[str]:
        """Names of the settings that are absent or unusable (no values)."""
        names: list[str] = []
        if not self.username:
            names.append("AUTH_USERNAME")
        if password_hash_problem(self.password_hash):
            names.append("AUTH_PASSWORD_HASH")
        if not self.session_secret:
            names.append("AUTH_SESSION_SECRET")
        return names

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
        retry_after = self.store.login_locked_for(key)
        if retry_after > 0:
            log.warning("login blocked by rate limit (retry in %ss)", retry_after)
            raise LoginLocked(retry_after)
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
    @property
    def renew_step(self) -> int:
        """How much lifetime a use has to add before the session is extended."""
        return max(1, min(SESSION_RENEW_STEP_SECONDS, self.session_ttl // 10))

    def resolve(self, token: str | None) -> Session | None:
        """Return the live session for *token*, or ``None`` if absent/expired.

        Resolving a session is using it: its expiry moves forward (see the
        module docstring). When it has moved, ``cookie_ttl`` of the result is
        the lifetime the browser's cookie has to be given again; the caller
        that answers the request sends it.
        """
        if not token or not self.session_secret:
            return None
        row = self.store.get_session(
            self._token_hash(token), ttl_seconds=self.session_ttl,
            max_seconds=self.session_max, renew_step_seconds=self.renew_step)
        if row is None:
            return None
        cookie_ttl = None
        if row.get("renewed"):
            cookie_ttl = max(1, int(row.get("seconds_left") or 0))
        return Session(token=token, csrf_token=row["csrf_token"], username=row["username"],
                       cookie_ttl=cookie_ttl)

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
    """Generate and verify the secrets the deployment needs.

    Usage::

        python -m orchestration.web_auth hash             # prompt, print the hash
        python -m orchestration.web_auth secret           # print a session secret
        python -m orchestration.web_auth check            # verify $AUTH_PASSWORD_HASH

    ``hash`` reads the password from a prompt (never from argv), so it does not
    land in the shell history. ``check`` reads the *stored* value from
    ``AUTH_PASSWORD_HASH`` (or stdin) and prints only a verdict, never the value;
    with ``--password`` it also confirms that a password matches.
    """
    import getpass
    import sys

    usage = ("usage: python -m orchestration.web_auth {hash|secret|check}\n"
             "  hash             prompt for a password and print its hash\n"
             "  secret           print a new AUTH_SESSION_SECRET\n"
             "  check [--stdin]  verify AUTH_PASSWORD_HASH (or a piped value)\n"
             "                   add --password to confirm a password matches")
    if len(sys.argv) < 2 or sys.argv[1] not in ("hash", "secret", "check"):
        print(usage, file=sys.stderr)
        return 2
    command = sys.argv[1]

    if command == "secret":
        print(secrets.token_urlsafe(48))
        return 0

    if command == "check":
        return _check_cli(sys.argv[2:])

    # The password is typed at a prompt, never passed as an argument, so it does
    # not reach the shell history or the process list. That requires a terminal;
    # without one getpass would block on /dev/tty, which looks like a hang.
    if not sys.stdin.isatty():
        print(
            "error: `hash` needs an interactive terminal to read the password.\n"
            "Run it directly in a terminal (not through a pipe or a script).",
            file=sys.stderr,
        )
        return 2
    pw = getpass.getpass("password: ")
    again = getpass.getpass("repeat:   ")
    if pw != again:
        print("passwords do not match", file=sys.stderr)
        return 1
    if len(pw) < 12:
        print("choose a password of at least 12 characters", file=sys.stderr)
        return 1
    value = hash_password(pw)
    print(value)
    print(file=sys.stderr)
    # Single quotes, because the value contains '$' and a shell would expand it
    # inside double quotes or a heredoc. This line is safe to paste into a shell.
    print("paste into Render as AUTH_PASSWORD_HASH; for a local shell use:",
          file=sys.stderr)
    print(f"  export AUTH_PASSWORD_HASH='{value}'", file=sys.stderr)
    return 0


def _check_cli(argv: list[str]) -> int:
    """Verify a stored hash without printing it. Returns 0 when it is usable.

    The value comes from ``AUTH_PASSWORD_HASH`` by default. ``--stdin`` reads it
    from stdin instead, which matters when that variable is already exported:
    otherwise a piped value would be silently ignored and the *ambient* one
    verified, telling the operator their new hash is fine when the old bad one is
    what the server would read.
    """
    import sys

    want_password = "--password" in argv
    from_stdin = "--stdin" in argv
    stored = None if from_stdin else os.environ.get("AUTH_PASSWORD_HASH")
    source = "AUTH_PASSWORD_HASH"
    if not stored:
        if sys.stdin.isatty():
            print("no value: set AUTH_PASSWORD_HASH or pipe one in with --stdin",
                  file=sys.stderr)
            return 2
        stored = sys.stdin.read()
        source = "stdin"

    problem = password_hash_problem(stored)
    if problem:
        print(f"INVALID ({source}): {problem}", file=sys.stderr)
        return 1
    salt, expected, iterations = parse_hash(stored)
    print(f"OK: {source} is a usable hash "
          f"({PBKDF2_ALGO}, {iterations} iterations, "
          f"{len(salt)}-byte salt, {len(expected)}-byte digest)")

    if want_password:
        import getpass
        if not sys.stdin.isatty():
            print("--password needs an interactive terminal", file=sys.stderr)
            return 2
        pw = getpass.getpass("password to confirm: ")
        if verify_password(pw, stored):
            print("OK: the password matches this hash")
            return 0
        print("MISMATCH: the password does not match this hash", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())

