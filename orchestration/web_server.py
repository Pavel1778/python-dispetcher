"""Web control panel for the Sr/Jr dispatcher (stdlib-only HTTP server).

A single owner logs in with a username and password; every page and API is
protected by a server-side session (see :mod:`orchestration.web_auth`). The user
submits a task, the server creates a workflow and starts a real Sr conversation
on OpenHands Cloud, and a background loop drives both sides:

* :class:`~orchestration.sr_runtime.SrRuntime` services the Sr's client-tool
  calls, which create and manage Jr tasks.
* :class:`~orchestration.cloud_dispatcher.CloudDispatcher` runs the Jr tasks as
  their own Cloud conversations.
* :class:`~orchestration.workflow_verifier.WorkflowVerifier` writes the only
  terminal workflow states.

Everything is durable in PostgreSQL, so a restart resumes the same workflows.
The Cloud API key and the DSN are read from the environment and never sent to
the browser.

Run::

    SUPABASE_DB_URL="postgresql://..." OPENHANDS_API_KEY="..." \
    AUTH_USERNAME="owner" AUTH_PASSWORD_HASH="pbkdf2_sha256$..." \
    AUTH_SESSION_SECRET="..." \
        python -m orchestration.web_server --port 8000

Public endpoints (no session):
    GET  /login                  the login page
    POST /login                  authenticate (rate limited)
    GET  /healthz                minimal liveness probe for Render
    GET  /static/app.css         stylesheet

Protected endpoints (session cookie, or Bearer WEB_AUTH_TOKEN for API writes):
    GET  /                       the dashboard
    POST /logout                 revoke the session
    GET  /api/session            current user + CSRF token
    GET  /api/dashboard          real counts + store/Cloud health
    GET  /api/config             whether a Cloud key is configured
    GET  /api/repositories       the repository allowlist
    GET  /api/github/repositories  preview repos discoverable via GITHUB_TOKEN
    POST /api/repositories       add/update a repository        (CSRF)
    POST /api/repositories/sync  import GitHub repos into the allowlist (CSRF)
    POST /api/repositories/remove  drop one repository from the allowlist (CSRF)
    POST /api/workflows          {goal, repository?, branch?}   (CSRF)
    GET  /api/workflows          list workflows
    GET  /api/workflows/<id>     one workflow with tasks, events, tool calls
    POST /api/workflows/<id>/message   message to Sr             (CSRF)
    GET  /api/tasks              task queue with state for filters
    GET  /api/system             safe diagnostics
    GET  /api/capabilities       account-level Cloud capabilities (read-only)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .cloud_client import CloudClient, CloudError
from .cloud_dispatcher import CloudDispatcher
from .cloud_store import (
    InvalidTransition,
    NotFound,
    PostgresStore,
    StoreError,
)
from .github_client import GitHubAuthError, GitHubClient, GitHubError, GitHubUnavailable
from .sr_runtime import SrRuntime
from .sr_tools import SrTools
from .web_auth import (
    CSRF_HEADER,
    SESSION_COOKIE,
    AuthError,
    AuthService,
    LoginLocked,
    cleared_cookie_header,
    parse_cookie,
    session_cookie_header,
)
from .web_ui import APP_CSS, APP_HTML, APP_JS, FAVICON_SVG
from .workflow_verifier import WorkflowVerifier

log = logging.getLogger("web_server")


def _version() -> str:
    """The package version, or ``unknown`` if it cannot be read."""
    try:
        from importlib.metadata import version
        return version("python-dispetcher")
    except Exception:  # noqa: BLE001 - diagnostics must not fail
        return "dev"

#: Minimal liveness page. Reveals nothing about configuration.
HEALTHZ = {"status": "ok"}

LOGIN_PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход — Диспетчер OpenHands</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="stylesheet" href="/static/app.css">
</head>
<body class="auth-body">
<main class="auth-card">
  <div class="auth-brand">
    <span class="dot" aria-hidden="true"></span>
    <h1>Диспетчер OpenHands</h1>
  </div>
  <p class="muted">Войдите, чтобы отправлять задачи в OpenHands Cloud.</p>
  <form method="post" action="/login" id="loginform" autocomplete="on">
    <label for="username">Логин</label>
    <input id="username" name="username" type="text" required
           autocomplete="username" autocapitalize="none" spellcheck="false"
           aria-describedby="loginerr">
    <label for="password">Пароль</label>
    <input id="password" name="password" type="password" required
           autocomplete="current-password" aria-describedby="loginerr">
    <p class="err" id="loginerr" role="alert">__ERROR__</p>
    <button type="submit" id="loginbtn">Войти</button>
  </form>
  <p class="muted small">Доступ только для владельца. Публичной регистрации нет.</p>
</main>
</body>
</html>
"""


class App:
    """Wires the store, Cloud client, Sr runtime and Jr dispatcher together."""

    def __init__(self, dsn: str, *, worker_id: str = "web", poll_interval: float = 5.0,
                 token: str | None = None, agent_profile_id: str | None = None,
                 plugins: list[dict[str, Any]] | None = None,
                 auth: AuthService | None = None):
        self.store = PostgresStore(dsn)
        self.client = CloudClient()
        # Repository discovery for the allowlist. Reads GITHUB_TOKEN; the value
        # is never exposed through the API or the UI, only its presence.
        self.github = GitHubClient()
        # Bearer token for the machine API. Without one, only read-only requests
        # are served, so a misconfigured ``--host 0.0.0.0`` cannot be used by a
        # stranger to start Cloud conversations. ``None`` means "not set".
        self.token = token if token is not None else os.environ.get("WEB_AUTH_TOKEN")
        # Browser login. Independent of the bearer token: a session authenticates
        # the human, the token authenticates a machine client. Neither weakens
        # the other; both are required to be present for their own path.
        self.auth = auth or AuthService(self.store)
        # Optional account-level selection, read from the environment. Unset
        # means "inherit the account's own active profile and MCP/skills".
        profile = agent_profile_id or os.environ.get("OPENHANDS_AGENT_PROFILE_ID")
        self.dispatcher = CloudDispatcher(
            self.store, self.client, worker_id=worker_id,
            start_poll_interval=2.0, start_poll_attempts=40,
        )
        self.runtime = SrRuntime(
            self.store, self.client, SrTools(self.store, self.dispatcher),
            worker_id=worker_id, start_poll_interval=2.0, start_poll_attempts=40,
            agent_profile_id=profile, plugins=plugins,
        )
        self.verifier = WorkflowVerifier(self.store, self.client)
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._loop: threading.Thread | None = None
        self.last_tick: dict[str, Any] = {}

    # ---------------------------------------------------------------- loop
    def start(self) -> None:
        self._loop = threading.Thread(target=self._run, daemon=True)
        self._loop.start()

    def stop(self) -> None:
        self._stop.set()
        if self._loop:
            self._loop.join(timeout=5)
        self.store.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                sr = self.runtime.tick().as_dict()
                jr = self.dispatcher.tick().as_dict()
                vf = self.verifier.tick().as_dict()
                self.last_tick = {"at": time.time(), "sr": sr, "jr": jr, "verify": vf}
            except CloudError as e:
                self.last_tick = {"at": time.time(), "cloud_error": str(e)}
            except Exception as e:  # noqa: BLE001 - the loop must survive
                log.exception("tick failed")
                self.last_tick = {"at": time.time(), "error": f"{type(e).__name__}: {e}"}
            self._stop.wait(self.poll_interval)

    # ------------------------------------------------------------- actions
    def start_workflow(self, goal: str, repository: str | None = None,
                       branch: str | None = None,
                       request_id: str | None = None) -> dict[str, Any]:
        """Create a workflow and start its Sr conversation.

        ``request_id`` makes this idempotent: the same logical request (the same
        id) reuses the workflow and its Sr conversation instead of creating
        another. This is what stops a retried HTTP request -- a client timeout
        followed by a resubmit, a double submit, a proxy retry -- from producing
        several Sr conversations for one user intent. Without it, every retry
        minted a new workflow id and started a new Sr, which is exactly the
        duplication observed in production.

        The uniqueness is enforced by ``workflows.idempotency_key``, so two
        concurrent requests with the same id cannot both create a workflow.
        """
        if not goal.strip():
            raise ValueError("goal is required")
        if repository is not None and self.store.allowed_repository(repository) is None:
            raise ValueError(f"repository not allowed: {repository}")

        if request_id:
            workflow_id, created = self.store.create_workflow_idempotent(
                goal, request_id
            )
        else:
            workflow_id, created = self.store.create_workflow_once(goal)

        existing = self.store.sr_for(workflow_id)
        if not created and existing and existing.get("sr_conversation_id"):
            # A previous attempt already owns the Sr for this request. Return it
            # rather than starting a second conversation.
            return {"workflow_id": workflow_id,
                    "sr_conversation_id": existing["sr_conversation_id"],
                    "reused": True}

        # Reserve the start before touching the Cloud. Two concurrent requests
        # with the same request id both reach this point (the loser of the
        # workflow insert reuses the winner's row), and without the claim both
        # would start an Sr. The loser returns immediately; the background loop
        # completes the start the winner began.
        if not self.store.claim_sr_start(workflow_id, self.dispatcher.worker_id,
                                         lease_seconds=max(
                                             self.dispatcher.lease_seconds, 120.0)):
            return {"workflow_id": workflow_id, "sr_conversation_id": None,
                    "reused": True, "starting": True}

        try:
            conversation_id = self.runtime.ensure_sr(workflow_id)
        except CloudError as e:
            # Release the lease so the Sr loop can retry promptly. Keep the
            # workflow so the user can see it failed to start Sr; the loop owns
            # the retry policy (transient -> backoff, permanent -> block).
            self.store.release_sr_start(workflow_id)
            self.store.record_sr_failure(
                workflow_id, error=f"{type(e).__name__}: {e}", attempt=1,
                backoff_seconds=0.0, give_up=False,
            )
            raise
        return {"workflow_id": workflow_id, "sr_conversation_id": conversation_id,
                "reused": not created}

    def cancel_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Stop a workflow for good. Terminal and idempotent.

        The Sr conversation is left alive on purpose: deleting it would destroy
        the transcript the operator needs to see why they stopped the work. Only
        the dispatcher's own state becomes terminal, so nothing is dispatched or
        polled for it again.
        """
        return self.store.cancel_workflow(workflow_id)

    def resume_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Reopen a blocked or failed workflow and requeue its retryable tasks."""
        return self.store.resume_workflow(workflow_id)

    def retry_task(self, task_id: str) -> dict[str, Any]:
        """Requeue one task for another attempt."""
        task = self.store.retry_task(task_id)
        return {"task_id": task.id, "state": task.state.value}

    def rename_sr(self, workflow_id: str, title: str) -> dict[str, Any]:
        """Rename the workflow's Sr conversation in Cloud.

        The title is applied to the real Cloud conversation (``PATCH``) first and
        only then recorded locally, so the two never disagree: if Cloud rejects
        the change, nothing local is written. The goal text is not touched, so a
        rename cannot change what the work is.
        """
        title = (title or "").strip()
        if not title:
            raise ValueError("title is required")
        if len(title) > 200:
            raise ValueError("title is too long (max 200)")
        sr = self.store.sr_for(workflow_id)
        if sr is None:
            raise NotFound(f"workflow {workflow_id}")
        conversation_id = sr.get("sr_conversation_id")
        if not conversation_id:
            raise ValueError("Sr conversation is not created yet")
        self.client.update_conversation(conversation_id, title=title)
        return {"workflow_id": workflow_id, "conversation_id": conversation_id,
                "title": title}

    def sync_github_repositories(self, *, include_archived: bool = False) -> dict[str, Any]:
        """Pull the account's GitHub repositories into the allowlist.

        Discovery is read-only against GitHub; the only write is to this
        dispatcher's own ``repositories`` table. A GitHub outage therefore
        cannot corrupt the allowlist: the list is fetched completely before
        anything is stored, and the store call is one transaction.
        """
        repos = self.github.list_repositories(include_archived=include_archived)
        payload = [
            {"full_name": r.full_name, "default_branch": r.default_branch,
             "provider": "github"}
            for r in repos
        ]
        result = self.store.sync_repositories(payload)
        result["repositories"] = [
            r.as_dict() for r in repos
        ]
        return result

    def workflow_detail(self, workflow_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", workflow_id):
            raise NotFound(workflow_id)
        wf = self.store.sr_for(workflow_id)
        if wf is None:
            raise NotFound(workflow_id)
        workflows = {w["id"]: w for w in self.store.list_workflows()}
        tasks = self.store.list_tasks(workflow_id=workflow_id)
        # A reason to show when the workflow is not running: the first piece of
        # evidence on any failed/blocked/dead-lettered task, else nothing.
        reason = None
        for t in tasks:
            if t.state.value in ("failed", "blocked", "dead_letter") and t.evidence:
                reason = t.evidence[0].get("reason") if isinstance(t.evidence[0], dict) else str(t.evidence[0])
                break
        return {
            "workflow": {
                "id": workflow_id,
                "goal": wf["goal"],
                "state": workflows.get(workflow_id, {}).get("state"),
                "sr_conversation_id": wf.get("sr_conversation_id"),
                "sr_status": wf.get("sr_status"),
                # The dashboard URL, derived from the API URL Cloud returns.
                # Without it the UI's "open in Cloud" link never rendered, so
                # there was no way to jump to the transcript.
                "sr_url": (
                    self.client.web_url(wf["sr_conversation_id"])
                    if wf.get("sr_conversation_id") else None
                ),
                "reason": reason,
            },
            "tasks": [
                {
                    "id": t.id, "goal": t.goal, "state": t.state.value,
                    "attempt": t.attempt, "max_attempts": t.max_attempts,
                    "repository": t.selected_repository,
                    "branch": t.selected_branch, "result": t.result,
                    "evidence": t.evidence,
                }
                for t in tasks
            ],
            "tool_calls": self.store.tool_calls(workflow_id=workflow_id),
            "events": [
                {"at": str(e.get("at") or ""), "kind": e.get("kind"),
                 "detail": e.get("detail")}
                for e in self.store.events(workflow_id=workflow_id, limit=50)
            ],
        }

    def system_info(self) -> dict[str, Any]:
        """Safe diagnostics: presence of settings, never their values."""
        store_ok, store_err = True, None
        try:
            self.store.dashboard_counts()
        except Exception as e:  # noqa: BLE001 - diagnostics must not raise
            store_ok, store_err = False, f"{type(e).__name__}"
        cloud_ok, cloud_err = False, None
        try:
            cloud_ok = self.client.health()
            if not cloud_ok:
                cloud_err = "unreachable"
        except CloudError as e:
            cloud_err = type(e).__name__
        except Exception as e:  # noqa: BLE001
            cloud_err = type(e).__name__
        last = self.last_tick or {}
        return {
            "store": {"ok": store_ok, "error": store_err},
            "cloud": {"ok": cloud_ok, "error": cloud_err, "url": self.client.base_url},
            "config": {
                "cloud_key": bool(os.environ.get("OPENHANDS_API_KEY")),
                "writes_require_auth": bool(self.token),
                # Presence only. The token value never leaves the environment.
                "github_token": self.github.configured,
                "login_configured": self.auth.configured,
                "login_missing": self.auth.missing_settings(),
                # Reasons only, never a value: makes a bad AUTH_PASSWORD_HASH
                # visible from the API without leaking the hash.
                "login_errors": self.auth.config_errors(),
            },
            "build": {
                "version": os.environ.get("APP_VERSION") or _version(),
                "commit": (os.environ.get("RENDER_GIT_COMMIT")
                           or os.environ.get("GIT_COMMIT") or "unknown"),
            },
            "runtime": {
                "poll_interval": self.poll_interval,
                "last_tick_at": last.get("at"),
                "last_error": last.get("error") or last.get("cloud_error"),
            },
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "SrJrDispatcher/2.0"

    def log_message(self, *a: Any) -> None:  # keep the console quiet
        pass

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    # ------------------------------------------------------------- helpers
    def _json(self, code: int, payload: Any, *, cookie: str | None = None) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code: int, text: str, *, cookie: str | None = None) -> None:
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _static(self, code: int, text: str, content_type: str) -> None:
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, to: str, *, cookie: str | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", to)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except json.JSONDecodeError:
            raise ValueError("body is not valid JSON")

    def _form(self) -> dict[str, str]:
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode() if n else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def _client_ip(self) -> str:
        # Render terminates TLS and sets X-Forwarded-For; take the first hop.
        fwd = self.headers.get("X-Forwarded-For")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "unknown"

    def _cookie_secure(self) -> bool:
        """Mark cookies Secure when the request arrived over HTTPS."""
        if os.environ.get("WEB_COOKIE_SECURE", "").lower() in ("1", "true", "yes"):
            return True
        if os.environ.get("WEB_COOKIE_SECURE", "").lower() in ("0", "false", "no"):
            return False
        return (self.headers.get("X-Forwarded-Proto") == "https")

    # --------------------------------------------------------------- session
    def _session(self):
        token = parse_cookie(self.headers.get("Cookie"), SESSION_COOKIE)
        return self.app.auth.resolve(token)

    def _bearer_ok(self) -> bool:
        """Machine authentication for the API, unchanged from before.

        Kept separate from the browser session so that adding login never
        weakens (or is weakened by) the existing bearer-token protection.
        """
        token = getattr(self.app, "token", None)
        if not token:
            return False
        return self.headers.get("Authorization", "") == f"Bearer {token}"

    def _require_read(self) -> bool:
        """Reads need either a browser session or a valid bearer token."""
        if self._session() is not None or self._bearer_ok():
            return True
        self._json(401, {"error": "unauthorized"})
        return False

    def _require_write(self) -> bool:
        """Writes need a session with a valid CSRF token, or a bearer token."""
        if self._bearer_ok():
            return True
        session = self._session()
        if session is None:
            self._json(401, {"error": "unauthorized"})
            return False
        if not self.app.auth.csrf_ok(session, self.headers.get(CSRF_HEADER)):
            self._json(403, {"error": "csrf token missing or invalid"})
            return False
        return True

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            # --- public ---
            if path == "/login":
                if self._session() is not None:
                    return self._redirect("/")
                return self._html(200, LOGIN_PAGE.replace("__ERROR__", ""))
            if path == "/healthz":
                return self._json(200, HEALTHZ)
            if path == "/static/app.css":
                return self._static(200, APP_CSS, "text/css; charset=utf-8")
            if path == "/static/app.js":
                return self._static(200, APP_JS, "application/javascript; charset=utf-8")
            if path == "/favicon.ico":
                # SVG works as a favicon in every browser that renders this
                # dashboard; declaring it in the markup avoids a 404 for the
                # default /favicon.ico probe.
                return self._static(200, FAVICON_SVG, "image/svg+xml")
            if path == "/favicon.svg":
                return self._static(200, FAVICON_SVG, "image/svg+xml")

            # --- everything below requires authentication ---
            if path == "/":
                if self._session() is None and not self._bearer_ok():
                    return self._redirect("/login")
                return self._html(200, APP_HTML)
            if not self._require_read():
                return

            if path == "/api/session":
                s = self._session()
                return self._json(200, {
                    "username": s.username if s else "api",
                    "csrf_token": s.csrf_token if s else "",
                })
            if path == "/api/dashboard":
                return self._json(200, {"counts": self.app.store.dashboard_counts()})
            if path == "/api/system":
                return self._json(200, self.app.system_info())
            if path == "/api/config":
                return self._json(200, {
                    "cloud_key": bool(os.environ.get("OPENHANDS_API_KEY")),
                    "cloud_url": self.app.client.base_url,
                })
            if path == "/api/repositories":
                return self._json(200, {
                    "repositories": self.app.store.list_repositories()
                })
            if path == "/api/github/repositories":
                # Preview what a sync would pull, without writing anything. Lets
                # the UI show the discovered list before the operator commits.
                try:
                    repos = self.app.github.list_repositories()
                except GitHubAuthError as e:
                    return self._json(400, {"error": f"github auth: {e}"})
                except GitHubUnavailable as e:
                    return self._json(503, {"error": f"github unavailable: {e}"})
                except GitHubError as e:
                    return self._json(502, {"error": f"github: {e}"})
                return self._json(200, {
                    "repositories": [r.as_dict() for r in repos],
                    "count": len(repos),
                })
            if path == "/api/tasks":
                return self._json(200, {"tasks": self.app.store.list_tasks_all()})
            if path == "/api/workflows":
                out = []
                for w in self.app.store.list_workflows():
                    tasks = self.app.store.list_tasks(workflow_id=w["id"])
                    out.append({
                        "id": w["id"], "goal": w["goal"], "state": w["state"],
                        "sr_status": w.get("sr_status"),
                        "sr_conversation_id": w.get("sr_conversation_id"),
                        "task_count": len(tasks),
                    })
                return self._json(200, {"workflows": out})
            if path.startswith("/api/workflows/"):
                workflow_id = path.split("/")[3]
                return self._json(200, self.app.workflow_detail(workflow_id))
            if path == "/api/health":
                return self._json(200, {
                    "store": True,
                    "cloud": self.app.client.health(),
                    "writes_require_auth": bool(getattr(self.app, "token", None)),
                    "login_configured": self.app.auth.configured,
                    "last_tick": self.app.last_tick,
                })
            if path == "/api/capabilities":
                # Read-only. Reports the account-level configuration the
                # dispatcher will inherit; it never changes anything and never
                # returns secret values.
                #
                # Repository discovery state is gathered first and always
                # included: a Cloud outage must not hide whether the allowlist
                # is populated or whether a GitHub token is set, because those
                # two facts are what the operator needs to act on.
                try:
                    allowlisted = len(self.app.store.list_repositories())
                except StoreError:
                    allowlisted = 0
                github = {
                    "token_configured": self.app.github.configured,
                    "allowlisted": allowlisted,
                }
                try:
                    caps = self.app.client.account_capabilities()
                except CloudError as e:
                    return self._json(502, {"error": f"cloud: {e}", "github": github})
                caps["dispatcher"] = {
                    "agent_profile_id": self.app.runtime.agent_profile_id,
                    "plugins": self.app.runtime.plugins or [],
                }
                caps["github"] = github
                return self._json(200, caps)
            return self._json(404, {"error": "not found"})
        except NotFound:
            return self._json(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            log.exception("GET %s failed", path)
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    # ----------------------------------------------------------------- POST
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            # --- public: login (rate limited) ---
            if path == "/login":
                return self._login()
            # --- logout: needs a session, but not CSRF (it only ends access) ---
            if path == "/logout":
                session = self._session()
                token = parse_cookie(self.headers.get("Cookie"), SESSION_COOKIE)
                if session is None and not self._bearer_ok():
                    return self._json(401, {"error": "unauthorized"})
                self.app.auth.logout(token)
                return self._json(200, {"ok": True},
                                  cookie=cleared_cookie_header(secure=self._cookie_secure()))
            # --- writes ---
            if not self._require_write():
                return
            body = self._body()
            if path == "/api/repositories":
                name = (body.get("full_name") or "").strip()
                if not re.fullmatch(r"[\w.-]+/[\w.-]+", name):
                    return self._json(400, {"error": "full_name must be owner/name"})
                rid = self.app.store.add_repository(
                    name,
                    provider=body.get("provider") or "github",
                    default_branch=body.get("default_branch") or "main",
                    allowed_branches=body.get("allowed_branches") or [],
                )
                return self._json(200, {"repository_id": rid})
            if path == "/api/repositories/sync":
                # One-shot import of the account's GitHub repositories. This is
                # the "let GitHub fill itself in" path: no hand-typed names.
                try:
                    out = self.app.sync_github_repositories(
                        include_archived=bool(body.get("include_archived")),
                    )
                except GitHubAuthError as e:
                    return self._json(400, {"error": f"github auth: {e}"})
                except GitHubUnavailable as e:
                    return self._json(503, {"error": f"github unavailable: {e}"})
                except GitHubError as e:
                    return self._json(502, {"error": f"github: {e}"})
                return self._json(200, out)
            if path == "/api/repositories/remove":
                name = (body.get("full_name") or "").strip()
                if not name:
                    return self._json(400, {"error": "full_name is required"})
                removed = self.app.store.remove_repository(name)
                if not removed:
                    return self._json(404, {"error": "repository not found"})
                return self._json(200, {"removed": name})
            if path == "/api/workflows":
                # A stable request id makes this endpoint safe to retry. The
                # browser sends one per logical submission (see app.js); a
                # machine client may send ``Idempotency-Key``. Without either,
                # the call behaves as before (always creates).
                request_id = (
                    self.headers.get("Idempotency-Key")
                    or (body.get("request_id") or "").strip()
                    or None
                )
                out = self.app.start_workflow(
                    body.get("goal") or "",
                    body.get("repository"),
                    body.get("branch"),
                    request_id=request_id,
                )
                return self._json(200, out)
            if path.startswith("/api/workflows/") and path.endswith("/message"):
                workflow_id = path.split("/")[3]
                text = (body.get("text") or "").strip()
                if not text:
                    return self._json(400, {"error": "text is required"})
                sr = self.app.store.sr_for(workflow_id)
                if not sr or not sr.get("sr_conversation_id"):
                    return self._json(409, {"error": "Sr conversation not ready"})
                ok = self.app.client.send_message(sr["sr_conversation_id"], text)
                return self._json(200, {"sent": ok})
            if path.startswith("/api/workflows/") and path.endswith("/cancel"):
                workflow_id = path.split("/")[3]
                return self._json(200, self.app.cancel_workflow(workflow_id))
            if path.startswith("/api/workflows/") and path.endswith("/resume"):
                workflow_id = path.split("/")[3]
                return self._json(200, self.app.resume_workflow(workflow_id))
            if path.startswith("/api/workflows/") and path.endswith("/rename"):
                workflow_id = path.split("/")[3]
                return self._json(200, self.app.rename_sr(
                    workflow_id, body.get("title") or ""
                ))
            if path.startswith("/api/tasks/") and path.endswith("/retry"):
                task_id = path.split("/")[3]
                return self._json(200, self.app.retry_task(task_id))
            return self._json(404, {"error": "not found"})
        except NotFound:
            return self._json(404, {"error": "not found"})
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except InvalidTransition as e:
            return self._json(409, {"error": str(e)})
        except CloudError as e:
            return self._json(502, {"error": f"cloud: {e}"})
        except Exception as e:  # noqa: BLE001
            log.exception("POST %s failed", path)
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    # ---------------------------------------------------------------- login
    def _login(self) -> None:
        form = self._form()
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        if not self.app.auth.configured:
            reasons = self.app.auth.config_errors()
            log.error("login is not configured: %s", "; ".join(reasons))
            return self._html(
                503,
                LOGIN_PAGE.replace(
                    "__ERROR__",
                    "Вход не настроен на сервере: "
                    + "; ".join(reasons)
                    + ". Сгенерируйте хеш командой "
                    "`python -m orchestration.web_auth hash` и задайте "
                    "AUTH_PASSWORD_HASH в Render.",
                ),
            )
        try:
            session = self.app.auth.login(username, password, client_ip=self._client_ip())
        except LoginLocked as e:
            minutes = max(1, (e.retry_after + 59) // 60)
            return self._html(
                429,
                LOGIN_PAGE.replace(
                    "__ERROR__",
                    "Слишком много неудачных попыток. Повторите через "
                    f"{minutes} мин. Если пароль забыт, хеш можно пересоздать "
                    "(см. DEPLOY.md).",
                ),
            )
        except AuthError:
            session = None
        if session is None:
            # The same message for every failure: no user enumeration.
            return self._html(
                401,
                LOGIN_PAGE.replace(
                    "__ERROR__", "Неверный логин или пароль. Попробуйте ещё раз."
                ),
            )
        cookie = session_cookie_header(
            session.token, ttl=self.app.auth.session_ttl, secure=self._cookie_secure()
        )
        return self._redirect("/", cookie=cookie)



class WebServer:
    """Context manager for the HTTP server and its background loop."""

    def __init__(self, app: App, host: str = "127.0.0.1", port: int = 8000):
        self.app = app
        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.app = app  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self) -> None:
        self.app.start()
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)
        self.app.stop()

    def __enter__(self) -> "WebServer":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description="Sr/Jr dispatcher web server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--poll-interval", type=float, default=5.0)
    ap.add_argument(
        "--token",
        default=os.environ.get("WEB_AUTH_TOKEN"),
        help="bearer token required for machine API writes (default: $WEB_AUTH_TOKEN)",
    )
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)

    dsn = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("set SUPABASE_DB_URL")
        return 2
    if args.host not in ("127.0.0.1", "localhost") and not args.token:
        print("refusing to bind a public interface without --token/WEB_AUTH_TOKEN")
        return 2
    # Login must be configured before exposing the server: without it, a public
    # deployment would have no way to authenticate a human. A non-empty but
    # unusable AUTH_PASSWORD_HASH counts as unconfigured, so a bad paste fails at
    # boot with a reason instead of rejecting every login as "wrong password".
    config_errors = AuthService(
        store=None,  # type: ignore[arg-type] - only reading env here
    ).config_errors()
    if args.host not in ("127.0.0.1", "localhost") and config_errors:
        print("refusing to bind a public interface without login configured:")
        for reason in config_errors:
            print(f"  - {reason}")
        print("generate a hash with: python -m orchestration.web_auth hash")
        return 2
    try:
        app = App(dsn, poll_interval=args.poll_interval, token=args.token)
    except StoreError as e:
        print(f"store error: {e}")
        return 2
    # A forgotten migration shows up as UndefinedTable on the first request, and a
    # foreign schema as UndefinedColumn; name the problem at startup instead.
    missing_tables = app.store.verify_schema()
    if missing_tables:
        print("database schema does not match this dispatcher: "
              + ", ".join(missing_tables)
              + " (a bare table name is missing; table(column) exists with a "
                "different shape). Apply migrations/, or point SUPABASE_DB_URL "
                "at an empty database.")
        return 2
    server = WebServer(app, args.host, args.port)
    server.start()
    print(f"listening on http://{args.host}:{server.port}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
