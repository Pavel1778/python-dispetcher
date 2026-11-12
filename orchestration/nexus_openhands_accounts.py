"""OpenHands multi-account: encrypted API keys, verified accounts, project binding.

An account is one row: a label, a base URL, a transport, an optional default
model and an encrypted API key. The key is encrypted with the same server-side
key as a GitHub PAT (see :mod:`orchestration.secrets`) and is **never** returned
by any method here; ``account_view`` is the only shape that leaves this module.

Security rules, each enforced in code:

* The API key is encrypted before it is written and never returned or logged.
* An account is verified against the Cloud API before it is trusted; an account
  whose key is rejected is marked ``invalid``, not silently kept.
* ``client_for_project`` returns a client for the project's bound account or
  raises; it never falls back to a different account or to the process-wide key,
  so a project cannot silently execute against the wrong identity.
* At most one account per principal is the default (a partial unique index).

Capabilities are discovered read-only and cached; they never include a secret.
"""
from __future__ import annotations

from typing import Any

from .cloud_client import (
    CloudAuthError,
    CloudClient,
    CloudError,
    CloudUnavailable,
    validate_base_url,
)
from .cloud_store import NotFound, PostgresStore
from .nexus_projects import ProjectService, _text
from .secrets import decrypt as _decrypt_secret, encrypt as _encrypt_secret

DEFAULT_BASE_URL = "https://app.all-hands.dev"


def _safe_base_url(url: str | None) -> str:
    """Validate a base URL as a client error, not a server error.

    ``validate_base_url`` enforces the SSRF policy and raises ``CloudError``,
    which the HTTP layer would report as a 502 (a server problem). A bad or
    hostile base URL is the *caller's* mistake, so it is re-raised as a
    ``ValueError`` and the route answers 400.
    """
    try:
        return validate_base_url(url or DEFAULT_BASE_URL).rstrip("/")
    except CloudError as e:
        raise ValueError(str(e)) from e


#: The key-free shape every read returns. Kept in one place so a new column
#: cannot be returned by one query and forgotten by another. It never includes
#: ``api_key_encrypted``; ``api_key_hint`` is the masked fragment instead.
_VIEW_COLUMNS = (
    "id, principal, label, base_url, transport, default_model, identity, state, "
    "is_default, capabilities, api_version, last_checked_at, last_error, created_at, "
    "updated_at, enabled, priority, health, active_tasks, last_error_at, "
    "max_concurrent_tasks, daily_conversation_limit, api_key_hint, connection_ok, "
    "last_connection_at, error_count"
)

#: A caller that wants "leave this field alone". Distinct from ``None``, which
#: means "clear it" (an operator removing a default model, say).
_UNSET = object()


def _mask_key(api_key: str) -> str:
    """A masked fragment of a key, safe to show and log.

    Never enough to reconstruct the key: the first few characters and the last
    few, with the middle replaced. A short key is masked whole, so a truncated
    key does not become legible by accident.
    """
    key = api_key.strip()
    if len(key) < 12:
        return "•" * len(key)
    return f"{key[:4]}…{key[-4:]}"


class OpenHandsAccountService:
    """Add, list, verify, rotate and bind OpenHands accounts. Never leaks a key."""

    def __init__(self, store: PostgresStore, *, client_factory=None):
        self.store = store
        #: Injectable so tests can point at the fake Cloud server.
        self._client_factory = client_factory or (
            lambda base_url, api_key: CloudClient(base_url=base_url, api_key=api_key))

    def _client(self, base_url: str, api_key: str) -> CloudClient:
        return self._client_factory(base_url, api_key)

    # ------------------------------------------------------------------- add
    def add(self, principal: str, *, label: str, api_key: str,
            base_url: str | None = None, transport: str = "direct",
            default_model: str | None = None, make_default: bool = False,
            max_concurrent_tasks: int | None = None,
            daily_conversation_limit: int | None = None) -> dict:
        """Verify the key, then store it encrypted. Returns a key-free view."""
        label = _text(label, "label", maximum=120)
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key is required")
        api_key = api_key.strip()
        base_url = _safe_base_url(base_url)
        if transport not in ("direct", "app"):
            raise ValueError("transport must be 'direct' or 'app'")
        max_concurrent_tasks = _check_limit(max_concurrent_tasks, "max_concurrent_tasks")
        daily_conversation_limit = _check_limit(daily_conversation_limit,
                                                "daily_conversation_limit")
        client = self._client(base_url, api_key)
        try:
            capabilities = client.account_capabilities()
        except CloudAuthError as e:
            raise ValueError(f"OpenHands rejected the API key: {e}") from e
        except (CloudUnavailable, CloudError) as e:
            raise ValueError(f"OpenHands is unavailable: {e}") from e
        identity = str(capabilities.get("active_agent_profile_id") or "")
        # Read-only discovery of the API version; a service that does not report
        # one is still accepted (the capability probe is what decides usability).
        # A minimal client (a test double) without the method reads as "unknown".
        version = client.api_version() if hasattr(client, "api_version") else None
        ciphertext = _encrypt_secret(api_key)
        with self.store._tx() as cur:
            cur.execute(
                "INSERT INTO nexus_openhands_accounts"
                "(principal, label, base_url, transport, default_model, api_key_encrypted, "
                " identity, capabilities, api_version, last_checked_at, api_key_hint, "
                " max_concurrent_tasks, daily_conversation_limit) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s,%s) "
                "ON CONFLICT (principal, label) DO UPDATE SET "
                "  base_url=EXCLUDED.base_url, transport=EXCLUDED.transport, "
                "  default_model=EXCLUDED.default_model, "
                "  api_key_encrypted=EXCLUDED.api_key_encrypted, "
                "  api_key_hint=EXCLUDED.api_key_hint, "
                "  max_concurrent_tasks=EXCLUDED.max_concurrent_tasks, "
                "  daily_conversation_limit=EXCLUDED.daily_conversation_limit, "
                "  identity=EXCLUDED.identity, capabilities=EXCLUDED.capabilities, "
                "  api_version=EXCLUDED.api_version, "
                "  state='active', last_error=NULL, last_checked_at=now(), updated_at=now() "
                f"RETURNING {_VIEW_COLUMNS}",
                (principal, label, base_url, transport, default_model, ciphertext,
                 identity, _json(capabilities), version, _mask_key(api_key),
                 max_concurrent_tasks, daily_conversation_limit))
            view = self._view(cur.fetchone())
            if make_default:
                self._set_default(cur, principal, view["id"])
                view["is_default"] = True
        return view

    def edit(self, principal: str, account_id: str, *,
             label: str | None = None, base_url: str | None = None,
             transport: str | None = None, default_model: object = _UNSET,
             max_concurrent_tasks: object = _UNSET,
             daily_conversation_limit: object = _UNSET) -> dict:
        """Edit an account's non-secret fields. The key is untouched here.

        Only the fields passed are changed; ``default_model`` and the limits
        accept an explicit ``None`` to clear them, which is why "unset" is a
        distinct sentinel rather than ``None``. Changing ``base_url`` re-verifies
        the stored key against the new endpoint before the change is committed,
        so an edit cannot leave an account pointing somewhere its key is rejected.
        """
        if label is not None:
            label = _text(label, "label", maximum=120)
        if base_url is not None:
            base_url = _safe_base_url(base_url)
        if transport is not None and transport not in ("direct", "app"):
            raise ValueError("transport must be 'direct' or 'app'")
        if max_concurrent_tasks is not _UNSET:
            max_concurrent_tasks = _check_limit(max_concurrent_tasks,
                                                "max_concurrent_tasks")
        if daily_conversation_limit is not _UNSET:
            daily_conversation_limit = _check_limit(daily_conversation_limit,
                                                    "daily_conversation_limit")
        with self.store._tx() as cur:
            cur.execute("SELECT base_url FROM nexus_openhands_accounts "
                        "WHERE id=%s AND principal=%s", (account_id, principal))
            row = cur.fetchone()
            if row is None:
                raise NotFound("account not found")
            old_base = row["base_url"]
        if base_url is not None and base_url != old_base:
            key = self._decrypt(principal, account_id)
            try:
                self._client(base_url, key).account_capabilities()
            except CloudAuthError as e:
                raise ValueError(f"the stored key is rejected by {base_url}: {e}") from e
            except (CloudUnavailable, CloudError) as e:
                raise ValueError(f"OpenHands is unavailable at {base_url}: {e}") from e
        sets: list[str] = []
        params: list[Any] = []
        for column, value in (("label", label), ("base_url", base_url),
                              ("transport", transport)):
            if value is not None:
                sets.append(f"{column}=%s")
                params.append(value)
        if default_model is not _UNSET:
            sets.append("default_model=%s")
            params.append(default_model)
        if max_concurrent_tasks is not _UNSET:
            sets.append("max_concurrent_tasks=%s")
            params.append(max_concurrent_tasks)
        if daily_conversation_limit is not _UNSET:
            sets.append("daily_conversation_limit=%s")
            params.append(daily_conversation_limit)
        if not sets:
            return self._account(principal, account_id)
        sets.append("updated_at=now()")
        params.extend([account_id, principal])
        with self.store._tx() as cur:
            cur.execute(
                f"UPDATE nexus_openhands_accounts SET {', '.join(sets)} "
                f"WHERE id=%s AND principal=%s RETURNING {_VIEW_COLUMNS}", params)
            row = cur.fetchone()
            if row is None:
                raise NotFound("account not found")
            return self._view(row)

    def check_connection(self, principal: str, account_id: str) -> dict:
        """Probe reachability of the account's endpoint, separately from auth.

        A connectivity check and an authorization check are different questions:
        an endpoint can be unreachable while the key is fine, and reachable while
        the key is rejected. This makes a plain request to the endpoint and
        records whether the transport worked; the authorization verdict stays
        :meth:`verify`'s job. The stored key is used but never returned.
        """
        with self.store._tx() as cur:
            cur.execute("SELECT base_url FROM nexus_openhands_accounts "
                        "WHERE id=%s AND principal=%s", (account_id, principal))
            row = cur.fetchone()
            if row is None:
                raise NotFound("account not found")
        key = self._decrypt(principal, account_id)
        probe = self._client(row["base_url"], key).ping()
        reachable = bool(probe.get("reachable"))
        error = None if reachable else (probe.get("detail") or "endpoint unreachable")
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_openhands_accounts SET connection_ok=%s, last_error=%s, "
                "last_connection_at=now(), updated_at=now() "
                f"WHERE id=%s AND principal=%s RETURNING {_VIEW_COLUMNS}",
                (reachable, (error or "")[:1000], account_id, principal))
            view = self._view(cur.fetchone())
        view["reachable"] = reachable
        view["error"] = error
        return view

    def list_accounts(self, principal: str) -> list[dict]:
        with self.store._tx() as cur:
            cur.execute(f"SELECT {_VIEW_COLUMNS} "
                        "FROM nexus_openhands_accounts WHERE principal=%s "
                        "ORDER BY is_default DESC, created_at DESC, id", (principal,))
            return [self._view(r) for r in cur.fetchall()]

    def has_any_accounts(self) -> bool:
        """True when *any* principal has at least one stored account.

        Accounts are stored per principal. A worker runs as ``machine:default``
        and owns none, while an account added in the studio belongs to
        ``user:<login>``. Asking whether the *deployment* has accounts — not
        whether one principal has — is what tells ``build_account_router`` the
        pool should be switched on: a per-principal check left the pool off on
        every deployment that has a login, so the automatic modes never ran.
        """
        with self.store._tx() as cur:
            cur.execute("SELECT 1 FROM nexus_openhands_accounts LIMIT 1")
            return cur.fetchone() is not None

    def error_history(self, principal: str, account_id: str, *,
                      limit: int = 50) -> list[dict]:
        """The account's observed failures and recoveries, newest first.

        The latest failure alone (``last_error``) says the account is broken
        now; the history says whether it is *flapping* — failing, recovering,
        failing again — which is what an operator needs to decide whether to
        disable it. Never a secret: the rows carry the failure message only.
        """
        with self.store._tx() as cur:
            cur.execute("SELECT 1 FROM nexus_openhands_accounts "
                        "WHERE id=%s AND principal=%s", (account_id, principal))
            if cur.fetchone() is None:
                raise NotFound("account not found")
            cur.execute(
                "SELECT id, kind, detail, created_at FROM nexus_openhands_account_events "
                "WHERE account_id=%s ORDER BY created_at DESC, id DESC LIMIT %s",
                (account_id, max(1, min(int(limit), 200))))
            return [{**dict(r), "id": str(r["id"])} for r in cur.fetchall()]

    def rotate(self, principal: str, account_id: str, *, api_key: str) -> dict:
        """Replace an account's key after re-verifying it."""
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key is required")
        api_key = api_key.strip()
        with self.store._tx() as cur:
            cur.execute("SELECT base_url FROM nexus_openhands_accounts "
                        "WHERE id=%s AND principal=%s", (account_id, principal))
            row = cur.fetchone()
            if row is None:
                raise NotFound("account not found")
            base_url = row["base_url"]
        try:
            capabilities = self._client(base_url, api_key).account_capabilities()
        except (CloudAuthError, CloudUnavailable, CloudError) as e:
            raise ValueError(f"new API key was rejected: {e}") from e
        ciphertext = _encrypt_secret(api_key)
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_openhands_accounts SET api_key_encrypted=%s, api_key_hint=%s, "
                "capabilities=%s, state='active', last_error=NULL, "
                "last_checked_at=now(), updated_at=now() "
                f"WHERE id=%s AND principal=%s RETURNING {_VIEW_COLUMNS}",
                (ciphertext, _mask_key(api_key), _json(capabilities),
                 account_id, principal))
            return self._view(cur.fetchone())

    def revoke(self, principal: str, account_id: str) -> dict:
        with self.store._tx() as cur:
            cur.execute("UPDATE nexus_openhands_accounts SET state='revoked', "
                        "is_default=false, updated_at=now() "
                        f"WHERE id=%s AND principal=%s RETURNING {_VIEW_COLUMNS}",
                        (account_id, principal))
            row = cur.fetchone()
            if row is None:
                raise NotFound("account not found")
            return self._view(row)

    def verify(self, principal: str, account_id: str) -> dict:
        """Re-check an account against Cloud; mark it invalid if rejected.

        The API version is read and stored alongside the capabilities, so the
        operator can see which OpenHands an account talks to. A service that
        does not report a version is not refused; the capability probe decides
        usability.
        """
        with self.store._tx() as cur:
            cur.execute("SELECT base_url FROM nexus_openhands_accounts "
                        "WHERE id=%s AND principal=%s", (account_id, principal))
            row = cur.fetchone()
            if row is None:
                raise NotFound("account not found")
        key = self._decrypt(principal, account_id)
        state, error, capabilities, version = "active", None, None, None
        client = self._client(row["base_url"], key)
        try:
            capabilities = client.account_capabilities()
            version = client.api_version()
        except CloudAuthError as e:
            state, error = "invalid", str(e)
        except (CloudUnavailable, CloudError) as e:
            state, error = "active", f"unavailable: {e}"
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_openhands_accounts SET state=%s, last_error=%s, "
                "capabilities=COALESCE(%s, capabilities), api_version=COALESCE(%s, api_version), "
                "last_checked_at=now(), "
                f"updated_at=now() WHERE id=%s AND principal=%s RETURNING {_VIEW_COLUMNS}",
                (state, error, _json(capabilities) if capabilities else None,
                 version, account_id, principal))
            return self._view(cur.fetchone())

    def rename(self, principal: str, account_id: str, *, label: str) -> dict:
        """Rename an account. The key, URL and state are untouched."""
        label = _text(label, "label", maximum=120)
        with self.store._tx() as cur:
            cur.execute("UPDATE nexus_openhands_accounts SET label=%s, updated_at=now() "
                        "WHERE id=%s AND principal=%s "
                        "RETURNING id, principal, label, base_url, transport, default_model, "
                        "  identity, state, is_default, capabilities, api_version, last_checked_at, "
                        "  last_error, created_at, updated_at, enabled, priority, health, active_tasks, last_error_at",
                        (label, account_id, principal))
            row = cur.fetchone()
            if row is None:
                raise NotFound("account not found")
            return self._view(row)

    def set_default(self, principal: str, account_id: str) -> dict:
        with self.store._tx() as cur:
            self._set_default(cur, principal, account_id)
            cur.execute(f"SELECT {_VIEW_COLUMNS} FROM nexus_openhands_accounts "
                        "WHERE id=%s", (account_id,))
            return self._view(cur.fetchone())

    @staticmethod
    def _set_default(cur: Any, principal: str, account_id: str) -> None:
        cur.execute("SELECT 1 FROM nexus_openhands_accounts WHERE id=%s AND principal=%s "
                    "AND state='active'", (account_id, principal))
        if cur.fetchone() is None:
            raise NotFound("account not found or not active")
        cur.execute("UPDATE nexus_openhands_accounts SET is_default=false "
                    "WHERE principal=%s AND is_default", (principal,))
        cur.execute("UPDATE nexus_openhands_accounts SET is_default=true, updated_at=now() "
                    "WHERE id=%s AND principal=%s", (account_id, principal))

    # --------------------------------------------------------------- binding
    def bind_project(self, principal: str, project_id: str, account_id: str | None) -> dict:
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            if account_id is not None:
                cur.execute("SELECT id FROM nexus_openhands_accounts "
                            "WHERE id=%s AND principal=%s AND state='active'",
                            (account_id, principal))
                if cur.fetchone() is None:
                    raise NotFound("account not found or not active")
            cur.execute("UPDATE nexus_projects SET openhands_account_id=%s, updated_at=now() "
                        "WHERE id=%s RETURNING id, name, openhands_account_id",
                        (account_id, project_id))
            return dict(cur.fetchone())

    def client_for_account(self, principal: str, account_id: str) -> CloudClient:
        """A Cloud client for one named account, or raise."""
        with self.store._tx() as cur:
            cur.execute("SELECT base_url, transport FROM nexus_openhands_accounts "
                        "WHERE id=%s AND principal=%s", (account_id, principal))
            row = cur.fetchone()
            if row is None:
                raise NotFound("account not found")
        key = self._decrypt(principal, account_id)
        return self._client(row["base_url"], key)

    def execution_client_for_account(self, principal: str, account_id: str):
        """A ready-to-run client for an account, honouring its own config.

        ``client_for_account`` returns the account-level Cloud API client, which
        is the right thing for capability/health calls but not what executes a
        task: a ``direct`` account must talk to its sandbox's agent-server, and a
        conversation must be started with the account's ``default_model`` (a bare
        sandbox defaults to a model whose key may be absent). Building the
        execution client in exactly one place is what keeps the diagnostics
        probe, the account pool and the worker from disagreeing about which
        transport or model an account uses.

        The key is decrypted here and used immediately; it is never logged.
        """
        from .cloud_direct import build_direct_client

        cfg = self.account_config(principal, account_id)
        key = self._decrypt(principal, account_id)
        cloud = self._client(cfg["base_url"], key)
        if cfg["transport"] == "app":
            return cloud
        return build_direct_client(self.store, cloud=cloud,
                                   llm_model=cfg.get("default_model"))

    def client_for_project(self, principal: str, project_id: str) -> CloudClient:
        """The client for a project's bound account, or raise.

        Never falls back to another account or to the process-wide key: a project
        must execute against the identity it was bound to.
        """
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("SELECT openhands_account_id FROM nexus_projects WHERE id=%s",
                        (project_id,))
            account_id = cur.fetchone()["openhands_account_id"]
        if account_id is None:
            raise NotFound("project has no OpenHands account bound")
        return self.client_for_account(principal, str(account_id))

    def account_config(self, principal: str, account_id: str) -> dict:
        """Non-secret execution config for an account (transport, base URL, model).

        The caller uses this to build the transport; the key never leaves here.
        """
        with self.store._tx() as cur:
            cur.execute("SELECT base_url, transport, default_model, state "
                        "FROM nexus_openhands_accounts WHERE id=%s AND principal=%s",
                        (account_id, principal))
            row = cur.fetchone()
            if row is None:
                raise NotFound("account not found")
            if row["state"] != "active":
                raise ValueError(f"account is {row['state']}")
            return {"base_url": row["base_url"], "transport": row["transport"],
                    "default_model": row["default_model"]}

    def _decrypt(self, principal: str, account_id: str) -> str:
        with self.store._tx() as cur:
            cur.execute("SELECT api_key_encrypted, state FROM nexus_openhands_accounts "
                        "WHERE id=%s AND principal=%s", (account_id, principal))
            row = cur.fetchone()
            if row is None:
                raise NotFound("account not found")
            if row["state"] != "active":
                raise ValueError(f"account is {row['state']}")
            return _decrypt_secret(row["api_key_encrypted"])

    @staticmethod
    def _view(row: Any) -> dict:
        """The key-free shape returned to callers. Never includes the API key."""
        out = dict(row)
        out.pop("api_key_encrypted", None)
        if out.get("id") is not None:
            out["id"] = str(out["id"])
        return out


def _check_limit(value: Any, name: str) -> int | None:
    """Validate an optional positive-integer limit. ``None`` means "no limit"."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer or null")
    return value


def _json(value: Any) -> str:
    import json

    return json.dumps(value, default=str)
