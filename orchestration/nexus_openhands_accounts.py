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
from .net_guard import AddressPolicy, AddressRefused, check_url
from .nexus_projects import ProjectService, _text
from .secrets import decrypt as _decrypt_secret, encrypt as _encrypt_secret

DEFAULT_BASE_URL = "https://app.all-hands.dev"

#: The key-free shape every read returns. Kept in one place so a new column
#: cannot be returned by one query and forgotten by another. It never includes
#: ``api_key_encrypted``; ``api_key_hint`` is the masked fragment instead.
_VIEW_COLUMNS = (
    "id, principal, label, base_url, transport, default_model, identity, state, "
    "is_default, capabilities, last_checked_at, last_error, created_at, updated_at, "
    "enabled, priority, health, active_tasks, last_error_at, max_concurrent_tasks, "
    "daily_conversation_limit, api_key_hint, connection_ok, last_connection_at, "
    "error_count, api_version, compatibility, compatibility_detail, health_checked_at"
)


class ConnectionInUse(RuntimeError):
    """A connection cannot be removed while something depends on it.

    ``dependencies`` says what: tasks at work on it, projects that list it as
    an executor, stop signals still owed to its conversations.
    """

    def __init__(self, message: str, dependencies: dict):
        super().__init__(message)
        self.dependencies = dependencies


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
        #: Injectable so tests can point at the fake Cloud server. The default
        #: client connects only where the address policy allows: the URL of a
        #: stored connection was chosen by whoever added it (``net_guard``).
        self._client_factory = client_factory or (
            lambda base_url, api_key: CloudClient(
                base_url=base_url, api_key=api_key,
                address_policy=AddressPolicy.from_env()))

    def _client(self, base_url: str, api_key: str) -> CloudClient:
        return self._client_factory(base_url, api_key)

    @staticmethod
    def _url(base_url: str | None) -> str:
        """*base_url* when a connection may use it, else ``ValueError``.

        The form of the URL and the addresses it resolves to are checked here,
        when the connection is saved; the address is checked again every time a
        socket is opened (``net_guard``).
        """
        try:
            return check_url(validate_base_url(base_url or DEFAULT_BASE_URL)).rstrip("/")
        except AddressRefused as e:
            raise ValueError(f"API URL is not allowed: {e}") from e
        except CloudError as e:
            raise ValueError(f"API URL is not allowed: {e}") from e

    @staticmethod
    def _probe(client: Any) -> dict:
        """What the service says about itself: version and compatibility.

        Never raises: a service that does not publish a description of its API
        is ``unknown``, not broken - the key check is what decides whether the
        connection works.
        """
        describe = getattr(client, "compatibility", None)
        if describe is None:
            return {"api_version": None, "compatibility": "unknown",
                    "detail": "this client cannot read the service's API description"}
        try:
            return describe()
        except CloudError as e:
            return {"api_version": None, "compatibility": "unknown",
                    "detail": f"the API description could not be read: {e}"}


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
        base_url = self._url(base_url)
        if transport not in ("direct", "app"):
            raise ValueError("transport must be 'direct' or 'app'")
        if default_model is not None:
            default_model = _text(default_model, "default_model", maximum=200,
                                  required=False) or None
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
        probe = self._probe(client)
        identity = str(capabilities.get("active_agent_profile_id") or "")
        ciphertext = _encrypt_secret(api_key)
        with self.store._tx() as cur:
            cur.execute(
                "INSERT INTO nexus_openhands_accounts"
                "(principal, label, base_url, transport, default_model, api_key_encrypted, "
                " identity, capabilities, last_checked_at, api_key_hint, "
                " max_concurrent_tasks, daily_conversation_limit, health, health_checked_at, "
                " connection_ok, last_connection_at, api_version, compatibility, "
                " compatibility_detail) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s,%s,'healthy',now(),true,now(),"
                "        %s,%s,%s) "
                "ON CONFLICT (principal, label) DO UPDATE SET "
                "  base_url=EXCLUDED.base_url, transport=EXCLUDED.transport, "
                "  default_model=EXCLUDED.default_model, "
                "  api_key_encrypted=EXCLUDED.api_key_encrypted, "
                "  api_key_hint=EXCLUDED.api_key_hint, "
                "  max_concurrent_tasks=EXCLUDED.max_concurrent_tasks, "
                "  daily_conversation_limit=EXCLUDED.daily_conversation_limit, "
                "  identity=EXCLUDED.identity, capabilities=EXCLUDED.capabilities, "
                "  state='active', last_error=NULL, last_checked_at=now(), "
                "  health='healthy', health_checked_at=now(), "
                "  connection_ok=true, last_connection_at=now(), "
                "  api_version=EXCLUDED.api_version, compatibility=EXCLUDED.compatibility, "
                "  compatibility_detail=EXCLUDED.compatibility_detail, updated_at=now() "
                f"RETURNING {_VIEW_COLUMNS}",
                (principal, label, base_url, transport, default_model, ciphertext,
                 identity, _json(capabilities), _mask_key(api_key),
                 max_concurrent_tasks, daily_conversation_limit, probe.get("api_version"),
                 probe.get("compatibility") or "unknown", (probe.get("detail") or "")[:500]))
            view = self._view(cur.fetchone())
            if make_default:
                self._set_default(cur, principal, view["id"])
                view["is_default"] = True
        return view

    def edit(self, principal: str, account_id: str, *,
             label: str | None = None, base_url: str | None = None,
             transport: str | None = None, default_model: object = _UNSET,
             max_concurrent_tasks: object = _UNSET,
             daily_conversation_limit: object = _UNSET,
             api_key: str | None = None) -> dict:
        """Edit a connection: its name, where it points, what it signs with.

        Only the fields passed are changed; ``default_model`` and the limits
        accept an explicit ``None`` to clear them, which is why "unset" is a
        distinct sentinel rather than ``None``.

        A new ``base_url`` and/or a new ``api_key`` is checked before anything
        is saved, with exactly the settings the connection would have: the
        (new) key against the (new) URL. So an edit cannot leave a connection
        pointing somewhere its key is rejected, and moving a connection to
        another instance with another key is one change, not two that each
        fail on their own. The key is never returned.
        """
        if label is not None:
            label = _text(label, "label", maximum=120)
        if base_url is not None:
            base_url = self._url(base_url)
        if transport is not None and transport not in ("direct", "app"):
            raise ValueError("transport must be 'direct' or 'app'")
        if api_key is not None:
            if not isinstance(api_key, str) or not api_key.strip():
                raise ValueError("api_key must not be empty")
            api_key = api_key.strip()
        if default_model is not _UNSET and default_model is not None:
            default_model = _text(default_model, "default_model", maximum=200,
                                  required=False) or None
        if max_concurrent_tasks is not _UNSET:
            max_concurrent_tasks = _check_limit(max_concurrent_tasks,
                                                "max_concurrent_tasks")
        if daily_conversation_limit is not _UNSET:
            daily_conversation_limit = _check_limit(daily_conversation_limit,
                                                    "daily_conversation_limit")
        with self.store._tx() as cur:
            cur.execute("SELECT base_url, state FROM nexus_openhands_accounts "
                        "WHERE id=%s AND principal=%s", (account_id, principal))
            row = cur.fetchone()
            if row is None:
                raise NotFound("account not found")
            old_base, old_state = row["base_url"], row["state"]
            if label is not None:
                cur.execute("SELECT 1 FROM nexus_openhands_accounts WHERE principal=%s "
                            "AND label=%s AND id<>%s", (principal, label, account_id))
                if cur.fetchone() is not None:
                    raise ValueError(f"another connection is already named {label!r}")
        moved = base_url is not None and base_url != old_base
        checked: dict | None = None
        if moved or api_key is not None:
            target = base_url if base_url is not None else old_base
            if api_key is not None:
                key = api_key
            elif old_state != "active":
                raise ValueError(f"the connection is {old_state}; give a key to bring it back")
            else:
                key = self._decrypt(principal, account_id)
            client = self._client(target, key)
            try:
                capabilities = client.account_capabilities()
            except CloudAuthError as e:
                raise ValueError(
                    (f"the new key is rejected by {target}: {e}" if api_key is not None
                     else f"the stored key is rejected by {target}: {e}")) from e
            except (CloudUnavailable, CloudError) as e:
                raise ValueError(f"OpenHands is unavailable at {target}: {e}") from e
            checked = {"capabilities": capabilities, "probe": self._probe(client)}
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
        if api_key is not None:
            sets.append("api_key_encrypted=%s")
            params.append(_encrypt_secret(api_key))
            sets.append("api_key_hint=%s")
            params.append(_mask_key(api_key))
        if checked is not None:
            # What the check just found is what the connection is now.
            probe = checked["probe"]
            sets += ["capabilities=%s", "state='active'", "last_error=NULL",
                     "last_error_at=NULL", "last_checked_at=now()", "health='healthy'",
                     "health_checked_at=now()", "connection_ok=true",
                     "last_connection_at=now()", "api_version=%s", "compatibility=%s",
                     "compatibility_detail=%s"]
            params += [_json(checked["capabilities"]), probe.get("api_version"),
                       probe.get("compatibility") or "unknown",
                       (probe.get("detail") or "")[:500]]
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

    def _account(self, principal: str, account_id: str) -> dict:
        """One connection as its owner sees it, or ``NotFound``."""
        with self.store._tx() as cur:
            cur.execute(f"SELECT {_VIEW_COLUMNS} FROM nexus_openhands_accounts "
                        "WHERE id=%s AND principal=%s", (account_id, principal))
            row = cur.fetchone()
            if row is None:
                raise NotFound("account not found")
            return self._view(row)

    #: Read one connection (the name the HTTP layer uses).
    get = _account

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
        """Check a connection now and record what was found.

        Three questions, each answered by a real call with the connection's own
        key: does the service take the key (``state``: a rejected key makes the
        connection ``invalid``), can it be reached (``health``), and which API
        does it speak (``api_version``, ``compatibility``). A service that
        cannot be reached leaves the key's standing as it was - "unreachable"
        is not "rejected".
        """
        with self.store._tx() as cur:
            cur.execute("SELECT base_url FROM nexus_openhands_accounts "
                        "WHERE id=%s AND principal=%s", (account_id, principal))
            row = cur.fetchone()
            if row is None:
                raise NotFound("account not found")
        key = self._decrypt(principal, account_id)
        state, error, capabilities, health = "active", None, None, "healthy"
        probe: dict | None = None
        try:
            client = self._client(row["base_url"], key)
            capabilities = client.account_capabilities()
            probe = self._probe(client)
        except CloudAuthError as e:
            state, error, health = "invalid", str(e), "unavailable"
        except (CloudUnavailable, CloudError) as e:
            state, error, health = "active", f"unavailable: {e}", "unavailable"
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_openhands_accounts SET state=%s, last_error=%s, "
                "capabilities=COALESCE(%s, capabilities), last_checked_at=now(), "
                "health=%s, health_checked_at=now(), "
                "last_error_at = CASE WHEN %s::text IS NULL THEN NULL ELSE now() END, "
                "api_version = CASE WHEN %s THEN %s ELSE api_version END, "
                "compatibility = CASE WHEN %s THEN %s ELSE compatibility END, "
                "compatibility_detail = CASE WHEN %s THEN %s ELSE compatibility_detail END, "
                "updated_at=now() WHERE id=%s AND principal=%s "
                f"RETURNING {_VIEW_COLUMNS}",
                (state, error, _json(capabilities) if capabilities else None, health, error,
                 probe is not None, (probe or {}).get("api_version"),
                 probe is not None, (probe or {}).get("compatibility") or "unknown",
                 probe is not None, ((probe or {}).get("detail") or "")[:500],
                 account_id, principal))
            return self._view(cur.fetchone())

    #: The same check under the name the connection screens use.
    check = verify

    # ------------------------------------------------------------- removal
    def dependencies(self, principal: str, account_id: str) -> dict:
        """What still leans on a connection: tasks at work on it, projects
        that list it as an executor, stop signals owed to its conversations."""
        with self.store._tx() as cur:
            cur.execute("SELECT active_tasks FROM nexus_openhands_accounts "
                        "WHERE id=%s AND principal=%s", (account_id, principal))
            row = cur.fetchone()
            if row is None:
                raise NotFound("account not found")
            cur.execute("SELECT count(*) AS n FROM nexus_run_tasks "
                        "WHERE openhands_account_id=%s "
                        "AND state IN ('dispatched','running','verifying')", (account_id,))
            # The durable rows are the truth; the counter is kept for a caller
            # that counts by hand (``AccountPool.begin_task``). Either holds.
            active = max(int(cur.fetchone()["n"]), int(row["active_tasks"] or 0))
            cur.execute(
                "SELECT p.id, p.name, e.position FROM nexus_project_executors e "
                "JOIN nexus_projects p ON p.id = e.project_id "
                "WHERE e.account_id=%s ORDER BY p.name, p.id", (account_id,))
            projects = [{"id": str(r["id"]), "name": r["name"],
                         "role": "primary" if r["position"] == 0 else "fallback"}
                        for r in cur.fetchall()]
            cur.execute("SELECT count(*) AS n FROM nexus_executor_stops "
                        "WHERE openhands_account_id=%s AND state='requested'", (account_id,))
            stops = int(cur.fetchone()["n"])
        return {"active_tasks": active, "projects": projects, "pending_stops": stops}

    def usage(self, principal: str, account_id: str, *, limit: int = 50) -> list[dict]:
        """Which tasks this connection ran, newest first: the history of its
        use. Only its owner reads it; the rows are of the owner's own
        projects and of projects that list the connection."""
        with self.store._tx() as cur:
            cur.execute("SELECT 1 FROM nexus_openhands_accounts WHERE id=%s AND principal=%s",
                        (account_id, principal))
            if cur.fetchone() is None:
                raise NotFound("account not found")
            cur.execute(
                "SELECT e.id, e.run_id, e.task_id, e.project_id, p.name AS project_name, "
                "  e.task_key, e.cycle, e.attempt, e.kind, e.conversation_id, e.repository, "
                "  e.branch, e.started_at, e.ended_at, e.outcome, e.reason "
                "FROM nexus_task_executions e JOIN nexus_projects p ON p.id = e.project_id "
                "WHERE e.openhands_account_id=%s "
                "AND EXISTS (SELECT 1 FROM nexus_project_members m "
                "            WHERE m.project_id = e.project_id AND m.principal=%s) "
                "ORDER BY e.started_at DESC, e.id LIMIT %s",
                (account_id, principal, max(1, min(int(limit), 200))))
            out = []
            for row in cur.fetchall():
                item = dict(row)
                for key in ("id", "run_id", "task_id", "project_id"):
                    item[key] = str(item[key])
                out.append(item)
            return out

    def remove(self, principal: str, account_id: str, *, force: bool = False) -> dict:
        """Delete a connection and its key.

        Refused (:class:`ConnectionInUse`) while a task is at work on it, a
        project lists it as an executor, or a stop signal is still owed to one
        of its conversations - removing it then would leave work nobody can
        reach. *force* removes it anyway: the projects lose it from their
        lists, and its tasks are found missing on the next pass and started
        again elsewhere. Its place in the history of what ran where stays
        (``nexus_task_executions`` keeps the name).
        """
        deps = self.dependencies(principal, account_id)
        blocking = []
        if deps["active_tasks"]:
            blocking.append(f"{deps['active_tasks']} task(s) at work on it")
        if deps["projects"]:
            blocking.append("it is an executor of: "
                            + ", ".join(p["name"] for p in deps["projects"][:5]))
        if deps["pending_stops"]:
            blocking.append(f"{deps['pending_stops']} conversation(s) it still has to stop")
        if blocking and not force:
            raise ConnectionInUse("the connection is in use: " + "; ".join(blocking), deps)
        with self.store._tx() as cur:
            # A project whose primary this was falls back to its next executor.
            cur.execute("SELECT project_id FROM nexus_project_executors WHERE account_id=%s",
                        (account_id,))
            affected = [r["project_id"] for r in cur.fetchall()]
            cur.execute("DELETE FROM nexus_openhands_accounts WHERE id=%s AND principal=%s",
                        (account_id, principal))
            if cur.rowcount == 0:
                raise NotFound("account not found")
            for project_id in affected:
                _renumber(cur, project_id)
        return deps

    # ------------------------------------------------------------ internal
    def owner_of(self, account_id: str) -> str | None:
        """Whose connection *account_id* is. For the loop, which is nobody's
        member: it reads the row by id and acts for the owner."""
        with self.store._tx() as cur:
            cur.execute("SELECT principal FROM nexus_openhands_accounts WHERE id=%s",
                        (account_id,))
            row = cur.fetchone()
            return row["principal"] if row else None

    def execution_client_internal(self, account_id: str):
        """An execution client for a connection named by id alone.

        The loop runs tasks of every project and is not the owner of any
        connection; which connection a task may use was decided before this
        (``openhands_router``), so no principal is asked for here.
        """
        principal = self.owner_of(account_id)
        if principal is None:
            raise NotFound("account not found")
        return self.execution_client_for_account(principal, account_id)

    def reprobe_unavailable(self, *, min_age_seconds: int = 120, limit: int = 5,
                            project_id: str | None = None) -> list[dict]:
        """Ask the connections that were written off whether they are back.

        A connection becomes ``unavailable`` when a start on it fails, and
        from then on it is given no work - so nothing would ever show that it
        recovered. This asks it directly (one cheap call each, at most once
        per *min_age_seconds*) and marks it healthy again when it answers.
        With *project_id*, only that project's executors and its owner's
        connections are asked.
        """
        with self.store._tx() as cur:
            cur.execute(
                "SELECT a.id, a.principal, a.base_url FROM nexus_openhands_accounts a "
                "WHERE a.health='unavailable' AND a.state='active' AND a.enabled "
                "AND (a.health_checked_at IS NULL "
                "     OR a.health_checked_at < now() - %s * interval '1 second') "
                "AND (%s::uuid IS NULL "
                "     OR a.id IN (SELECT account_id FROM nexus_project_executors "
                "                 WHERE project_id=%s) "
                "     OR a.principal IN (SELECT principal FROM nexus_project_members "
                "                        WHERE project_id=%s AND role='owner')) "
                "ORDER BY a.health_checked_at NULLS FIRST LIMIT %s",
                (min_age_seconds, project_id, project_id, project_id, limit))
            rows = [dict(r) for r in cur.fetchall()]
        out = []
        for row in rows:
            account_id = str(row["id"])
            healthy, error = False, None
            try:
                key = self._decrypt(row["principal"], account_id)
                client = self._client(row["base_url"], key)
                healthy = bool(client.health())
                if not healthy:
                    error = "the service did not answer the health call"
            except (CloudError, ValueError, NotFound) as e:
                error = f"{type(e).__name__}: {e}"
            with self.store._tx() as cur:
                cur.execute(
                    "UPDATE nexus_openhands_accounts SET health=%s, health_checked_at=now(), "
                    "last_error = CASE WHEN %s THEN NULL ELSE COALESCE(%s, last_error) END, "
                    "last_error_at = CASE WHEN %s THEN NULL ELSE last_error_at END, "
                    "updated_at=now() WHERE id=%s",
                    ("healthy" if healthy else "unavailable", healthy,
                     (error or None) and error[:1000], healthy, account_id))
            out.append({"id": account_id, "healthy": healthy, "error": error})
        return out

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
            # The project's executor list says the same thing: binding one
            # connection makes it the primary (anything already listed stays
            # behind it as a backup); unbinding empties the list.
            if account_id is None:
                cur.execute("DELETE FROM nexus_project_executors WHERE project_id=%s",
                            (project_id,))
            else:
                cur.execute("UPDATE nexus_project_executors SET position = position + 100000 "
                            "WHERE project_id=%s", (project_id,))
                cur.execute(
                    "INSERT INTO nexus_project_executors(project_id, account_id, position, "
                    "  created_by) VALUES (%s,%s,0,%s) "
                    "ON CONFLICT (project_id, account_id) DO UPDATE SET position=0, "
                    "  enabled=true, updated_at=now()", (project_id, account_id, principal))
            _renumber(cur, project_id)
            cur.execute("SELECT id, name, openhands_account_id FROM nexus_projects WHERE id=%s",
                        (project_id,))
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
        # ``scope``: this connection reuses only the sandboxes it started.
        return build_direct_client(self.store, cloud=cloud,
                                   llm_model=cfg.get("default_model"),
                                   scope=str(account_id))

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


def _renumber(cur: Any, project_id: Any) -> None:
    """Close the gaps in a project's executor order and keep
    ``nexus_projects.openhands_account_id`` naming its primary."""
    cur.execute("SELECT account_id FROM nexus_project_executors WHERE project_id=%s "
                "ORDER BY position, created_at, account_id FOR UPDATE", (project_id,))
    ordered = [row["account_id"] for row in cur.fetchall()]
    # Out of the way first: positions are unique, and two rows cannot swap in place.
    cur.execute("UPDATE nexus_project_executors SET position = position + 100000 "
                "WHERE project_id=%s", (project_id,))
    for position, account_id in enumerate(ordered):
        cur.execute("UPDATE nexus_project_executors SET position=%s, updated_at=now() "
                    "WHERE project_id=%s AND account_id=%s",
                    (position, project_id, account_id))
    cur.execute("UPDATE nexus_projects SET openhands_account_id=%s, updated_at=now() "
                "WHERE id=%s", (ordered[0] if ordered else None, project_id))


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
