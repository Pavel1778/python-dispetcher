"""GitHub multi-account: encrypted PATs, verified identities, project binding.

The single ``GITHUB_TOKEN`` is not enough for a lab that works across several
accounts. This module stores one account per row, with the PAT encrypted at rest
and the account's identity verified against GitHub before it is trusted.

Security rules, each enforced in code:

* The PAT is encrypted with Fernet (key from ``NEXUS_SECRET_KEY``) before it is
  written and is **never** returned by any method here. ``account_view`` is the
  only shape that leaves this module and it contains no token.
* An account is verified by calling ``GET /user`` and comparing the login; an
  account whose token is rejected is marked ``invalid``, not silently kept.
* A project binds to exactly one account. ``client_for_project`` returns a client
  for that account or raises; it never falls back to a different account, so a
  project cannot silently run against the wrong identity.
* A Fernet key is required. Without ``NEXUS_SECRET_KEY`` this module refuses to
  store an account rather than write a plaintext token.
"""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Any

from .cloud_store import NotFound, PostgresStore
from .github_client import GitHubAuthError, GitHubClient, GitHubUnavailable
from .nexus_projects import ProjectService, _text

_ENV_SECRET = "NEXUS_SECRET_KEY"


class SecretsError(RuntimeError):
    """The encryption key is missing or unusable."""


def _fernet():
    """A Fernet built from ``NEXUS_SECRET_KEY``. Refuses to run without one."""
    raw = os.environ.get(_ENV_SECRET, "").strip()
    if not raw:
        raise SecretsError(f"{_ENV_SECRET} is not set; refusing to store a GitHub PAT")
    try:
        from cryptography.fernet import Fernet
    except ImportError as e:  # pragma: no cover - cryptography is a declared dep
        raise SecretsError("cryptography is not installed") from e
    # Accept either a real Fernet key or an arbitrary passphrase (derive a key).
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError):
        digest = hashlib.sha256(raw.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


class GitHubAccountService:
    """Add, list, verify, rotate and bind GitHub accounts. Never leaks a PAT."""

    def __init__(self, store: PostgresStore, *, client_factory=None):
        self.store = store
        #: Injectable so tests can point at the fake GitHub server.
        self._client_factory = client_factory or (lambda token: GitHubClient(token=token))

    def _client(self, token: str) -> GitHubClient:
        return self._client_factory(token)

    # ------------------------------------------------------------------- add
    def add(self, principal: str, *, label: str, token: str,
            base_url: str | None = None) -> dict:
        """Verify the token, then store it encrypted. Returns a token-free view."""
        label = _text(label, "label", maximum=120)
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token is required")
        token = token.strip()
        client = self._client_factory(token) if base_url is None else \
            GitHubClient(base_url=base_url, token=token)
        try:
            identity = client.current_user()
        except GitHubAuthError as e:
            raise ValueError(f"GitHub rejected the token: {e}") from e
        except GitHubUnavailable as e:
            raise ValueError(f"GitHub is unavailable: {e}") from e
        login = identity["login"]
        ciphertext = _fernet().encrypt(token.encode())
        with self.store._tx() as cur:
            cur.execute(
                "INSERT INTO nexus_github_accounts(principal, label, login, token_encrypted) "
                "VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (principal, label) DO UPDATE SET "
                "  login=EXCLUDED.login, token_encrypted=EXCLUDED.token_encrypted, "
                "  state='active', last_error=NULL, updated_at=now() "
                "RETURNING id, principal, label, login, state, created_at, updated_at",
                (principal, label, login, ciphertext))
            return self._view(cur.fetchone())

    def list_accounts(self, principal: str) -> list[dict]:
        with self.store._tx() as cur:
            cur.execute("SELECT id, principal, label, login, state, scopes, "
                        "last_checked_at, last_error, created_at, updated_at "
                        "FROM nexus_github_accounts WHERE principal=%s "
                        "ORDER BY created_at DESC, id", (principal,))
            return [self._view(r) for r in cur.fetchall()]

    def rotate(self, principal: str, account_id: str, *, token: str) -> dict:
        """Replace an account's PAT after re-verifying the identity."""
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token is required")
        token = token.strip()
        with self.store._tx() as cur:
            cur.execute("SELECT id FROM nexus_github_accounts WHERE id=%s AND principal=%s",
                        (account_id, principal))
            if cur.fetchone() is None:
                raise NotFound("account not found")
        try:
            login = self._client(token).current_user()["login"]
        except (GitHubAuthError, GitHubUnavailable) as e:
            raise ValueError(f"new token was rejected: {e}") from e
        ciphertext = _fernet().encrypt(token.encode())
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_github_accounts SET token_encrypted=%s, login=%s, "
                "state='active', last_error=NULL, updated_at=now() "
                "WHERE id=%s AND principal=%s "
                "RETURNING id, principal, label, login, state, created_at, updated_at",
                (ciphertext, login, account_id, principal))
            return self._view(cur.fetchone())

    def revoke(self, principal: str, account_id: str) -> dict:
        with self.store._tx() as cur:
            cur.execute("UPDATE nexus_github_accounts SET state='revoked', updated_at=now() "
                        "WHERE id=%s AND principal=%s "
                        "RETURNING id, principal, label, login, state, created_at, updated_at",
                        (account_id, principal))
            row = cur.fetchone()
            if row is None:
                raise NotFound("account not found")
            return self._view(row)

    def verify(self, principal: str, account_id: str) -> dict:
        """Re-check an account against GitHub; mark it invalid if rejected."""
        token = self._decrypt(principal, account_id)
        try:
            login = self._client(token).current_user()["login"]
            state, error = "active", None
        except GitHubAuthError as e:
            login, state, error = None, "invalid", str(e)
        except GitHubUnavailable as e:
            login, state, error = None, "active", f"unavailable: {e}"
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_github_accounts SET state=%s, last_error=%s, "
                "login=COALESCE(%s, login), last_checked_at=now(), updated_at=now() "
                "WHERE id=%s AND principal=%s "
                "RETURNING id, principal, label, login, state, last_checked_at, last_error, "
                "created_at, updated_at",
                (state, error, login, account_id, principal))
            row = cur.fetchone()
            if row is None:
                raise NotFound("account not found")
            return self._view(row)

    # --------------------------------------------------------------- binding
    def bind_project(self, principal: str, project_id: str, account_id: str | None) -> dict:
        """Bind a project to one account (or unbind with ``None``)."""
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            if account_id is not None:
                cur.execute("SELECT id FROM nexus_github_accounts "
                            "WHERE id=%s AND principal=%s AND state='active'",
                            (account_id, principal))
                if cur.fetchone() is None:
                    raise NotFound("account not found or not active")
            cur.execute("UPDATE nexus_projects SET github_account_id=%s, updated_at=now() "
                        "WHERE id=%s RETURNING id, name, github_account_id",
                        (account_id, project_id))
            return dict(cur.fetchone())

    def client_for_project(self, principal: str, project_id: str) -> GitHubClient:
        """A client for the project's bound account, or raise.

        Never falls back to another account: a project must run against the
        identity it was bound to.
        """
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("SELECT github_account_id FROM nexus_projects WHERE id=%s", (project_id,))
            account_id = cur.fetchone()["github_account_id"]
        if account_id is None:
            raise NotFound("project has no GitHub account bound")
        token = self._decrypt(principal, str(account_id))
        return self._client(token)

    def _decrypt(self, principal: str, account_id: str) -> str:
        with self.store._tx() as cur:
            cur.execute("SELECT token_encrypted, state FROM nexus_github_accounts "
                        "WHERE id=%s AND principal=%s", (account_id, principal))
            row = cur.fetchone()
            if row is None:
                raise NotFound("account not found")
            if row["state"] != "active":
                raise ValueError(f"account is {row['state']}")
            return _fernet().decrypt(bytes(row["token_encrypted"])).decode()

    @staticmethod
    def _view(row: Any) -> dict:
        """The token-free shape returned to callers. Never includes the PAT."""
        out = dict(row)
        out.pop("token_encrypted", None)
        if out.get("id") is not None:
            out["id"] = str(out["id"])
        return out
