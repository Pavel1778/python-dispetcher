"""NEXUS Studio and its API (stdlib-only HTTP server).

A single owner logs in with a username and password; every page and API is
protected by a server-side session (see :mod:`orchestration.web_auth`). The
browser gets one application, the studio (:mod:`orchestration.web_ui`): a chat
with the assistant, the bound repository's files, and the progress of the work
a chat turn started. A "Code" turn creates a workflow and starts a real Sr
conversation on OpenHands Cloud, and a background loop drives both sides:

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
    GET  /readyz                 readiness probe (liveness + DB round-trip)
    GET  /static/...             the studio's assets (closed list, see web_ui.STATIC)

Protected endpoints (session cookie, or Bearer WEB_AUTH_TOKEN for API writes):
    GET  /                       the studio
    POST /logout                 revoke the session
    GET  /api/session            current user + CSRF token
    GET  /api/studio             what is configured (names only, never a value)
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
import hmac
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .cloud_client import CloudError
from .cloud_direct import build_cloud_client
from .cloud_dispatcher import CloudDispatcher
from .cloud_store import (
    InvalidTransition,
    NotFound,
    PostgresStore,
    StoreError,
    StoreUnavailable,
)
from .file_lab import FileLabService, max_upload_bytes
from .github_client import (
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubUnavailable,
    GitHubWriter,
    VisibilityError,
)
from .github_confirmations import Confirmations
from .multipart import MultipartError, parse_multipart
from .nexus_actions import ActionStateError, ConfirmationMismatch, PendingActions
from .nexus_binding import AuthorizationError, ProjectBindingError, authorize_run_task
from .nexus_chat import ChatError
from .nexus_intent import classify, clarifying_question, effective_mode
from .nexus_files import FileService, MAX_FILE_SIZE
from .voice import VoiceNoteService
from .nexus_projects import IdempotencyConflict, ProjectService, principal_for_session
from .nexus_planning import PlanService
from .nexus_policy import install_log_redaction, policy_report
from .nexus_spec_planner import PlanningFailed, SpecPlanner
from .nexus_specs import (
    SpecCorrupted,
    SpecService,
    SpecTooLarge,
    check_message,
    check_text,
    inline_chars,
    max_spec_chars,
    preview,
)
from .providers import ProviderCascade
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
from . import web_ui
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

# Explicit allowlist: do not expose arbitrary files from the package directory.
_PWA_DIR = Path(__file__).resolve().parent / "pwa"
PWA_ASSETS = {
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json", "no-cache"),
    "/service-worker.js": ("service-worker.js", "application/javascript; charset=utf-8", "no-cache"),
    "/static/pwa/register.js": ("register.js", "application/javascript; charset=utf-8", "public, max-age=300"),
    "/static/pwa/icon-192.png": ("icon-192.png", "image/png", "public, max-age=300"),
    "/static/pwa/icon-512.png": ("icon-512.png", "image/png", "public, max-age=300"),
}

# Ids are UUIDs. ``[0-9a-fA-F-]{36}`` is too weak: 36 hyphens match it and then
# reach PostgreSQL, which rejects the value and turns a bad request into a 500
# that echoes the database error. Require the real 8-4-4-4-12 shape so a
# malformed id is a 404 before any query runs.
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

#: Sent with every HTML page. The studio renders model output, repository files
#: and GitHub names, all of which are untrusted text. The renderer escapes them;
#: this policy is the second line: with no inline script and no foreign origin
#: allowed, markup that slipped through an escaping bug still cannot run code or
#: send data anywhere. ``style-src 'self'`` is why the UI never emits a ``style``
#: attribute and sets dynamic sizes through the CSSOM instead.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


def title_from_message(text: str, *, limit: int = 60) -> str:
    """A conversation title made from the first line of its first message.

    Cut at a word boundary so the history never shows half a word, and marked
    with an ellipsis when something was dropped.
    """
    stripped = (text or "").strip()
    line = " ".join(stripped.splitlines()[0].split()) if stripped else ""
    if len(line) <= limit:
        return line
    cut = line[:limit]
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,.;:-") + "…"


def _task_brief(task: Any) -> dict[str, Any]:
    """A compact task view for list endpoints (no events or evidence payload)."""
    return {
        "id": task.id,
        "goal": task.goal,
        "state": task.state.value,
        "attempt": task.attempt,
        "max_attempts": task.max_attempts,
        "project_id": task.project_id,
        "workflow_id": task.workflow_id,
        "repository": task.selected_repository,
        "branch": task.selected_branch,
        "updated_at": task.updated_at,
    }


class BodyTooLarge(ValueError):
    """The request body is larger than its route accepts. Answered with 413."""


#: JSON bodies under ``/api/v1/projects`` unless the route carries a task text.
_PROJECT_BODY_BYTES = 4 * 1024 * 1024
_TEXT_ROUTE = re.compile(
    r"/api/v1/projects/[^/]+/(?:goals|conversations/[^/]+/messages)\Z")


def _json_body_cap(path: str) -> int | None:
    """How many bytes of JSON a POST to *path* may carry.

    The two routes that take a task text accept NEXUS_SPEC_MAX_CHARS characters
    of it, however the client encoded them: JSON may write any character as
    ``\\uXXXX`` (six bytes) and a character outside the basic plane as two of
    those, so the byte limit is twelve per character plus room for the rest of
    the body. The limit on the text itself is checked on the decoded text, in
    characters (``nexus_specs.check_text``); this one only bounds what is read.
    """
    if not path.startswith("/api/v1/projects"):
        return None
    if _TEXT_ROUTE.fullmatch(path):
        return max(_PROJECT_BODY_BYTES, max_spec_chars() * 12 + 64 * 1024)
    return _PROJECT_BODY_BYTES


def lab_inline_default() -> bool:
    """Whether this server advances autonomous runs itself. On unless switched off.

    ``NEXUS_LAB_INLINE`` set to ``0``, ``false``, ``no`` or ``off`` switches it
    off - for a deployment where ``python -m orchestration.lab_worker`` does
    that work as its own process. Unset, empty or anything else leaves it on.

    It used to be off unless asked for. A deployment that is one web process
    (the Render blueprint here, and the free plan has no worker to add) then
    accepted every run, planned it and never started a task: nothing said so
    but a note in the run's card, and the runs sat at "pending, attempt 0"
    until their deadline. A run that nobody advances is the worse default.
    """
    value = os.environ.get("NEXUS_LAB_INLINE", "").strip().lower()
    if value in ("0", "false", "no", "off"):
        return False
    if value not in ("", "1", "true", "yes", "on"):
        log.warning("NEXUS_LAB_INLINE=%r is not 0/1; the server advances runs itself", value)
    return True


def _int_env(name: str, default: int) -> int:
    """Read a positive integer from the environment, or *default*."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
    return value if value > 0 else default


def trusted_hop_count() -> int:
    """How many trusted proxies sit in front of this process.

    ``X-Forwarded-For`` is client-controlled on the left: a caller can prepend
    any value, and a proxy only appends. So the *n*-th address from the right,
    where *n* is the number of proxies we actually run behind, is the first one
    a trusted hop wrote. Reading the leftmost value (the naive approach) lets an
    attacker rotate the header and defeat the login rate limit entirely.

    ``TRUSTED_PROXY_HOPS`` defaults to 1, which matches a single platform load
    balancer such as Render's. Treat that default as an assumption to verify,
    not a fact: Render documents Cloudflare in front of its load balancers and
    does not publish how many ``X-Forwarded-For`` entries a deployment adds. Set
    it to 0 to ignore the header completely (a direct connection, or a
    deployment with no proxy), and raise it for each additional proxy of your
    own in front of the app.
    """
    raw = os.environ.get("TRUSTED_PROXY_HOPS", "1").strip()
    try:
        hops = int(raw)
    except ValueError:
        log.warning("TRUSTED_PROXY_HOPS=%r is not an integer; using 1", raw)
        return 1
    return max(0, hops)


def client_ip_from_values(values: list[str] | None, peer: str | None, hops: int) -> str:
    """Return the client IP, trusting only the rightmost *hops* addresses.

    *values* is every ``X-Forwarded-For`` header line, in order. A caller may
    send the header more than once, and each proxy appends its own line, so the
    lines must be joined before the list is indexed: reading only the first line
    (``headers.get``) hands the key back to the caller, who simply repeats the
    header. With ``hops=0`` the header is ignored and the socket peer is used.
    A list shorter than *hops* cannot be trusted (a proxy that should have
    appended did not), so the socket peer is used instead of a spoofable value.
    """
    peer = peer or "unknown"
    if hops <= 0 or not values:
        return peer
    joined = ",".join(v for v in values if v)
    addresses = [part.strip() for part in joined.split(",") if part.strip()]
    if len(addresses) < hops:
        log.warning(
            "X-Forwarded-For has %d address(es) but %d proxy hop(s) are "
            "configured; using the socket peer", len(addresses), hops,
        )
        return peer
    return addresses[len(addresses) - hops]


def client_ip_from(forwarded_for: str | None, peer: str | None, hops: int) -> str:
    """Single-header convenience wrapper around :func:`client_ip_from_values`."""
    return client_ip_from_values([forwarded_for] if forwarded_for else [], peer, hops)


class _LoginThrottle:
    """Cap login attempts, per client IP and globally.

    Two ceilings, because the client IP is not fully trustworthy:

    * **Per IP** caps a single source. It relies on the resolved client IP, so it
      is only as good as the proxy configuration (see ``trusted_hop_count``).
    * **Global** caps the whole endpoint regardless of the claimed IP. It is the
      backstop for the case where the IP is spoofable - a platform that
      overwrites rather than appends ``X-Forwarded-For``, or a header we
      misconfigured. It cannot be bypassed by any request header.

    In-memory on purpose: this is a backstop against online guessing, not the
    source of truth, so losing it on restart is acceptable.
    """

    def __init__(self, max_attempts: int = 30, window_seconds: float = 60.0,
                 global_max_attempts: int = 120, global_window_seconds: float = 60.0):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.global_max_attempts = global_max_attempts
        self.global_window_seconds = global_window_seconds
        self._hits: dict[str, list[float]] = {}
        self._global: list[float] = []
        self._lock = threading.Lock()

    def check(self, ip: str) -> int:
        """Record an attempt for *ip*. Return seconds to wait, or 0 if allowed."""
        now = time.time()
        cutoff = now - self.window_seconds
        g_cutoff = now - self.global_window_seconds
        with self._lock:
            self._global = [t for t in self._global if t > g_cutoff]
            if len(self._global) >= self.global_max_attempts:
                return int(self._global[0] + self.global_window_seconds - now) + 1
            hits = [t for t in self._hits.get(ip, ()) if t > cutoff]
            if len(hits) >= self.max_attempts:
                self._hits[ip] = hits
                return int(hits[0] + self.window_seconds - now) + 1
            hits.append(now)
            self._hits[ip] = hits
            self._global.append(now)
            if len(self._hits) > 10_000:  # bound memory under a spray of IPs
                self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > cutoff}
            return 0

    def reset(self, ip: str) -> None:
        with self._lock:
            self._hits.pop(ip, None)


class App:
    """Wires the store, Cloud client, Sr runtime and Jr dispatcher together."""

    def __init__(self, dsn: str, *, worker_id: str = "web", poll_interval: float = 5.0,
                 token: str | None = None, agent_profile_id: str | None = None,
                 plugins: list[dict[str, Any]] | None = None,
                 auth: AuthService | None = None, lab_inline: bool | None = None):
        self.store = PostgresStore(dsn)
        self.projects = ProjectService(self.store)
        self.plans = PlanService(self.store)
        # The File Lab service is the superset: uploads, previews, workspaces,
        # archive extraction and voice notes. ``opaque_files`` is the earlier,
        # simpler opaque-blob service; both share the ``nexus_files`` table and
        # the ``/files`` routes, distinguished by row provenance (see
        # ``_file_path`` / ``Handler._get_project_resource``).
        self.files = FileLabService(self.store)
        self.opaque_files = FileService(self.store)
        self.voice = VoiceNoteService(self.store)
        self.actions = PendingActions(self.store)
        # D-2 project execution binding and the Evidence Trust Engine.
        from .nexus_binding import ProjectBindingService, ProjectRepositoryService
        from .nexus_evidence import EvidenceService
        from .evidence_trust import (
            EvidenceTrustEngine,
            Keyring,
            signer_from_env,
        )
        self.project_repos = ProjectRepositoryService(self.store)
        # The explicit project execution binding (repository + branch + account).
        self.project_binding = ProjectBindingService(self.store)
        self.evidence_keyring = Keyring.from_env()
        self.evidence_signer = signer_from_env()
        self.evidence = EvidenceService(
            self.store,
            EvidenceTrustEngine(self.evidence_keyring, self.evidence_signer),
        )
        self.github_confirmations = Confirmations()
        self.github_writes_enabled = os.environ.get("GITHUB_ENABLE_WRITES", "").lower() in ("1", "true", "yes")
        # The LLM orchestrator cascade (Gemini -> Groq -> OpenRouter ->
        # OpenHands). It only proposes plans; it never executes them. Only
        # configured providers are used, and only their presence is reported.
        self.providers = ProviderCascade()
        # The autonomous lab: durable runs/tasks, project context and memory.
        # The web server *starts* a run. What advances it is the lab loop: a
        # thread of this server (``_run_lab``) unless NEXUS_LAB_INLINE switches
        # it off, and ``python -m orchestration.lab_worker`` as its own process
        # where there is one; both may run at once. Either way a browser
        # request never drives a run, and closing the browser never stops one.
        self.lab_inline = (lab_inline_default() if lab_inline is None
                           else bool(lab_inline))
        self._lab_worker_id = f"{worker_id}-lab"
        from .nexus_context import ContextService, MemoryService
        from .nexus_github_accounts import GitHubAccountService
        from .nexus_lab_store import LabStore
        from .nexus_openhands_accounts import OpenHandsAccountService
        from .nexus_planner import Planner
        self.lab = LabStore(self.store)
        self.context = ContextService(self.store)
        self.memory = MemoryService(self.store)
        self.planner = Planner(self.providers)
        self.github_accounts = GitHubAccountService(self.store)
        # OpenHands multi-account: an account is one row, the API key encrypted
        # with NEXUS_SECRET_KEY and never returned. A project may bind one.
        self.openhands_accounts = OpenHandsAccountService(self.store)
        # A project's own list of executors: which connections it may run on,
        # in order. A project with a list is served from it and nothing else
        # (``openhands_router``); one without keeps the previous behaviour.
        from .nexus_executors import ProjectExecutorService
        self.project_executors = ProjectExecutorService(self.store, self.openhands_accounts)
        # A completed run's report: written with the completion, delivered to
        # the chat the run was started from (``nexus_reports``).
        from .nexus_reports import ReportService
        self.reports = ReportService(self.store)
        # The account pool: which account runs work, and in what order, under a
        # selection mode. The policy lives in the service, not the UI.
        from .openhands_pool import AccountPool
        self.account_pool = AccountPool(self.store, self.openhands_accounts)
        # The persistent conversational surface. The responder needs the provider
        # cascade for ask/plan and the dispatch hooks for code/autonomous.
        from .nexus_chat import ChatResponder
        from .nexus_conversations import ConversationService
        self.conversations = ConversationService(self.store)
        self.chat = ChatResponder(
            self.providers,
            dispatch_code=self._chat_dispatch_code,
            start_autonomous=self._chat_start_autonomous,
        )
        # A task text of any size up to NEXUS_SPEC_MAX_CHARS: stored exactly as
        # it was sent, indexed, and planned in parts that fit the planning
        # model. A text that is one part is planned inside the request; a
        # longer one by ``_run_planning``, which always runs in this process -
        # it is the process that accepted the text, and the only one a
        # one-service deployment has. The planner reads ``self.planner.cascade``
        # at the moment of use, so replacing the planner replaces its providers.
        self.specs = SpecService(self.store)
        self.spec_planner = SpecPlanner(
            self.specs, self.lab, cascade=lambda: self.planner.cascade,
            describe=self._describe_project, conversations=self.conversations,
            owner=f"{worker_id}-planner")
        # The Cloud transport. "direct" (default) provisions a sandbox and talks
        # to its agent-server -- the only path that yields a running conversation
        # on the live service; "app" keeps the legacy app-conversation client.
        self.client = build_cloud_client(self.store)
        # Repository discovery for the allowlist. Reads GITHUB_TOKEN; the value
        # is never exposed through the API or the UI, only its presence.
        self.github = GitHubClient()
        # The guarded write path. Kept separate from ``self.github`` so nothing
        # that only needs to read can reach a write, and every call happens
        # inside a confirmed-action handler (see ``_github_*`` routes).
        self.github_writer = GitHubWriter(self.github)
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
        # Client-IP resolution depends on how many proxies we trust; see
        # ``trusted_hop_count``. Read once so a request never re-parses the env.
        self.trusted_hops = trusted_hop_count()
        # In-memory per-IP backstop for login attempts (see ``_LoginThrottle``).
        self.login_throttle = _LoginThrottle(
            max_attempts=_int_env("LOGIN_MAX_ATTEMPTS_PER_IP", 30),
            global_max_attempts=_int_env("LOGIN_MAX_ATTEMPTS_GLOBAL", 120),
        )
        self._stop = threading.Event()
        self._loop: threading.Thread | None = None
        self._lab_loop: threading.Thread | None = None
        self._plan_loop: threading.Thread | None = None
        self.last_tick: dict[str, Any] = {}
        self.last_lab_tick: dict[str, Any] = {}
        self.last_plan_tick: dict[str, Any] = {}
        # Sweep expired sessions and stale rate-limit rows occasionally, not
        # every tick: they are housekeeping, not dispatch work.
        self._sweep_every = 60
        self._sweeps_done = 0

    # ---------------------------------------------------------------- loop
    def start(self) -> None:
        self._loop = threading.Thread(target=self._run, daemon=True)
        self._loop.start()
        self._plan_loop = threading.Thread(target=self._run_planning, daemon=True)
        self._plan_loop.start()
        if self.lab_inline:
            self._lab_loop = threading.Thread(target=self._run_lab, daemon=True)
            self._lab_loop.start()

    def stop(self) -> None:
        self._stop.set()
        self.spec_planner.wake()
        for loop in (self._loop, self._lab_loop, self._plan_loop):
            if loop:
                loop.join(timeout=5)
        self.store.close()

    def _run_planning(self) -> None:
        """Build the plans of long specifications, one part per pass.

        A text that does not fit one planning request is accepted at once and
        planned here. Every part is a row in PostgreSQL claimed with a short
        lease, and every finished part keeps its plan, so a restart continues
        with the first part that has none; nothing is planned twice and nothing
        about a plan lives in this thread. When the last part is planned the run
        is created and the conversation is told, in one transaction.
        """
        while not self._stop.is_set():
            try:
                self.last_plan_tick = {"at": time.time(), **self.spec_planner.tick()}
            except Exception as e:  # noqa: BLE001 - the loop must survive
                log.exception("planning pass failed")
                self.last_plan_tick = {"at": time.time(),
                                       "errors": [f"{type(e).__name__}: {e}"]}
            self.spec_planner.wait(self.poll_interval)

    def _describe_project(self, principal: str, project_id: str) -> dict[str, Any]:
        """What a planning request says about the project besides the task."""
        project = self.projects.get(principal, project_id)
        context = self.context.current(principal, project_id)
        return {"name": project["name"],
                "context": (context or {}).get("summary") or {},
                "memory": self.memory.retrieve(principal, project_id, limit=30),
                # The repositories work may run on; with several, the planner
                # assigns each task to one (``nexus_spec_planner``).
                "repositories": self.project_repos.context(project_id)}

    def _run_lab(self) -> None:
        """Advance autonomous runs from inside this process.

        The same pass ``python -m orchestration.lab_worker`` makes, on its own
        thread so a slow conversation start never delays the workflow loop.
        Nothing about a run lives in this thread: every claim is a lease in
        PostgreSQL, so a restart continues where the last pass stopped, and a
        separate worker may run beside it.
        """
        from .lab_worker import LabWorker
        from .openhands_router import build_account_router
        worker: LabWorker | None = None
        log.info("this server advances autonomous runs itself (worker %s)",
                 self._lab_worker_id)
        while not self._stop.is_set():
            try:
                if worker is None:
                    # Built here, not before the loop: if it cannot be built
                    # now, the next pass tries again and the reason is in the
                    # status. A thread that died on its first line would leave
                    # a server that says it advances runs and does not.
                    built = LabWorker(self.store, worker_id=self._lab_worker_id,
                                      client=self.client, github=self.github,
                                      github_accounts=self.github_accounts,
                                      account_router=build_account_router(self.store),
                                      evidence=self.evidence)
                    # Known to the diagnostics as a live worker (migration 0023):
                    # a server that advances runs is one. Its heartbeat is
                    # written by every pass.
                    built.lab.register_worker(self._lab_worker_id, kind="inline")
                    worker = built
                summary = worker.tick_once()
                self.last_lab_tick = {"at": time.time(), **summary}
                if summary.get("advanced") or summary.get("errors"):
                    log.info("lab pass: %s", summary)
            except Exception as e:  # noqa: BLE001 - the loop must survive
                log.exception("lab tick failed")
                self.last_lab_tick = {"at": time.time(),
                                      "errors": [f"{type(e).__name__}: {e}"]}
            self._stop.wait(self.poll_interval)
        if worker is not None:
            try:
                worker.lab.stop_worker(self._lab_worker_id)
            except Exception:  # noqa: BLE001 - the process is stopping; best effort
                log.debug("could not record that the lab loop stopped", exc_info=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                sr = self.runtime.tick().as_dict()
                jr = self.dispatcher.tick().as_dict()
                vf = self.verifier.tick().as_dict()
                self.last_tick = {"at": time.time(), "sr": sr, "jr": jr, "verify": vf}
                self._maybe_sweep()
            except CloudError as e:
                self.last_tick = {"at": time.time(), "cloud_error": str(e)}
            except Exception as e:  # noqa: BLE001 - the loop must survive
                log.exception("tick failed")
                self.last_tick = {"at": time.time(), "error": f"{type(e).__name__}: {e}"}
            self._stop.wait(self.poll_interval)

    def _maybe_sweep(self) -> None:
        """Occasionally drop expired sessions and stale rate-limit rows.

        Without this the ``sessions`` and ``login_attempts`` tables grow without
        bound: nothing else deletes an expired session row or a rate-limit row
        whose window has passed. A failure here must never stop the dispatch
        loop, so it is swallowed and logged.
        """
        self._sweeps_done += 1
        if self._sweeps_done % self._sweep_every:
            return
        try:
            sessions = self.store.purge_expired_sessions()
            attempts = self.store.purge_stale_login_attempts()
            if sessions or attempts:
                log.info("swept %d expired session(s), %d stale login row(s)",
                         sessions, attempts)
        except Exception:  # noqa: BLE001 - housekeeping must not break dispatch
            log.exception("sweep failed")

    # ------------------------------------------------------------- actions
    def start_workflow(self, goal: str, repository: str | None = None,
                       branch: str | None = None,
                       request_id: str | None = None,
                       project_id: str | None = None,
                       principal: str | None = None) -> dict[str, Any]:
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
        # Project context is authorized before the workflow exists. A project
        # workflow may only name a repository that project owns.
        if project_id is not None:
            self.projects.get(principal, project_id)  # raises NotFound if not a member
            if repository is not None and not self.store.project_repository_linked(
                    project_id, repository):
                raise ValueError(f"repository not linked to project: {repository}")

        if request_id:
            workflow_id, created = self.store.create_workflow_idempotent(
                goal, request_id, project_id=project_id
            )
        else:
            workflow_id, created = self.store.create_workflow_once(
                goal, project_id=project_id
            )

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

    def _chat_dispatch_code(self, request: dict) -> dict[str, Any]:
        """Chat "Code" mode: start a real OpenHands workflow for the request.

        The project's bound account executes it (when one is bound), and the
        workflow is scoped to the project so the dispatcher's authorization gate
        re-checks the repository link before any conversation is created.
        """
        return self.start_workflow(
            request["goal"], request.get("repository"), request.get("branch"),
            project_id=request.get("project_id"),
            principal=request.get("principal"),
        )

    def _chat_start_autonomous(self, request: dict) -> dict[str, Any]:
        """Chat "Autonomous" mode: plan the task and start a real Lab run.

        The task is the specification the turn's text was stored as
        (``request["spec_id"]``): exact, of any size the server accepts. The
        goal row keeps a label of at most 20 000 characters and points at it.

        The plan comes from the provider cascade and is validated before it is
        stored; a provider outage raises, so the chat records a failure instead
        of a fabricated plan. The durable worker advances the run.

        Returns ``{"status": ...}``: ``started`` with ``run_id`` and ``tasks``;
        ``planning`` when the text is planned in parts in the background; or
        ``existing`` with the ``run_id`` of the run this same text already has
        in this conversation. The same text is the same specification and the
        same goal, so a repeated send - a retry after a timeout, a double
        click - never starts a second run.
        """
        principal = request["principal"]
        project_id = request["project_id"]
        repository = request.get("repository") or ""
        branch = request.get("branch") or ""
        self.projects.get(principal, project_id)
        if not repository:
            # A conversation with no bound repository runs against the project's
            # primary one (the single linked repository is primary in effect).
            repository = self.project_repos.primary_repository(project_id) or ""
        # Refuse before a planning call is spent, and before a goal is stored.
        authorize_run_task(self.store, project_id=project_id,
                           repository=repository, branch=branch)
        if request.get("spec_id"):
            goal, spec, _created = self.specs.goal_for(principal, project_id,
                                                       request["spec_id"])
        else:
            goal, spec, _created = self.specs.create_with_goal(
                principal, project_id, request["goal"], source="chat",
                conversation_id=request.get("conversation_id"))
        outcome = self.spec_planner.start(
            principal=principal, project_id=project_id, goal_id=str(goal["id"]),
            spec_id=spec["id"], repository=repository, branch=branch)
        summary = SpecService.summary(spec)
        if outcome["status"] == "started":
            run_id = str(outcome["run"]["id"])
            return {"status": "started", "run_id": run_id, "spec": summary,
                    "plan": outcome["plan"],
                    "tasks": self.lab.list_tasks(principal, project_id, run_id)}
        if outcome["status"] == "existing":
            return {"status": "existing", "run_id": outcome["run_id"], "spec": summary}
        return {"status": "planning", "spec": summary, "stages": outcome["stages"]}

    def studio_status(self) -> dict[str, Any]:
        """What the studio needs in order to explain itself: what is configured.

        Names and booleans only. No key, token, URL or hash is returned, and
        nothing here calls an external service, so the page can ask on every
        load. It deliberately does not touch the Cloud client.
        """
        return {
            "version": os.environ.get("APP_VERSION") or _version(),
            # The LLM providers that can serve an "ask" or "plan" turn. The UI
            # warns before the first message when none of them can.
            "providers": self.providers.configured(),
            "openhands": {
                "server_key": bool(os.environ.get("OPENHANDS_API_KEY")),
                "transport": os.environ.get("OPENHANDS_TRANSPORT", "direct"),
            },
            "github": {"server_token": self.github.configured},
            # Whether this process advances autonomous runs itself. When it
            # does not, a run waits for ``python -m orchestration.lab_worker``,
            # and the page says so instead of showing a run that never moves.
            "lab": {"inline": self.lab_inline},
            # Stored accounts are encrypted with NEXUS_SECRET_KEY; without it
            # adding one is refused (orchestration/secrets.py).
            "accounts": {
                "encryption_key": bool(os.environ.get("NEXUS_SECRET_KEY", "").strip()),
            },
        }

    def github_for_project(self, principal: str, project_id: str) -> GitHubClient:
        """The GitHub client a project's reads go through.

        A project with an account bound reads with that account's token, so the
        branches and files shown are the ones the connected identity can see.
        Without a binding the process-wide client (``GITHUB_TOKEN``) is used, as
        before. A bound account that was revoked or rejected is an error, never a
        silent switch to another identity.
        """
        binding = self.project_binding.get(principal, project_id)
        if binding.get("github_account_id"):
            return self.github_accounts.client_for_project(principal, project_id)
        return self.github

    def _admit_repository(self, client: GitHubClient, full_name: str,
                          branch: str | None) -> tuple[str, str, str]:
        """Look a repository and a branch up on GitHub as *client* and make
        them allowed here. Returns ``(canonical name, branch, default branch)``.

        Every GitHub check happens before the first write. The allowlist is
        only ever extended: an unlisted repository is added, and the chosen
        branch is added to a listed repository when the dispatch gate would
        otherwise refuse it. Nothing is removed, and a disabled repository
        stays disabled and is refused.
        """
        try:
            details = client.get_repository(full_name)
        except (GitHubAuthError, GitHubUnavailable):
            raise
        except GitHubError as e:
            if "404" in str(e):
                raise ValueError(f"repository {full_name} was not found, or this GitHub "
                                 "account cannot see it") from e
            raise
        canonical = details.get("full_name") or full_name
        default_branch = details.get("default_branch") or "main"
        chosen = branch or default_branch
        if chosen != default_branch:
            names = [b["name"] for b in client.list_branches(canonical)]
            if chosen not in names:
                # Only the first page of branches is listed; ask about this one
                # directly before refusing it.
                found = False
                if len(names) >= 100:
                    try:
                        found = client.default_branch_sha(canonical, chosen) is not None
                    except (GitHubAuthError, GitHubUnavailable):
                        raise
                    except GitHubError:
                        found = False
                if not found:
                    raise ValueError(f"branch {chosen} does not exist in {canonical}")

        existing = next((r for r in self.store.list_repositories(enabled_only=False)
                         if r["full_name"] == canonical), None)
        if existing is None:
            allowed = [default_branch] if chosen == default_branch else [default_branch, chosen]
            self.store.add_repository(canonical, default_branch=default_branch,
                                      allowed_branches=allowed)
        else:
            if not existing["enabled"]:
                raise ProjectBindingError("repository is disabled in the allowlist")
            allowed = list(existing.get("allowed_branches") or [])
            permitted = chosen in allowed if allowed else chosen == existing["default_branch"]
            if not permitted:
                self.store.add_repository(
                    canonical, provider=existing["provider"],
                    default_branch=existing["default_branch"],
                    allowed_branches=(allowed or [existing["default_branch"]]) + [chosen])
        return canonical, chosen, default_branch

    def add_project_repository(self, principal: str, project_id: str, *, full_name: Any,
                               branch: Any = None, account_id: str | None = None,
                               use_server_token: bool = False,
                               role_note: Any = None) -> dict[str, Any]:
        """Add one more repository to a project, with its own GitHub account.

        The project keeps its main repository and its own account; this one is
        linked beside them (``account-A/frontend`` next to ``account-B/backend``)
        and read with the identity chosen here:

        * ``account_id`` - that stored account, for this repository only;
        * ``use_server_token`` - the process ``GITHUB_TOKEN``;
        * neither - the project's own identity.

        As in :meth:`connect_repository`, the repository and the branch are
        looked up on GitHub with that identity before anything is written, and
        what it may do there (read, push, private or not) is recorded with the
        link. A project's first repository becomes its main one.
        """
        full_name = full_name.strip() if isinstance(full_name, str) else ""
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", full_name):
            raise ValueError("full_name must be owner/name")
        if branch is not None and not isinstance(branch, str):
            raise ValueError("branch must be a string")
        branch = (branch or "").strip() or None
        self.projects.get(principal, project_id)  # membership; NotFound otherwise
        if account_id is not None and use_server_token:
            raise ValueError("choose a GitHub account or the server token, not both")
        if account_id is not None:
            client = self.github_accounts.client_for_account(principal, account_id)
        elif use_server_token:
            client = self.github
        else:
            client = self.github_for_project(principal, project_id)
        canonical, chosen, default_branch = self._admit_repository(client, full_name, branch)
        from .nexus_binding import _KEEP, check_access
        access = check_access(client, canonical, chosen)
        self.project_repos.link(
            principal, project_id, canonical,
            account_id=account_id if (account_id is not None or use_server_token) else _KEEP,
            branch="" if chosen == default_branch else chosen,
            role_note=role_note if role_note is not None else _KEEP)
        return self.project_repos.record_access(principal, project_id, canonical, access)

    def repository_client(self, project_id: str, full_name: str) -> GitHubClient:
        """The GitHub client one repository of a project is read with: the
        account of its link, else the project's account, else the server's
        token (``ProjectRepositoryService.account_for_repository``)."""
        owner, account_id = self.project_repos.account_for_repository(project_id, full_name)
        if account_id is None:
            return self.github
        return self.github_accounts.client_for_account(owner, account_id)

    def check_project_repository(self, principal: str, project_id: str,
                                 full_name: str) -> dict[str, Any]:
        """Ask GitHub now what this project's identity for *full_name* may do
        with it, and record the answer with the link."""
        from .nexus_binding import check_access
        from .nexus_github_accounts import SecretsError

        link = next((item for item in self.project_repos.list(principal, project_id)
                     if item["full_name"] == full_name), None)
        if link is None:
            raise NotFound("repository not linked to this project")
        try:
            client = self.repository_client(project_id, full_name)
            access = check_access(client, full_name, link["working_branch"])
        except (NotFound, ValueError, SecretsError) as e:
            # The account the link names cannot be used: revoked, or its token
            # cannot be read. That is the answer of the check.
            access = {"readable": False, "push": False, "admin": False, "private": None,
                      "default_branch": None, "branch": link["working_branch"],
                      "branch_exists": None,
                      "error": f"the GitHub account of this repository cannot be used: {e}"}
        return self.project_repos.record_access(principal, project_id, full_name, access)

    def connect_repository(self, principal: str, project_id: str, *, full_name: Any,
                           branch: Any = None, account_id: str | None = None,
                           use_server_token: bool = False) -> dict[str, Any]:
        """Connect a repository and branch to a project, checked against GitHub.

        One explicit action replaces the three calls a client had to make in the
        right order (allowlist, account, binding) and adds the check none of them
        made: the repository and the branch are looked up on GitHub with the
        identity that will read them. A typo or a repository that identity cannot
        see is refused here, not discovered later when a task fails.

        The identity is the caller's explicit choice, and it becomes the
        project's identity for every later read:

        * ``account_id`` - that stored account; it is bound to the project;
        * ``use_server_token`` - the process ``GITHUB_TOKEN``; any bound account
          is unbound, so the project does not keep reading as someone else;
        * neither - whatever the project already uses.

        Every GitHub check happens before the first write. The allowlist is only
        ever extended: an unlisted repository is added, and the chosen branch is
        added to a listed repository when the dispatch gate would otherwise
        refuse it (an empty list there means "default branch only"). Nothing is
        removed, and a disabled repository stays disabled and is refused.
        """
        full_name = full_name.strip() if isinstance(full_name, str) else ""
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", full_name):
            raise ValueError("full_name must be owner/name")
        if branch is not None and not isinstance(branch, str):
            raise ValueError("branch must be a string")
        branch = (branch or "").strip() or None
        self.projects.get(principal, project_id)  # membership; NotFound otherwise
        if account_id is not None and use_server_token:
            raise ValueError("choose a GitHub account or the server token, not both")
        if account_id is not None:
            client = self.github_accounts.client_for_account(principal, account_id)
        elif use_server_token:
            client = self.github
        else:
            client = self.github_for_project(principal, project_id)

        canonical, chosen, _default = self._admit_repository(client, full_name, branch)
        if account_id is not None or use_server_token:
            self.github_accounts.bind_project(principal, project_id, account_id)
        self.project_binding.set_repository(principal, project_id,
                                            full_name=canonical, branch=chosen)
        if account_id is not None or use_server_token:
            # The identity chosen here is the project's, and the main
            # repository is read with it: an account this repository's link
            # carried from an earlier "add" must not go on overriding it.
            self.project_repos.update(principal, project_id, canonical, account_id=None)
        return self.project_binding.get(principal, project_id)

    def cancel_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Stop a workflow for good. Terminal and idempotent.

        The Sr conversation is left alive on purpose: deleting it would destroy
        the transcript the operator needs to see why they stopped the work. Only
        the dispatcher's own state becomes terminal, so nothing is dispatched or
        polled for it again.
        """
        if not UUID_RE.fullmatch(workflow_id):
            raise NotFound(workflow_id)
        return self.store.cancel_workflow(workflow_id)

    def resume_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Reopen a blocked or failed workflow and requeue its retryable tasks."""
        if not UUID_RE.fullmatch(workflow_id):
            raise NotFound(workflow_id)
        return self.store.resume_workflow(workflow_id)

    def retry_task(self, task_id: str) -> dict[str, Any]:
        """Requeue one task for another attempt."""
        if not UUID_RE.fullmatch(task_id):
            raise NotFound(task_id)
        task = self.store.retry_task(task_id)
        return {"task_id": task.id, "state": task.state.value}

    def switch_task_executor(self, principal: str, project_id: str, run_id: str,
                             task_id: str, account_id: str) -> dict[str, Any]:
        """Hand a running task to another OpenHands account (manual switch).

        The task must belong to *run_id* in *project_id* (a non-member gets a
        404). The account must be one of the principal's active accounts. The
        switch is prepared here; the lab worker performs it on its next tick,
        passing the hand-off context. A task that is not in flight is refused.

        The abandoned conversation is **stopped** (best effort) so the old
        executor does not keep writing to the working copy while the new one
        works. Its transcript is kept; only the run loop is stopped. When the
        transport cannot stop a conversation (the Cloud V1 app-conversation
        transport pauses at the sandbox, not the conversation), the stop is
        reported as ``False`` and the branch lock remains the guarantee that two
        executors do not push to one branch at once.
        """
        if not UUID_RE.fullmatch(task_id) or not UUID_RE.fullmatch(account_id):
            raise NotFound("task or account id")
        # Membership + ownership: the task must be in this run and project.
        run = self.lab.get_run(principal, project_id, run_id)
        task = next((t for t in self.lab.list_tasks(principal, project_id, run_id)
                     if str(t["id"]) == task_id), None)
        if task is None:
            raise NotFound("task not found in this run")
        if run["state"] in ("completed", "failed", "cancelled"):
            raise ValueError(f"cannot switch a task of a {run['state']} run")
        account = next((a for a in self.openhands_accounts.list_accounts(principal)
                        if str(a["id"]) == account_id), None)
        if account is None:
            raise NotFound("account not found")
        if account["state"] != "active" or not account.get("enabled", True):
            raise ValueError("the target account is not active and enabled")
        # Read the abandoned conversation before the switch clears it, so the
        # old executor can be stopped rather than merely left unpolled.
        abandoned = task.get("conversation_id")
        from_account = task.get("openhands_account_id")
        updated = self.lab.switch_task_executor(task_id, target_account_id=account_id)
        stopped = self._stop_abandoned_conversation(principal, from_account, abandoned)
        # The store also recorded the conversation in the task's ``abandoned``
        # list, which holds the branch until it is stopped. This path stopped it
        # here, so drop the entry too; when it did not stop (the app transport
        # cannot), leave the entry and let the worker try (it will report the
        # transport cannot stop, and release the branch).
        if stopped and abandoned:
            self.lab.mark_abandoned_stopped(task_id, abandoned)
        return {"task": updated, "target_account": {"id": account_id,
                                                    "label": account["label"]},
                "abandoned_conversation": abandoned, "stopped": stopped}

    def _stop_abandoned_conversation(self, principal: str, account_id: str | None,
                                     conversation_id: str | None) -> bool:
        """Stop the conversation a switch left behind, without deleting it.

        Best effort and never fatal: a switch that has already been recorded must
        not fail because the old conversation could not be stopped. The
        transcript is the record of why the work moved, so the conversation is
        not deleted. Returns whether the transport reported a stop.
        """
        if not conversation_id:
            return False
        try:
            if account_id:
                client = self.openhands_accounts.execution_client_for_account(
                    principal, str(account_id))
            else:
                client = self.client
            stop = getattr(client, "stop_conversation", None)
            return bool(stop(conversation_id)) if stop is not None else False
        except Exception as e:  # noqa: BLE001 - the switch already stands
            log.warning("could not stop abandoned conversation %s: %s",
                        conversation_id, type(e).__name__)
            return False

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
        if not UUID_RE.fullmatch(workflow_id):
            raise NotFound(workflow_id)
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
        if not UUID_RE.fullmatch(workflow_id):
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
                "project_id": workflows.get(workflow_id, {}).get("project_id"),
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

    def openhands_diagnostics(self, principal: str, *, live: bool = False,
                              account_id: str | None = None,
                              project_id: str | None = None) -> dict[str, Any]:
        """Run the OpenHands diagnostic checks and return the honest report.

        Every check says VERIFIED, FAILED, BLOCKED or UNVERIFIED; the report
        never claims a check passed that did not run. ``live`` opts into the
        end-to-end conversation probe (it creates and deletes one conversation).
        """
        from .openhands_diagnostics import OpenHandsDiagnostics

        diagnostics = OpenHandsDiagnostics(
            self.store, accounts=self.openhands_accounts, pool=self.account_pool,
            cloud_client=self.client, providers=self.providers)
        return diagnostics.run(principal, live=live, account_id=account_id,
                               project_id=project_id).as_dict()

    def tool_inventory(self, principal: str, *, live: bool = False,
                       account_id: str | None = None,
                       project_id: str | None = None) -> dict[str, Any]:
        """Which tools are available to NEXUS and which to OpenHands.

        A focused view of section 9 of the acceptance text: the NEXUS surface
        (the MCP tools and the ``sr_`` client tools) and the OpenHands surface
        (the agent's own tool catalog, read from the agent-server and mapped to
        the required capabilities), plus the MCP servers the account has
        configured. It runs exactly the three diagnostics checks
        (``nexus_tools`` / ``openhands_tools`` / ``mcp_configuration``) and
        returns the same honest statuses, so this surface cannot claim more than
        the full report does.

        Reading the agent's catalog on the ``direct`` transport may provision a
        sandbox, so it is opt-in with ``live`` exactly like the conversation
        probe; without it ``openhands_tools`` is UNVERIFIED, never assumed. No
        key or secret is returned.
        """
        from .openhands_diagnostics import OpenHandsDiagnostics, Report

        diagnostics = OpenHandsDiagnostics(
            self.store, accounts=self.openhands_accounts, pool=self.account_pool,
            cloud_client=self.client, providers=self.providers)
        report = Report()
        diagnostics.check_nexus_tools(report)
        diagnostics.check_openhands_tools(report, principal, live=live,
                                          account_id=account_id, project_id=project_id)
        diagnostics.check_mcp_configuration(report, principal, account_id=account_id,
                                            project_id=project_id)
        return report.as_dict()

    def provider_status(self, *, live: bool = False) -> dict[str, Any]:
        """The orchestrator cascade: configuration, and an optional real probe.

        ``live=False`` reports only which providers have a key and the order
        they are tried in — no provider is contacted, no key is returned. With
        ``live=True`` each configured provider is probed with one read-only
        request (see :meth:`Provider.probe`) and earns its own status, so a
        provider is never reported connected on faith. This is deliberately
        separate from the OpenHands account pool: switching a *model* provider
        is not switching an OpenHands *account*.
        """
        configured = self.providers.configured()
        order = [p.name for p in self.providers.providers]
        out: dict[str, Any] = {"order": order, "configured": configured}
        if live:
            out["probes"] = [
                {"provider": r.provider, "model": r.model, "kind": r.kind,
                 "detail": r.detail, "model_available": r.model_available}
                for r in self.providers.probe_all()]
        return out

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
        components = {
            "database": {"ok": store_ok, "error": store_err},
            "openhands": {"ok": cloud_ok, "error": cloud_err},
            "github": {"ok": self.github.configured,
                       "detail": "token configured" if self.github.configured else "no token"},
            "auth": {"ok": self.auth.configured,
                     "detail": "configured" if self.auth.configured
                     else "missing: " + ", ".join(self.auth.missing_settings())},
            "evidence": {"ok": bool(self.evidence_keyring) and self.evidence_signer is not None,
                         "detail": "signer and keyring" if self.evidence_signer
                         else "no signer configured (NEXUS_EVIDENCE_PRIVATE_KEY unset)"},
            "providers": {"ok": bool(self.providers.configured()),
                          "detail": ", ".join(self.providers.configured()) or
                          "no orchestrator provider configured"},
        }
        return {
            "store": {"ok": store_ok, "error": store_err},
            "cloud": {"ok": cloud_ok, "error": cloud_err, "url": self.client.base_url},
            "components": components,
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
                "lab_inline": self.lab_inline,
                "lab_last_tick_at": self.last_lab_tick.get("at"),
            },
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "SrJrDispatcher/2.0"

    def log_message(self, *a: Any) -> None:  # keep the console quiet
        pass

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    #: ``Set-Cookie`` for a session that this request has just extended, until
    #: a response takes it (``_session``, ``_take_renewed_cookie``).
    _renewed_cookie: str | None = None

    # ------------------------------------------------------------- helpers
    def _take_renewed_cookie(self) -> str | None:
        """The renewed session cookie this request owes the browser, once.

        ``_json``, ``_html`` and ``_redirect`` send it: the answers a cache
        does not keep (the first two say ``no-store``; a 303 is not stored
        unless it says it may be). A session cookie must never ride on an
        answer that a cache may hand to somebody else, so ``_static`` does not
        carry it - and it serves only what needs no session. A renewal that
        meets no such answer (a file download) is not lost for long: the next
        one is due ``AuthService.renew_step`` later.
        """
        cookie, self._renewed_cookie = self._renewed_cookie, None
        return cookie

    def _json(self, code: int, payload: Any, *, cookie: str | None = None) -> None:
        # What the handler says about the cookie itself (login, logout) stands.
        renewed = self._take_renewed_cookie()
        cookie = cookie or renewed
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code: int, text: str, *, cookie: str | None = None) -> None:
        renewed = self._take_renewed_cookie()
        cookie = cookie or renewed
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _static(
        self, code: int, text: str | bytes, content_type: str, *,
        cache_control: str = "public, max-age=300",
    ) -> None:
        body = text.encode() if isinstance(text, str) else text
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, to: str, *, cookie: str | None = None) -> None:
        # ``/login`` sends a signed-in owner on to the studio: an address a
        # browser remembers, so this may well be the first request of the day.
        renewed = self._take_renewed_cookie()
        cookie = cookie or renewed
        self.send_response(303)
        self.send_header("Location", to)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    @staticmethod
    def _content_disposition(filename: str) -> str:
        """A safe ``Content-Disposition`` value for an arbitrary filename.

        The filename is attacker-controlled, so it is reduced to a conservative
        ASCII token for ``filename=`` and also offered as ``filename*`` UTF-8,
        which every current browser prefers. Quoting and dropping path
        separators/control characters prevents a header-injection or a path hint
        from reaching the client.
        """
        base = (filename or "download").replace("\\", "/").rsplit("/", 1)[-1]
        ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_"
                             for c in base) or "download"
        from urllib.parse import quote

        return (f"attachment; filename=\"{ascii_name}\"; "
                f"filename*=UTF-8''{quote(base)}")

    def _attachment(self, code: int, body: bytes, content_type: str,
                    filename: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Disposition", self._content_disposition(filename))
        # Uploaded content is untrusted; never let a browser cache or sniff it.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self, max_bytes: int | None = None) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length", 0))
        if n < 0:
            raise ValueError("invalid or oversized request body")
        if max_bytes is not None and n > max_bytes:
            # Refused unread. What the client is still sending is dealt with
            # after the answer has gone out (see ``_linger``).
            self._unread = n if n <= 4 * max_bytes else 0
            raise BodyTooLarge(
                f"the request body is {n} bytes; this route accepts up to {max_bytes}")
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("body is not valid JSON")

    #: How long a refused body is still read after the answer: in all, and
    #: while nothing arrives.
    LINGER_SECONDS = 15.0
    LINGER_IDLE_SECONDS = 2.0

    def _linger(self) -> None:
        """Let the client finish sending a body that was refused unread.

        The answer is already written. Closing the socket while the rest of
        the body is still arriving resets the connection, and the client then
        reports a network failure instead of the answer it was sent - a browser
        shows "network error" where it should say why the text was refused. So
        the answer is ended first (the client is not kept waiting for it), and
        then what the client goes on sending is read and dropped: in small
        pieces, kept nowhere, for a bounded time, and not at all for a body
        several times over the limit. The connection is closed either way.
        """
        left = getattr(self, "_unread", 0)
        self._unread = 0
        self.close_connection = True
        if left <= 0:
            return
        deadline = time.monotonic() + self.LINGER_SECONDS
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)       # the answer is complete
            self.connection.settimeout(self.LINGER_IDLE_SECONDS)
            while left > 0 and time.monotonic() < deadline:
                chunk = self.rfile.read1(min(left, 64 * 1024))
                if not chunk:
                    return
                left -= len(chunk)
        except (OSError, ValueError):
            return

    def _raw_body(self, max_bytes: int) -> bytes:
        """Read the request body as raw bytes, capped at *max_bytes*.

        Uploads are not JSON, so they cannot go through :meth:`_body`. The cap
        is enforced against ``Content-Length`` *before* reading, so an oversized
        upload is rejected without buffering it.
        """
        raw_len = self.headers.get("Content-Length", "")
        try:
            n = int(raw_len)
        except (TypeError, ValueError):
            raise ValueError("missing or invalid Content-Length")
        if n < 0:
            raise ValueError("invalid Content-Length")
        if n > max_bytes:
            raise ValueError(f"upload exceeds the {max_bytes} byte limit")
        return self.rfile.read(n) if n else b""

    def _form(self) -> dict[str, str]:
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode() if n else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def _client_ip(self) -> str:
        # Trust only the hops we actually run behind. The leftmost value is
        # client-supplied and must never key a security decision; see
        # ``client_ip_from_values`` for why the rightmost trusted hop is the one
        # used, and why every header line is considered (not just the first).
        return client_ip_from_values(
            self.headers.get_all("X-Forwarded-For"),
            self.client_address[0] if self.client_address else None,
            self.app.trusted_hops,
        )

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
        session = self.app.auth.resolve(token)
        ttl = getattr(session, "cookie_ttl", None)
        if ttl:
            # Using the session has just extended it. The browser's copy of the
            # cookie has to live as long, or the browser drops it while the
            # server would still honour it - and the owner signs in again.
            self._renewed_cookie = session_cookie_header(
                session.token, ttl=ttl, secure=self._cookie_secure())
        return session

    def _bearer_ok(self) -> bool:
        """Machine authentication for the API, unchanged from before.

        Kept separate from the browser session so that adding login never
        weakens (or is weakened by) the existing bearer-token protection.
        """
        token = getattr(self.app, "token", None)
        if not token:
            return False
        # Compared in constant time: how long a mismatch takes must say nothing
        # about how much of the token was right.
        return hmac.compare_digest(
            self.headers.get("Authorization", "").encode("utf-8", "replace"),
            f"Bearer {token}".encode("utf-8", "replace"))

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

    def _project_principal(self) -> str:
        # The legacy bearer is an owner-level credential, not a scoped token.
        # Bind it to the configured login so CLI and browser share projects.
        session = self._session()
        if session is not None:
            return principal_for_session(session.username)
        return principal_for_session(self.app.auth.username)

    @staticmethod
    def _project_path(path: str) -> tuple[str, str] | None:
        parts = path.strip("/").split("/")
        if len(parts) not in (4, 5) or parts[:3] != ["api", "v1", "projects"]:
            return None
        if not UUID_RE.fullmatch(parts[3]):
            return None
        return parts[3], parts[4] if len(parts) == 5 else ""

    @staticmethod
    def _project_nested(path: str) -> tuple[str, str, str | None] | None:
        """Parse ``/api/v1/projects/<uuid>/<resource>[/<id>]``.

        Returns ``(project_id, resource, item_id_or_None)``. Only the resources
        the MVP exposes are accepted; anything else is None (a 404 upstream).
        """
        parts = path.strip("/").split("/")
        if len(parts) not in (5, 6) or parts[:3] != ["api", "v1", "projects"]:
            return None
        if not UUID_RE.fullmatch(parts[3]):
            return None
        resource = parts[4]
        if resource not in ("repositories", "tasks", "workflows", "evidence", "executors"):
            return None
        item = parts[5] if len(parts) == 6 else None
        if item is not None and not UUID_RE.fullmatch(item):
            # The repositories and executors collections accept a non-uuid
            # action segment (``remove``); anything else must be a uuid.
            allowed = ((resource == "repositories" and item == "remove")
                       or (resource == "executors" and item == "remove"))
            if not allowed:
                return None
        return parts[3], resource, item

    @staticmethod
    def _file_path(path: str) -> tuple[str, str, bool] | None:
        parts = path.strip("/").split("/")
        if (len(parts) not in (6, 7) or parts[:3] != ["api", "v1", "projects"]
                or parts[4] != "files" or not UUID_RE.fullmatch(parts[3])
                or not UUID_RE.fullmatch(parts[5]) or (len(parts) == 7 and parts[6] != "delete")):
            return None
        return parts[3], parts[5], len(parts) == 7

    def _download(self, name: str, content: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", "attachment; filename=download; filename*=UTF-8''" +
                         urllib.parse.quote(name, safe=""))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _file_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length") is None:
            raise ValueError("Content-Length required; transfer encoding is not supported")
        n = int(self.headers["Content-Length"])
        if n < 0 or n > MAX_FILE_SIZE:
            raise ValueError("file exceeds 5 MiB")
        content = self.rfile.read(n)
        if len(content) != n:
            raise ValueError("incomplete file")
        return content

    @staticmethod
    def _project_subpath(path: str) -> tuple[str, list[str]] | None:
        """Split a ``/api/v1/projects/{id}/...`` path into (project_id, rest).

        Unlike :meth:`_project_path` this keeps every trailing segment, so the
        file-lab, workspace, voice and action routes can dispatch on ``rest``.
        The project id is validated as a UUID before any query runs, so a
        malformed id is a 404 rather than a database error.
        """
        parts = path.strip("/").split("/")
        if len(parts) < 4 or parts[:3] != ["api", "v1", "projects"]:
            return None
        if not UUID_RE.fullmatch(parts[3]):
            return None
        return parts[3], parts[4:]

    def _get_project_resource(self, path: str, project_id: str, principal: str):
        """Dispatch the deeper project GET routes, or ``None`` if unmatched.

        Returns an HTTP response tuple ``(status, payload, raw)`` where ``raw``
        is ``(bytes, content_type)`` for a file download, or ``None`` when the
        payload is JSON. Keeping the raw case explicit avoids base64-inflating a
        binary file just to send it.
        """
        parsed = self._project_subpath(path)
        if parsed is None:
            return None
        _, rest = parsed
        files = self.app.files
        # GET /api/v1/projects/{id}/files
        if rest == ["files"]:
            return (200, {"files": files.list_files(principal, project_id)}, None)
        # GET /api/v1/projects/{id}/files/{file_id}
        if len(rest) == 2 and rest[0] == "files" and UUID_RE.fullmatch(rest[1]):
            return (200, {"file": files.preview(principal, project_id, rest[1])}, None)
        # GET /api/v1/projects/{id}/files/{file_id}/download
        if (len(rest) == 3 and rest[0] == "files" and UUID_RE.fullmatch(rest[1])
                and rest[2] == "download"):
            row = files.get_file(principal, project_id, rest[1])
            return (200, None, (bytes(row["data"]), row["content_type"],
                                row["filename"]))
        # GET /api/v1/projects/{id}/workspaces
        if rest == ["workspaces"]:
            return (200, {"workspaces": files.list_workspaces(principal, project_id)}, None)
        # GET /api/v1/projects/{id}/workspaces/{ws}
        if len(rest) == 2 and rest[0] == "workspaces" and UUID_RE.fullmatch(rest[1]):
            return (200, {"files": files.list_workspace_files(principal, project_id, rest[1])}, None)
        # GET /api/v1/projects/{id}/workspaces/{ws}/diff
        if (len(rest) == 3 and rest[0] == "workspaces" and UUID_RE.fullmatch(rest[1])
                and rest[2] == "diff"):
            return (200, {"diff": files.workspace_diff(principal, project_id, rest[1])}, None)
        # GET /api/v1/projects/{id}/workspaces/{ws}/files/{path...}
        if len(rest) >= 4 and rest[0] == "workspaces" and UUID_RE.fullmatch(rest[1]) \
                and rest[2] == "files":
            rel = "/".join(rest[3:])
            return (200, {"file": files.get_workspace_file(principal, project_id, rest[1], rel)}, None)
        # GET /api/v1/projects/{id}/voice
        if rest == ["voice"]:
            return (200, {"voice_notes": self.app.voice.list_notes(principal, project_id)}, None)
        # GET /api/v1/projects/{id}/voice/{note_id}/audio
        if (len(rest) == 3 and rest[0] == "voice" and UUID_RE.fullmatch(rest[1])
                and rest[2] == "audio"):
            row = self.app.voice.get_audio(principal, project_id, rest[1])
            return (200, None, (bytes(row["data"]), row["content_type"], "audio.webm"))
        # GET /api/v1/projects/{id}/actions
        if rest == ["actions"]:
            # Pending actions are actor-scoped, but the route names a project;
            # require membership so a foreign project id is a 404 rather than a
            # window into another user's proposals.
            try:
                self.app.projects.get(principal, project_id)
            except NotFound:
                return None
            return (200, {"actions": self.app.actions.list_pending(principal)}, None)
        # GET /api/v1/projects/{id}/actions/{action_id}
        if len(rest) == 2 and rest[0] == "actions" and UUID_RE.fullmatch(rest[1]):
            return (200, {"action": self.app.actions.get(principal, rest[1])}, None)
        # GET /api/v1/projects/{id}/runs            (the autonomous lab)
        if rest == ["runs"]:
            return (200, {"runs": self.app.lab.list_runs(principal, project_id)}, None)
        # GET /api/v1/projects/{id}/runs/{run_id}
        if len(rest) == 2 and rest[0] == "runs" and UUID_RE.fullmatch(rest[1]):
            run = self.app.lab.get_run(principal, project_id, rest[1])
            tasks = self.app.lab.list_tasks(principal, project_id, rest[1])
            # The report is part of the run's record: the same text that was
            # delivered to the chat the run came from, so a person reading the
            # run sees it too. ``None`` until the run has completed. And who
            # ran what: one line per conversation a task was given. ``switches``
            # names every manual/failover hand-off and ``events`` the run's own
            # timeline, so the interface can show the current executor, the
            # conversation ids, the switches and the last events without a
            # second call.
            return (200, {"run": run, "tasks": tasks,
                          "report": self.app.reports.for_run(rest[1]),
                          "executions": self.app.lab.executions(rest[1]),
                          "switches": self.app.lab.run_handoffs(rest[1]),
                          "events": self.app.lab.run_events(rest[1]),
                          "stops": self.app.lab.stops(rest[1])}, None)
        # GET /api/v1/projects/{id}/runs/{run_id}/report
        if (len(rest) == 3 and rest[0] == "runs" and UUID_RE.fullmatch(rest[1])
                and rest[2] == "report"):
            return (200, {"report": self.app.reports.get(principal, project_id, rest[1])},
                    None)
        # GET /api/v1/projects/{id}/specs/{spec_id}[/content|sections|requirements|stages]
        if rest and rest[0] == "specs" and len(rest) in (2, 3) and UUID_RE.fullmatch(rest[1]):
            return self._get_spec(principal, project_id, rest[1], rest[2] if len(rest) == 3 else "")
        # GET /api/v1/projects/{id}/context          (current project context)
        if rest == ["context"]:
            return (200, {"context": self.app.context.current(principal, project_id)}, None)
        # GET /api/v1/projects/{id}/memory?q=...
        if rest and rest[0] == "memory" and len(rest) == 1:
            query = None
            if "?" in path:
                from urllib.parse import parse_qs
                query = (parse_qs(path.split("?", 1)[1]).get("q") or [None])[0]
            return (200, {"memory": self.app.memory.retrieve(principal, project_id, query=query)}, None)
        # GET /api/v1/projects/{id}/notifications
        if rest == ["notifications"]:
            return (200, {"notifications": self.app.lab.notifications(principal, project_id)}, None)
        # GET /api/v1/projects/{id}/conversations[?q=&archived=1]
        if rest and rest[0] == "conversations" and len(rest) == 1:
            # The query lives on the raw request target, not the stripped path.
            from urllib.parse import parse_qs
            params = parse_qs(urlparse(self.path).query)
            query = (params.get("q") or [None])[0]
            include_archived = (params.get("archived") or ["0"])[0] in ("1", "true")
            return (200, {"conversations": self.app.conversations.list(
                principal, project_id, include_archived=include_archived,
                query=query)}, None)
        # GET /api/v1/projects/{id}/conversations/{cid}
        if len(rest) == 2 and rest[0] == "conversations" and UUID_RE.fullmatch(rest[1]):
            return (200, {"conversation": self.app.conversations.get(
                principal, project_id, rest[1])}, None)
        # GET /api/v1/projects/{id}/conversations/{cid}/messages
        if (len(rest) == 3 and rest[0] == "conversations" and UUID_RE.fullmatch(rest[1])
                and rest[2] == "messages"):
            return (200, {"messages": self.app.conversations.list_messages(
                principal, project_id, rest[1])}, None)
        # GET /api/v1/projects/{id}/agents  (agent sessions)
        if rest == ["agents"]:
            return (200, {"sessions": self.app.conversations.list_agent_sessions(
                principal, project_id)}, None)
        # GET /api/v1/projects/{id}/binding  (repository/branch/account binding)
        if rest == ["binding"]:
            return (200, {"binding": self.app.project_binding.get(principal, project_id)}, None)
        # GET /api/v1/projects/{id}/github/repositories[/{owner}/{name}[/section]]
        if rest and rest[0] == "github" and len(rest) >= 2 and rest[1] == "repositories":
            # The GitHub views read no project data, but the route still names a
            # project, so membership is verified: a foreign project id must not
            # become a way to probe the account's repositories.
            try:
                self.app.projects.get(principal, project_id)
                client = self.app.github_for_project(principal, project_id)
            except NotFound:
                return None
            except ValueError as e:
                # The bound account is revoked or was rejected by GitHub.
                return (400, {"error": f"github account: {e}"}, None)
            return self._get_github(principal, rest, client)
        return None

    def _get_spec(self, principal: str, project_id: str, spec_id: str, part: str):
        """A stored task text ("specification") and what was derived from it.

        * ``""``             - sizes, SHA-256, planning state and progress, how
          the pieces of the text are accounted for by the plan, the run;
        * ``content``        - the text itself, byte for byte as it was sent
          (UTF-8). It is checked against its SHA-256 before it is served;
        * ``sections``       - the outline;
        * ``requirements``   - the addressable pieces (``?offset=&limit=&section=``);
        * ``stages``         - the parts it is planned in, each with its state
          and, once planned, its tasks.
        """
        specs = self.app.specs
        if part == "":
            spec = specs.get(principal, project_id, spec_id)
            spec["coverage"] = specs.coverage_internal(spec_id)
            return (200, {"spec": spec}, None)
        if part == "content":
            try:
                spec, text = specs.content(principal, project_id, spec_id)
            except SpecCorrupted as e:
                return (500, {"error": str(e)}, None)
            return (200, None, (text.encode("utf-8"), "text/plain; charset=utf-8",
                                f"spec-{spec['sha256'][:12]}.md"))
        if part == "sections":
            return (200, {"sections": specs.sections(principal, project_id, spec_id)}, None)
        if part == "stages":
            return (200, {"stages": specs.stages(principal, project_id, spec_id)}, None)
        if part == "requirements":
            params = urllib.parse.parse_qs(urlparse(self.path).query)
            try:
                offset = int((params.get("offset") or ["0"])[0])
                limit = int((params.get("limit") or ["500"])[0])
                section = params.get("section")
                rows = specs.requirements(
                    principal, project_id, spec_id, offset=offset, limit=limit,
                    section=int(section[0]) if section else None)
            except ValueError as e:
                return (400, {"error": str(e) if "must be" in str(e)
                              else "offset, limit and section must be whole numbers"}, None)
            return (200, {"requirements": rows, "offset": offset}, None)
        return None

    def _github_account_get(self, path: str):
        """GET a GitHub account's repositories using *that account's* token.

        This is what makes the visual repository picker multi-account: the list
        comes from the named account, never from the process-wide token. The PAT
        never leaves the server.
        """
        from urllib.parse import unquote

        parts = [unquote(part) for part in path.strip("/").split("/")]
        principal = self._project_principal()
        if len(parts) < 4 or not UUID_RE.fullmatch(parts[3]):
            return self._json(404, {"error": "not found"})
        if len(parts) == 4:  # /api/github/accounts/{id}
            account = self._find_account(self.app.github_accounts.list_accounts(principal),
                                         parts[3])
            return self._json(200, {"account": account})
        listing = len(parts) == 5 and parts[4] == "repositories"
        # /api/github/accounts/{id}/repositories/{owner}/{name}/branches - what
        # the repository dialog offers before the project is bound to anything.
        branches = len(parts) == 8 and parts[4] == "repositories" and parts[7] == "branches"
        if not (listing or branches):
            return self._json(404, {"error": "not found"})
        try:
            client = self.app.github_accounts.client_for_account(principal, parts[3])
            if branches:
                return self._json(200, {"branches": client.list_branches(
                    f"{parts[5]}/{parts[6]}")})
            repos = client.list_repositories()
        except NotFound:
            return self._json(404, {"error": "account not found"})
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except GitHubAuthError as e:
            return self._json(400, {"error": f"github auth: {e}"})
        except GitHubUnavailable as e:
            return self._json(503, {"error": f"github unavailable: {e}"})
        except GitHubError as e:
            return self._json(502, {"error": f"github: {e}"})
        return self._json(200, {"repositories": [r.as_dict() for r in repos],
                                "count": len(repos)})

    def _openhands_account_get(self, path: str):
        """GET one OpenHands account, or its discovered capabilities.

        Capabilities come from the named account's own key, read-only, and never
        include a secret. The API key never leaves the server.
        """
        parts = path.strip("/").split("/")
        principal = self._project_principal()
        if len(parts) < 4 or not UUID_RE.fullmatch(parts[3]):
            return self._json(404, {"error": "not found"})
        account = self._find_account(self.app.openhands_accounts.list_accounts(principal),
                                     parts[3])
        if len(parts) == 4:
            return self._json(200, {"account": account})
        if len(parts) == 5 and parts[4] == "capabilities":
            try:
                client = self.app.openhands_accounts.client_for_account(principal, parts[3])
                caps = client.account_capabilities()
            except NotFound:
                return self._json(404, {"error": "account not found"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except CloudError as e:
                return self._json(502, {"error": f"cloud: {e}"})
            return self._json(200, {"capabilities": caps})
        if len(parts) == 5 and parts[4] == "errors":
            try:
                errors = self.app.openhands_accounts.error_history(principal, parts[3])
            except NotFound:
                return self._json(404, {"error": "account not found"})
            return self._json(200, {"errors": errors})
        # What still leans on the connection: asked before it is removed.
        if len(parts) == 5 and parts[4] == "dependencies":
            return self._json(200, {"dependencies": self.app.openhands_accounts.dependencies(
                principal, parts[3])})
        # Which tasks it ran, newest first: the history of its use.
        if len(parts) == 5 and parts[4] == "executions":
            return self._json(200, {"executions": self.app.openhands_accounts.usage(
                principal, parts[3])})
        return self._json(404, {"error": "not found"})

    @staticmethod
    def _find_account(accounts: list[dict], account_id: str) -> dict:
        for account in accounts:
            if str(account.get("id")) == account_id:
                return account
        raise NotFound("account not found")

    def _github_account_post(self, path: str, body: dict):
        """Actions on a GitHub account: verify / rotate / revoke / default."""
        parts = path.strip("/").split("/")
        principal = self._project_principal()
        account_id, action = parts[3], (parts[4] if len(parts) > 4 else "")
        if not UUID_RE.fullmatch(account_id):
            return self._json(404, {"error": "not found"})
        try:
            if action == "verify":
                return self._json(200, {"account": self.app.github_accounts.verify(
                    principal, account_id)})
            if action == "rotate":
                return self._json(200, {"account": self.app.github_accounts.rotate(
                    principal, account_id, token=body.get("token"))})
            if action == "revoke":
                return self._json(200, {"account": self.app.github_accounts.revoke(
                    principal, account_id)})
        except NotFound:
            return self._json(404, {"error": "account not found"})
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        return self._json(404, {"error": "not found"})

    def _openhands_account_post(self, path: str, body: dict):
        """Actions on an OpenHands account: verify / rotate / revoke / default.

        Pool actions (enable / disable / priority / remove) go through the pool
        service, so the selection policy and the account controls stay together.
        """
        from .nexus_openhands_accounts import _UNSET
        from .openhands_pool import PoolError

        parts = path.strip("/").split("/")
        principal = self._project_principal()
        account_id, action = parts[3], (parts[4] if len(parts) > 4 else "")
        if not UUID_RE.fullmatch(account_id):
            return self._json(404, {"error": "not found"})
        try:
            if action == "verify":
                return self._json(200, {"account": self.app.openhands_accounts.verify(
                    principal, account_id)})
            if action == "connection":
                return self._json(200, {"account": self.app.openhands_accounts.check_connection(
                    principal, account_id)})
            if action == "edit":
                return self._json(200, {"account": self.app.openhands_accounts.edit(
                    principal, account_id,
                    label=body.get("label"),
                    base_url=(body.get("base_url") or None),
                    transport=(body.get("transport") or None),
                    default_model=body.get("default_model", _UNSET),
                    max_concurrent_tasks=body.get("max_concurrent_tasks", _UNSET),
                    daily_conversation_limit=body.get(
                        "daily_conversation_limit", _UNSET),
                    # New credentials: checked together with the (new) URL
                    # before anything is saved. Never echoed back.
                    api_key=(body.get("api_key") or None))})
            if action == "rotate":
                return self._json(200, {"account": self.app.openhands_accounts.rotate(
                    principal, account_id, api_key=body.get("api_key"))})
            if action == "rename":
                # One way to change a connection: a rename is an edit of its name.
                return self._json(200, {"account": self.app.openhands_accounts.edit(
                    principal, account_id, label=body.get("label"))})
            if action == "revoke":
                return self._json(200, {"account": self.app.openhands_accounts.revoke(
                    principal, account_id)})
            if action == "default":
                return self._json(200, {"account": self.app.openhands_accounts.set_default(
                    principal, account_id)})
            if action == "enable":
                return self._json(200, {"account": self.app.account_pool.set_enabled(
                    principal, account_id, bool(body.get("enabled", True)))})
            if action == "priority":
                return self._json(200, {"account": self.app.account_pool.set_priority(
                    principal, account_id, body.get("priority"))})
            if action == "remove":
                self.app.account_pool.remove(principal, account_id,
                                             force=bool(body.get("force")))
                return self._json(200, {"removed": account_id})
        except NotFound:
            return self._json(404, {"error": "account not found"})
        except PoolError as e:
            answer: dict[str, Any] = {"error": str(e)}
            if action == "remove":
                # What stands in the way, so that the interface can say it.
                try:
                    answer["dependencies"] = self.app.openhands_accounts.dependencies(
                        principal, account_id)
                except NotFound:
                    pass
            return self._json(409, answer)
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        return self._json(404, {"error": "not found"})

    def _get_github(self, principal: str, rest: list[str], client: GitHubClient):
        """Read-only GitHub views. Never writes and never returns the token.

        *client* is the project's own (see :meth:`App.github_for_project`), so a
        project bound to an account reads with that account, not with whatever
        ``GITHUB_TOKEN`` the process happens to hold.
        """
        from urllib.parse import parse_qs, unquote

        # The path segments arrive percent-encoded; decode each so a Cyrillic
        # owner, name, branch or file path reaches the client as real text.
        rest = [unquote(part) for part in rest]
        query = parse_qs(urlparse(self.path).query)
        ref = (query.get("ref") or [None])[0]
        try:
            if len(rest) == 2:
                repos = client.list_repositories()
                return (200, {"repositories": [r.as_dict() for r in repos],
                              "count": len(repos)}, None)
            if len(rest) >= 4:
                full_name = f"{rest[2]}/{rest[3]}"
                if len(rest) == 4:
                    return (200, {"repository": client.get_repository(full_name)}, None)
                section = rest[4]
                if section == "branches":
                    return (200, {"branches": client.list_branches(full_name)}, None)
                if section == "commits":
                    return (200, {"commits": client.list_commits(
                        full_name, branch=ref)}, None)
                if section == "issues":
                    return (200, {"issues": client.list_issues(full_name)}, None)
                if section == "pulls":
                    return (200, {"pulls": client.list_pulls(full_name)}, None)
                # The IDE view: the file tree and one file's text, at a ref.
                if section == "tree":
                    return (200, {"tree": client.list_tree(full_name, branch=ref)}, None)
                if section == "contents" and len(rest) >= 5:
                    path = "/".join(rest[5:])
                    found = client.read_text_file(full_name, path, branch=ref)
                    if found is None:
                        return (404, {"error": "file not found or binary"}, None)
                    # ``truncated`` lets the viewer say that it shows only the
                    # beginning, instead of presenting a cut file as the whole.
                    return (200, {"path": path, "content": found["content"], "ref": ref,
                                  "truncated": found["truncated"]}, None)
        except GitHubAuthError as e:
            return (400, {"error": f"github auth: {e}"}, None)
        except GitHubUnavailable as e:
            return (503, {"error": f"github unavailable: {e}"}, None)
        except GitHubError as e:
            return (502, {"error": f"github: {e}"}, None)
        except ValueError as e:
            return (400, {"error": str(e)}, None)
        return None

    def _post_project_resource(self, path: str, principal: str, body: dict) -> bool:
        """Dispatch the deeper project POST routes.

        Returns ``True`` when a route matched and its response was written, or
        ``False`` when nothing matched. ``_json`` itself returns ``None``, so the
        two are kept separate: treating ``_json``'s return value as the "handled"
        signal made every POST fall through to a 404 after already sending a 201.
        """
        parsed = self._project_subpath(path)
        if parsed is None:
            return False
        project_id, rest = parsed
        files = self.app.files
        # POST /api/v1/projects/{id}/workspaces/from-file
        if rest == ["workspaces", "from-file"]:
            file_id = body.get("file_id")
            if not isinstance(file_id, str) or not UUID_RE.fullmatch(file_id):
                self._json(400, {"error": "file_id must be a uuid"})
                return True
            workspace = files.extract_into_workspace(
                principal, project_id, name=body.get("name"), file_id=file_id)
            self._json(201, {"workspace": workspace})
            return True
        # POST /api/v1/projects/{id}/workspaces
        if rest == ["workspaces"]:
            self._json(201, {"workspace": files.create_workspace(
                principal, project_id, name=body.get("name"))})
            return True
        # POST /api/v1/projects/{id}/workspaces/{ws}/files
        if (len(rest) == 3 and rest[0] == "workspaces" and UUID_RE.fullmatch(rest[1])
                and rest[2] == "files"):
            self._json(200, {"file": files.put_workspace_file(
                principal, project_id, rest[1], path=body.get("path"),
                text=body.get("text"))})
            return True
        # POST /api/v1/projects/{id}/actions  (propose)
        if rest == ["actions"]:
            action = self.app.actions.propose(
                principal, body.get("action"), body.get("payload") or {},
                project_id=project_id)
            self._json(201, {"action": action})
            return True
        # POST /api/v1/projects/{id}/actions/{id}/confirm | cancel
        if (len(rest) == 3 and rest[0] == "actions" and UUID_RE.fullmatch(rest[1])
                and rest[2] in ("confirm", "cancel")):
            action_id = rest[1]
            if rest[2] == "cancel":
                self._json(200, {"action": self.app.actions.cancel(principal, action_id)})
            else:
                self._confirm_action(principal, project_id, action_id, body)
            return True
        # POST /api/v1/projects/{id}/github/repositories  (propose create)
        if rest == ["github", "repositories"]:
            name = (body.get("name") or "").strip()
            private = body.get("private", True)
            action = self.app.actions.propose(
                principal, "github.create_repository",
                {"name": name, "private": bool(private),
                 "description": (body.get("description") or "")[:350]},
                project_id=project_id)
            self._json(201, {"action": action})
            return True
        # POST /api/v1/projects/{id}/github/repositories/{owner}/{name}/visibility
        if (len(rest) == 5 and rest[0] == "github" and rest[1] == "repositories"
                and rest[4] == "visibility"):
            full_name = f"{rest[2]}/{rest[3]}"
            private = body.get("private")
            if not isinstance(private, bool):
                self._json(400, {"error": "private must be a boolean"})
                return True
            action = self.app.actions.propose(
                principal, "github.set_visibility",
                {"full_name": full_name, "private": private},
                project_id=project_id)
            self._json(201, {"action": action})
            return True
        # POST /api/v1/projects/{id}/executors/check
        #   Check every executor of the project now, each with its own key.
        if rest == ["executors", "check"]:
            self._json(200, self.app.project_executors.check(principal, project_id))
            return True
        # POST /api/v1/projects/{id}/executors/{account_id}/{enable|disable|remove|primary}
        if (len(rest) == 3 and rest[0] == "executors" and UUID_RE.fullmatch(rest[1])
                and rest[2] in ("enable", "disable", "remove", "primary")):
            account_id = rest[1]
            try:
                if rest[2] == "remove":
                    out = self.app.project_executors.remove(principal, project_id, account_id)
                elif rest[2] == "primary":
                    out = self.app.project_executors.make_primary(
                        principal, project_id, account_id)
                else:
                    out = self.app.project_executors.set_enabled(
                        principal, project_id, account_id, rest[2] == "enable")
                out["executor"] = next((item for item in out["executors"]
                                        if item["account_id"] == account_id), None)
                self._json(200, out)
            except NotFound as e:
                self._json(404, {"error": str(e) or "not found"})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            return True
        # POST /api/v1/projects/{id}/repositories/check
        #   Ask GitHub about every repository of the project, each with the
        #   identity that reads it.
        if rest == ["repositories", "check"]:
            checked = [self.app.check_project_repository(principal, project_id,
                                                         item["full_name"])
                       for item in self.app.project_repos.list(principal, project_id)]
            self._json(200, {"repositories": checked})
            return True
        # POST /api/v1/projects/{id}/repositories/{owner}/{name}/{action}
        #   primary            make it the project's main repository
        #   account            {"account_id": uuid|null}: who reads it
        #   update             {"branch", "role_note", "label", "enabled", "account_id"}
        #   enable | disable   the link's own switch
        #   check              ask GitHub what its identity may do with it
        if (len(rest) == 4 and rest[0] == "repositories"
                and rest[3] in ("primary", "account", "update", "enable", "disable", "check")):
            from .nexus_binding import _KEEP
            full_name, action = f"{rest[1]}/{rest[2]}", rest[3]
            account_id = body.get("account_id")
            if account_id is not None and not (isinstance(account_id, str)
                                               and UUID_RE.fullmatch(account_id)):
                self._json(400, {"error": "account_id must be a uuid or null"})
                return True
            repos = self.app.project_repos
            try:
                if action == "primary":
                    link = repos.set_primary(principal, project_id, full_name)
                elif action == "account":
                    link = repos.set_account(principal, project_id, full_name, account_id)
                elif action in ("enable", "disable"):
                    link = repos.update(principal, project_id, full_name,
                                        enabled=action == "enable")
                elif action == "check":
                    link = self.app.check_project_repository(principal, project_id,
                                                             full_name)
                else:
                    link = repos.update(
                        principal, project_id, full_name,
                        account_id=account_id if "account_id" in body else _KEEP,
                        branch=body.get("branch", _KEEP),
                        role_note=body.get("role_note", _KEEP),
                        label=body.get("label", _KEEP),
                        enabled=body.get("enabled", _KEEP))
            except NotFound as e:
                self._json(404, {"error": str(e) or "not found"})
                return True
            except (ProjectBindingError, ValueError) as e:
                self._json(400, {"error": str(e)})
                return True
            self._json(200, {"repository": link})
            return True
        # POST /api/v1/projects/{id}/context/refresh  (analyze the repository)
        if rest == ["context", "refresh"]:
            self._lab_refresh_context(principal, project_id, body)
            return True
        # POST /api/v1/projects/{id}/goals/{gid}/run  (start an autonomous run)
        if (len(rest) == 3 and rest[0] == "goals" and UUID_RE.fullmatch(rest[1])
                and rest[2] == "run"):
            self._lab_start_run(principal, project_id, rest[1], body)
            return True
        # POST /api/v1/projects/{id}/runs/{run_id}/{pause|resume|cancel|hide}
        #   cancel is durable and idempotent: the run is settled and hidden from
        #   the active lists at once, and the loop signals the executor. A
        #   second cancel is a no-op, never an error.
        if (len(rest) == 3 and rest[0] == "runs" and UUID_RE.fullmatch(rest[1])
                and rest[2] in ("pause", "resume", "cancel", "hide")):
            if rest[2] == "hide":
                self._json(200, {"run": self.app.lab.set_hidden(
                    principal, project_id, rest[1], True)})
                return True
            # ``cancelled`` settles the run, takes it out of the lists and
            # leaves a stop signal for every conversation at work for it - all
            # in one transaction (``LabStore._apply_run_state``); the loop
            # delivers the signals. A second cancel changes nothing.
            state = {"pause": "paused", "resume": "running",
                     "cancel": "cancelled"}[rest[2]]
            self._json(200, {"run": self.app.lab.set_run_state(
                principal, project_id, rest[1], state)})
            return True
        # POST /api/v1/projects/{id}/runs/{run_id}/tasks/{task_id}/switch
        #   {"account_id": "<uuid>"}  — hand the task to another account.
        if (len(rest) == 5 and rest[0] == "runs" and UUID_RE.fullmatch(rest[1])
                and rest[2] == "tasks" and UUID_RE.fullmatch(rest[3])
                and rest[4] == "switch"):
            account_id = body.get("account_id")
            if not (isinstance(account_id, str) and UUID_RE.fullmatch(account_id)):
                self._json(400, {"error": "account_id must be a uuid"})
                return True
            try:
                result = self.app.switch_task_executor(
                    principal, project_id, rest[1], rest[3], account_id)
            except NotFound as e:
                self._json(404, {"error": str(e) or "not found"})
                return True
            except ValueError as e:
                self._json(409, {"error": str(e)})
                return True
            self._json(200, result)
            return True
        # POST /api/v1/projects/{id}/conversations   (create)
        if rest == ["conversations"]:
            self._json(201, {"conversation": self.app.conversations.create(
                principal, project_id, title=body.get("title") or "",
                mode=body.get("mode") or "ask")})
            return True
        # POST /api/v1/projects/{id}/conversations/{cid}/messages   (a chat turn)
        if (len(rest) == 3 and rest[0] == "conversations" and UUID_RE.fullmatch(rest[1])
                and rest[2] == "messages"):
            self._chat_turn(principal, project_id, rest[1], body)
            return True
        # POST /api/v1/projects/{id}/repository   (connect: verify, allowlist, bind)
        #   {"full_name": "owner/name", "branch"?: "...", "account_id"?: uuid | null}
        # ``account_id`` picks the identity: a uuid is that stored account, an
        # explicit null is the server's GITHUB_TOKEN, and leaving the key out
        # keeps the identity the project already has.
        if rest == ["repository"]:
            account_id = body.get("account_id")
            if account_id is not None and not (isinstance(account_id, str)
                                               and UUID_RE.fullmatch(account_id)):
                self._json(400, {"error": "account_id must be a uuid or null"})
                return True
            server_token = "account_id" in body and account_id is None
            try:
                if body.get("additional") is True:
                    # One more repository beside the main one, read with the
                    # identity chosen for it.
                    link = self.app.add_project_repository(
                        principal, project_id, full_name=body.get("full_name"),
                        branch=body.get("branch"), account_id=account_id,
                        use_server_token=server_token, role_note=body.get("role_note"))
                    self._json(200, {"repository": link})
                    return True
                binding = self.app.connect_repository(
                    principal, project_id, full_name=body.get("full_name"),
                    branch=body.get("branch"), account_id=account_id,
                    use_server_token=server_token)
            except ProjectBindingError as e:
                self._json(400, {"error": str(e)})
                return True
            self._json(200, {"binding": binding})
            return True
        # POST /api/v1/projects/{id}/binding   (bind repository + branch)
        if rest == ["binding"]:
            try:
                binding = self.app.project_binding.set_repository(
                    principal, project_id,
                    repository_id=body.get("repository_id"),
                    full_name=body.get("full_name"),
                    branch=body.get("branch"))
            except ProjectBindingError as e:
                self._json(400, {"error": str(e)})
                return True
            self._json(200, {"binding": binding})
            return True
        # POST /api/v1/projects/{id}/binding/branch   (change only the branch)
        if rest == ["binding", "branch"]:
            try:
                binding = self.app.project_binding.set_branch(
                    principal, project_id, body.get("branch"))
            except ProjectBindingError as e:
                self._json(400, {"error": str(e)})
                return True
            self._json(200, {"binding": binding})
            return True
        # POST /api/v1/projects/{id}/openhands-account   (bind an OpenHands account)
        if rest == ["openhands-account"]:
            account_id = body.get("account_id")
            if account_id is not None and not UUID_RE.fullmatch(str(account_id)):
                self._json(400, {"error": "account_id must be a uuid or null"})
                return True
            try:
                project = self.app.openhands_accounts.bind_project(
                    principal, project_id, str(account_id) if account_id else None)
            except NotFound:
                self._json(404, {"error": "account not found or not active"})
                return True
            self._json(200, {"project": project})
            return True
        # POST /api/v1/projects/{id}/github/account  (bind one account)
        if rest == ["github", "account"]:
            account_id = body.get("account_id")
            if account_id is not None and not UUID_RE.fullmatch(str(account_id)):
                self._json(400, {"error": "account_id must be a uuid or null"})
                return True
            try:
                project = self.app.github_accounts.bind_project(
                    principal, project_id, str(account_id) if account_id else None)
            except NotFound:
                self._json(404, {"error": "account not found or not active"})
                return True
            self._json(200, {"project": project})
            return True
        return False

    def _lab_refresh_context(self, principal: str, project_id: str, body: dict) -> None:
        """Read the project's bound repository and store a context snapshot."""
        from .nexus_analysis import analyze_repository

        repository = (body.get("repository") or "").strip()
        if not repository:
            self._json(400, {"error": "repository (owner/name) is required"})
            return
        branch = (body.get("branch") or "").strip() or None
        try:
            client = self.app.github_for_project(principal, project_id)
        except ValueError as e:
            self._json(400, {"error": f"github account: {e}"})
            return
        try:
            summary = analyze_repository(client, repository, branch=branch)
        except GitHubAuthError as e:
            self._json(400, {"error": f"github auth: {e}"})
            return
        except GitHubUnavailable as e:
            self._json(503, {"error": f"github unavailable: {e}"})
            return
        except GitHubError as e:
            self._json(502, {"error": f"github: {e}"})
            return
        row, created = self.app.context.refresh(
            principal, project_id, repository=repository,
            revision=summary.get("revision") or "", summary=summary)
        self._json(201 if created else 200, {"context": row, "created": created})

    def _lab_start_run(self, principal: str, project_id: str, goal_id: str,
                       body: dict) -> None:
        """Plan the goal (provider cascade) and start a run. Nothing runs inline.

        The plan is validated by the application; a provider outage is a 503,
        not a fabricated plan. Starting the run does not dispatch: the durable
        worker advances it, so closing the browser cannot stop it.

        Everything that can refuse the request is checked before the planning
        call is made: the body, the repository (the same gate the scheduler
        applies again before each conversation), and whether the goal already
        has a live run. The run and its plan are then stored in one transaction.
        """
        from .nexus_lab_store import MAX_DEADLINE_SECONDS, MAX_RUN_ITERATIONS, MAX_TASK_ATTEMPTS
        from .providers import AllProvidersFailed, ProviderError

        def whole(name: str, default: int | None, most: int) -> int | None:
            value = body.get(name)
            if value is None:
                return default
            if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= most:
                raise ValueError(f"{name} must be a whole number between 1 and {most}")
            return value

        try:
            # Unset budgets are chosen for the size of the plan (never less than
            # they used to be): a plan of 120 tasks cannot run in 50 task starts.
            max_iterations = whole("max_iterations", None, MAX_RUN_ITERATIONS)
            max_attempts = whole("max_attempts_per_task", 3, MAX_TASK_ATTEMPTS)
            deadline_seconds = whole("deadline_seconds", None, MAX_DEADLINE_SECONDS)
        except ValueError as e:
            self._json(400, {"error": str(e)})
            return
        repository, branch = body.get("repository") or "", body.get("branch") or ""
        if not isinstance(repository, str) or not isinstance(branch, str):
            self._json(400, {"error": "repository and branch must be strings"})
            return
        repository, branch = repository.strip(), branch.strip()
        if not repository:
            # No repository named: use the project's primary one (the single
            # linked repository is primary in effect). This is what makes the
            # "primary repository" real instead of a label.
            repository = self.app.project_repos.primary_repository(project_id) or ""
        if repository and not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            self._json(400, {"error": "repository must be owner/name"})
            return

        # Membership first, then the goal itself - looked up by id, not found by
        # scanning the list of goals, which shows only the 200 newest.
        try:
            self.app.projects.get(principal, project_id)
        except NotFound:
            self._json(404, {"error": "not found"})
            return
        try:
            self.app.projects.get_goal(principal, project_id, goal_id)
        except NotFound:
            self._json(404, {"error": "goal not found"})
            return
        try:
            authorize_run_task(self.app.store, project_id=project_id,
                               repository=repository, branch=branch)
        except AuthorizationError as e:
            self._json(409, {"error": str(e)})
            return
        live = self.app.lab.active_run_for_goal(principal, project_id, goal_id)
        if live is not None:
            self._json(409, {"error": "goal already has an active run",
                             "run_id": str(live["id"])})
            return
        options = {name: value for name, value in (
            ("max_iterations", max_iterations), ("max_attempts_per_task", max_attempts),
            ("deadline_seconds", deadline_seconds)) if value is not None}
        # A run nobody named a budget for keeps going until it is done or
        # cancelled. Naming one - a number of task starts, a deadline - asks
        # for a run that stops when it is spent; ``policy`` says it outright.
        policy = body.get("policy")
        if policy is None:
            policy = ("bounded" if max_iterations is not None or deadline_seconds is not None
                      else "durable")
        if policy not in ("durable", "bounded"):
            self._json(400, {"error": "policy must be durable or bounded"})
            return
        options["policy"] = policy
        try:
            # The goal's task is its specification. A goal from before
            # specifications existed gets one now, from its text.
            _goal, spec = self.app.specs.ensure_for_goal(principal, project_id, goal_id)
            outcome = self.app.spec_planner.start(
                principal=principal, project_id=project_id, goal_id=goal_id,
                spec_id=spec["id"], repository=repository, branch=branch, options=options)
        except AllProvidersFailed as e:
            self._json(503, {"error": "no orchestrator provider succeeded",
                             "failures": e.failures})
            return
        except ProviderError as e:
            self._json(503, {"error": str(e)})
            return
        except PlanningFailed as e:
            self._json(422, {"error": f"the goal cannot be planned: {e}"})
            return
        summary = SpecService.summary(spec)
        if outcome["status"] == "existing":
            self._json(409, {"error": "goal already has an active run",
                             "run_id": outcome["run_id"]})
            return
        if outcome["status"] == "planning":
            # Too long for one planning request: the plan is built in parts by
            # the planning loop and the run starts when it is complete. Asking
            # again returns the progress, not a second plan. Follow it at
            # GET .../specs/{id}: ``state`` becomes "planned" and ``run_id`` is set.
            self._json(202, {"status": "planning", "spec": summary,
                             "planning": outcome["stages"]})
            return
        run = outcome["run"]
        stored = self.app.lab.list_tasks(principal, project_id, str(run["id"]))
        self._json(201, {"run": run, "tasks": stored, "plan": outcome["plan"],
                         "spec": summary})

    def _run_facts(self, principal: str, project_id: str) -> dict:
        """The real state of the project's autonomous runs, for an analysis turn.

        Read from the database, not guessed: the newest runs with their state,
        progress and reason, and each one's report if it has finished. This is
        what lets "что уже готово?" and "почему прогон остановился?" be answered
        from facts instead of starting another run. A read error yields an empty
        shape, never a fabricated one.
        """
        try:
            runs = self.app.lab.list_runs(principal, project_id, include_hidden=True)
        except NotFound:
            return {"runs": []}
        out: list[dict] = []
        for run in runs[:10]:
            try:
                counts = self.app.lab.count_tasks(str(run["id"]))
            except Exception:  # noqa: BLE001 - a facts gather must not fail a turn
                counts = {}
            report = self.app.lab.report_for_run(str(run["id"])) or {}
            out.append({
                "id": str(run["id"]),
                "state": run["state"],
                "goal_id": str(run.get("goal_id") or ""),
                "iteration": run.get("iteration"),
                "max_iterations": run.get("max_iterations"),
                "retry_attempt": run.get("retry_attempt"),
                "max_retry_cycles": run.get("max_retry_cycles"),
                "last_error": (run.get("last_error") or "")[:500],
                "tasks": counts,
                "report": (report.get("content") or "")[:3000] if report else "",
            })
        return {"runs": out}

    def _continue_run(self, principal: str, project_id: str, conversation_id: str,
                      active_run: dict) -> list[dict]:
        """Continue the run this conversation started. Never a second run.

        A ``paused`` or ``blocked`` run is resumed (the same action as the
        "Продолжить" button). A ``running``/``retrying`` run is already working,
        so the reply says so and nothing is changed - the dialogue does not stop
        or restart a working run. A terminal run is reported as finished; a new
        run needs a new message, not "продолжи".
        """
        run_id = str(active_run["id"])
        state = active_run["state"]
        if state in ("paused", "blocked"):
            try:
                self.app.lab.set_run_state(principal, project_id, run_id, "running")
            except InvalidTransition as e:
                return [{"role": "assistant",
                         "content": f"Прогон {run_id[:8]} не удалось продолжить: {e}",
                         "meta": {"mode": "autonomous", "run_id": run_id}}]
            content = (f"Продолжаю прогон {run_id[:8]}: задачи, не прошедшие проверку, "
                       "возвращены в работу. Ход работы виден на вкладке «Агент».")
            return [{"role": "assistant", "content": content,
                     "meta": {"mode": "autonomous", "run_id": run_id, "resumed": True}}]
        if state in ("running", "retrying", "waiting_for_approval"):
            content = (f"Прогон {run_id[:8]} уже выполняется (состояние: {state}). "
                       "Ничего не перезапускаю; ход работы виден на вкладке «Агент».")
            return [{"role": "assistant", "content": content,
                     "meta": {"mode": "autonomous", "run_id": run_id, "already_running": True}}]
        content = (f"Прогон {run_id[:8]} завершён (состояние: {state}). "
                   "Чтобы начать новую работу, опишите её новым сообщением.")
        return [{"role": "assistant", "content": content,
                 "meta": {"mode": "autonomous", "run_id": run_id, "terminal": True}}]

    def _chat_turn(self, principal: str, project_id: str, conversation_id: str,
                   body: dict) -> None:
        """Store the user turn, produce the assistant turn, return the new messages.

        The user message is persisted first, so it survives even if the assistant
        fails. The assistant reply is produced by :mod:`orchestration.nexus_chat`
        (real provider cascade or real dispatch), never fabricated; a failure is
        recorded as an ``error`` system message and the conversation state becomes
        ``error``. The response is the list of messages appended this turn, which
        the client appends to the transcript (a single JSON body, not a stream —
        the transport here is a stdlib HTTP server; the client renders a typing
        indicator until the body arrives).
        """
        sent = body.get("text")
        if sent is not None and not isinstance(sent, str):
            self._json(400, {"error": "text must be a string"})
            return
        text = (sent or "").strip()
        if not text:
            self._json(400, {"error": "text is required"})
            return
        mode = body.get("mode")
        conversation = self.app.conversations.get(principal, project_id, conversation_id)
        # Everything that can refuse the message is checked before anything is
        # written, so a refused message leaves no trace: not a new mode, not a
        # title made of words that were never accepted. The raised errors are
        # answered 400, or 413 for a text that is too long.
        if mode:
            self.app.conversations.check_mode(mode)
        # The Intent Router decides what this message asks for, before any mode
        # is applied: a question must not start a run just because the
        # conversation's mode is ``autonomous``. ``auto`` (the default) and
        # ``autonomous`` leave the decision to the message; an explicit
        # ask/plan/analysis/code mode is the person's own choice.
        active_run = self.app.conversations.linked_run(principal, project_id, conversation_id)
        chosen = mode or conversation["mode"]
        intent = classify(text, mode=chosen, has_active_run=active_run is not None)
        turn_mode = effective_mode(chosen, intent)
        if (turn_mode == "autonomous" and chosen != "autonomous"
                and not (conversation.get("repository")
                         or self.app.project_repos.primary_repository(project_id))):
            # In "Авто" mode the router chose work, but the project has no
            # repository to run it on. Rather than fail, answer in the chat and
            # say what is missing: the conversation stays usable. An explicit
            # "Автономно" choice is the person's own and is not overridden.
            turn_mode = "ask"
        if turn_mode == "autonomous":
            check_text(sent)
        else:
            check_message(sent, turn_mode)
        if mode:
            self.app.conversations.set_mode(principal, project_id, conversation_id, mode)
            # Re-read so the joined repository/branch fields are present; set_mode
            # returns only the conversation row itself.
            conversation = self.app.conversations.get(principal, project_id, conversation_id)
        if not (conversation.get("title") or "").strip():
            # An untitled conversation is named after its first message, so the
            # history reads as what was asked. This only fills a blank; a title
            # the user chose is never replaced.
            self.app.conversations.rename(
                principal, project_id, conversation_id, title_from_message(text))
        # An autonomous turn's text is the task. It is stored first, exactly as
        # it was sent - not trimmed, any size the server accepts - with its
        # SHA-256, as a specification; planning and the agents work from that
        # copy. The same text in the same conversation is the same row, which is
        # what lets a repeated send be recognised.
        spec = None
        shown, user_meta = text, None
        if turn_mode == "autonomous":
            spec, _new = self.app.specs.create(
                principal, project_id, sent, source="chat", conversation_id=conversation_id)
            user_meta = {"spec": SpecService.summary(spec)}
            if spec["char_count"] > inline_chars():
                # The transcript shows the beginning; the message says so and
                # points at the whole text. Nothing downstream reads this copy.
                shown = preview(sent)
                user_meta["spec"]["preview"] = True
        # Persist the user turn first: it must not be lost to an assistant failure.
        user_msg = self.app.conversations.add_message(
            principal, project_id, conversation_id, role="user", content=shown,
            meta=user_meta)
        # What was said before this turn. The turn's own text is the request
        # and is not repeated as history.
        history = [m for m in self.app.conversations.list_messages(
            principal, project_id, conversation_id) if str(m["id"]) != str(user_msg["id"])]
        appended: list[dict] = [user_msg]
        # "Продолжи" continues the run this conversation already started; it
        # never creates a second one. A run that is paused or blocked is
        # resumed; a run that is already working is reported as it is.
        if intent.continue_run and active_run is not None:
            produced = self._continue_run(principal, project_id, conversation_id,
                                          active_run)
            for msg in produced:
                appended.append(self.app.conversations.add_message(
                    principal, project_id, conversation_id, role=msg["role"],
                    content=msg["content"], meta=msg.get("meta")))
            self.app.conversations.set_state(principal, project_id, conversation_id, "idle")
            self._json(200, {"messages": appended})
            return
        # An ambiguous message in a mode the router had to guess (``auto``) does
        # not start a run: it is answered with one short clarifying question. In
        # an explicit ``autonomous`` conversation the person already chose work,
        # so an unclear *goal* runs rather than being turned into a question.
        if intent.ambiguous and turn_mode != "autonomous" and not intent.starts_work:
            self.app.conversations.set_state(principal, project_id, conversation_id, "idle")
            ask = self.app.conversations.add_message(
                principal, project_id, conversation_id, role="assistant",
                content=clarifying_question(),
                meta={"mode": "ask", "clarify": True, "intent": intent.intent})
            self._json(200, {"messages": appended + [ask]})
            return
        self.app.conversations.set_state(principal, project_id, conversation_id, "thinking")
        context = self.app.context.current(principal, project_id)
        memory = self.app.memory.retrieve(principal, project_id, limit=30)
        # What the project's runs really did, for an analysis turn. Read here so
        # a question about progress is answered from the database, not guessed.
        run_facts = self._run_facts(principal, project_id) if turn_mode == "analysis" else None
        try:
            produced = self.app.chat.respond(
                mode=turn_mode, goal=text, context=context, memory=memory,
                repository=conversation.get("repository") or "",
                branch=conversation.get("selected_branch") or "",
                history=history,
                # The work a turn starts belongs to this conversation's project
                # and to the person who asked for it.
                project_id=project_id, principal=principal,
                conversation_id=conversation_id,
                spec_id=spec["id"] if spec else None,
                run_facts=run_facts)
        except ChatError as e:
            self.app.conversations.set_state(principal, project_id, conversation_id, "error")
            err = self.app.conversations.add_message(
                principal, project_id, conversation_id, role="system",
                content=f"Не удалось получить ответ: {e}", meta={"error": True})
            self._json(502, {"error": str(e), "messages": appended + [err]})
            return
        # Persist each produced message, attaching any run/task ids to the
        # conversation so the agent panel can follow it.
        for msg in produced:
            stored = self.app.conversations.add_message(
                principal, project_id, conversation_id, role=msg["role"],
                content=msg["content"], meta=msg.get("meta"))
            appended.append(stored)
            meta = msg.get("meta") or {}
            if meta.get("workflow_id") and msg["role"] == "assistant":
                self._record_agent_session(principal, project_id, conversation_id,
                                           meta, msg)
            if meta.get("run_id") and msg["role"] == "assistant":
                self.app.conversations.link_run(
                    principal, project_id, conversation_id, str(meta["run_id"]))
        self.app.conversations.set_state(principal, project_id, conversation_id, "idle")
        self._json(200, {"messages": appended})

    def _record_agent_session(self, principal: str, project_id: str,
                              conversation_id: str, meta: dict, msg: dict) -> None:
        """Record the mapping between this NEXUS chat and the OpenHands work.

        A NEXUS task/conversation is not an OpenHands conversation; this keeps the
        two distinct and durable so the agent panel can show the real session.
        """
        try:
            binding = self.app.project_binding.get(principal, project_id)
            session = self.app.conversations.start_agent_session(
                principal, project_id, conversation_id=conversation_id,
                openhands_account_id=(str(binding["openhands_account_id"])
                                      if binding.get("openhands_account_id") else None),
                external_conversation_id=meta.get("sr_conversation_id"),
                repository=meta.get("repository") or binding.get("full_name") or "",
                branch=meta.get("branch") or binding.get("selected_branch") or "")
            self.app.conversations.update_agent_session(
                session["id"], state="running",
                current_activity=(msg.get("content") or "")[:500])
        except (NotFound, ValueError):
            pass

    def _confirm_action(self, principal: str, project_id: str, action_id: str,
                        body: dict):
        """Confirm a proposal and, for a GitHub action, execute it once.

        The confirmation and the side effect are separate steps: the row is
        marked ``confirmed`` first, so a retried confirm cannot run the action
        twice (the second confirm is refused as already-confirmed). A failed
        execution is recorded as the result and surfaced, never retried blindly.
        """
        decided = self.app.actions.confirm(principal, action_id, body.get("token") or "")
        name = decided["action"]
        payload = decided["payload"] if isinstance(decided["payload"], dict) else {}
        result: dict[str, Any]
        if name == "github.create_repository":
            if not self.app.github_writer.configured:
                return self._json(400, {"error": "no GitHub token configured"})
            try:
                result = self.app.github_writer.create_repository(
                    name=payload.get("name", ""), private=bool(payload.get("private", True)),
                    description=payload.get("description", ""))
            except (GitHubAuthError, GitHubUnavailable, GitHubError, ValueError) as e:
                return self._json(self._github_status(e), {"error": f"github: {e}",
                                                           "action": decided})
        elif name == "github.set_visibility":
            if not self.app.github_writer.configured:
                return self._json(400, {"error": "no GitHub token configured"})
            private = bool(payload.get("private", True))
            try:
                result = self.app.github_writer.set_repository_visibility(
                    full_name=payload.get("full_name", ""), private=private,
                    # The proposal named the change and a human confirmed it, so
                    # publishing is acknowledged here and only here.
                    acknowledge_public=not private)
            except VisibilityError as e:
                return self._json(400, {"error": str(e), "action": decided})
            except (GitHubAuthError, GitHubUnavailable, GitHubError, ValueError) as e:
                return self._json(self._github_status(e), {"error": f"github: {e}",
                                                           "action": decided})
        else:
            return self._json(400, {"error": f"unknown action: {name}"})
        recorded = self.app.actions.record_result(principal, action_id, result)
        return self._json(200, {"action": recorded, "result": result})

    @staticmethod
    def _github_status(error: Exception) -> int:
        if isinstance(error, GitHubAuthError):
            return 400
        if isinstance(error, GitHubUnavailable):
            return 503
        return 502

    def _project_upload(self, path: str) -> None:
        """Handle a multipart upload into a project (file or voice note).

        Reached only for ``multipart/form-data`` under ``/api/v1/projects/``.
        The project id and route are validated before the body is read, so a
        malformed request does not get to consume an upload's worth of bytes.
        """
        parsed = self._project_subpath(path)
        if parsed is None:
            return self._json(404, {"error": "not found"})
        project_id, rest = parsed
        if rest not in (["files"], ["voice"]):
            return self._json(404, {"error": "not found"})
        boundary = self._multipart_boundary()
        if not boundary:
            return self._json(400, {"error": "multipart boundary is missing"})
        try:
            body = self._raw_body(max_upload_bytes())
        except ValueError as e:
            return self._json(413 if "limit" in str(e) else 400, {"error": str(e)})
        try:
            parts = parse_multipart(body, boundary)
        except MultipartError as e:
            return self._json(400, {"error": str(e)})
        principal = self._project_principal()
        if rest == ["files"]:
            part = parts.get("file")
            if part is None or not part.is_file:
                return self._json(400, {"error": "a file part is required"})
            row = self.app.files.upload(
                principal, project_id,
                filename=part.filename or parts.get("filename", part).data.decode("utf-8", "replace"),
                content_type=part.content_type or "application/octet-stream",
                data=part.data)
            return self._json(201, {"file": row})
        # voice note
        part = parts.get("audio") or parts.get("file")
        if part is None or not part.is_file:
            return self._json(400, {"error": "an audio part is required"})
        duration = parts.get("duration_ms")
        duration_ms = None
        if duration is not None and duration.data:
            try:
                duration_ms = int(duration.data.decode("ascii").strip())
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "duration_ms must be an integer"})
        transcript = parts.get("transcript")
        note = self.app.voice.create(
            principal, project_id,
            content_type=part.content_type or "audio/webm",
            data=part.data, duration_ms=duration_ms,
            transcript=transcript.data.decode("utf-8", "replace") if transcript else None,
            # A transcript the browser supplies came from the browser's own
            # recognizer; label it so it is never mistaken for a server check.
            engine="browser" if transcript else None)
        return self._json(201, {"voice_note": note})

    def _multipart_boundary(self) -> str | None:
        ctype = self.headers.get("Content-Type", "")
        for token in ctype.split(";")[1:]:
            key, _, value = token.partition("=")
            if key.strip().lower() == "boundary":
                return value.strip().strip('"') or None
        return None

    @staticmethod
    def _plan_path(path: str) -> tuple[str, str, str] | None:
        parts = path.strip("/").split("/")
        if len(parts) not in (6, 7) or parts[:3] != ["api", "v1", "projects"]:
            return None
        if not UUID_RE.fullmatch(parts[3]) or not UUID_RE.fullmatch(parts[5]):
            return None
        if len(parts) == 7 and parts[4] == "goals" and parts[6] == "plans":
            return parts[3], "goal-plans", parts[5]
        if len(parts) == 6 and parts[4] == "plans":
            return parts[3], "plan", parts[5]
        if len(parts) == 7 and parts[4] == "plans" and parts[6] == "approve":
            return parts[3], "approve", parts[5]
        if len(parts) == 7 and parts[4] == "goals" and parts[6] == "plan-draft":
            return parts[3], "goal-plan-draft", parts[5]
        return None

    def _plan_draft(self, principal: str, project_id: str, goal_id: str) -> None:
        """Ask the provider cascade to draft a plan for a goal. Nothing runs.

        The goal text is read (membership enforced), sent to the first working
        orchestrator provider, validated, and returned as a *draft*. It is not
        stored and nothing is dispatched: approving and dispatching stay
        explicit, separate steps. A provider outage is a ``503``, never a
        fabricated plan.
        """
        from .providers import AllProvidersFailed, ProviderError

        try:
            goal = self.app.projects.get(principal, project_id)  # membership check
            goals = self.app.projects.list_goals(principal, project_id)
        except NotFound:
            return self._json(404, {"error": "not found"})
        match = next((g for g in goals if str(g["id"]) == goal_id), None)
        if match is None:
            return self._json(404, {"error": "goal not found"})
        prompt = (
            "Return a JSON object {\"tasks\": [{\"id\", \"title\", \"description\", "
            "\"dependencies\"}]} that decomposes this goal. Keep it minimal.\n\n"
            f"Project: {goal['name']}\nGoal: {match['text']}"
        )
        try:
            result = self.app.providers.plan(prompt)
        except AllProvidersFailed as e:
            return self._json(503, {"error": "no orchestrator provider succeeded",
                                    "failures": e.failures})
        except ProviderError as e:
            return self._json(503, {"error": str(e)})
        return self._json(200, {
            "draft": {"tasks": result.plan},
            "provider": result.provider,
            "failovers": result.failovers,
            "stored": False,
        })

    # -------------------------------------------------- nested project routes
    def _project_nested_get(self, project_id: str, resource: str,
                            item_id: str | None) -> None:
        """Read a project-scoped resource. Membership is enforced by the service
        layer (a non-member gets 404), so no cross-project read is possible."""
        principal = self._project_principal()
        self.app.projects.get(principal, project_id)  # membership check
        if resource == "repositories":
            return self._json(200, {"repositories": self.app.project_repos.list(principal, project_id)})
        if resource == "executors":
            # The project's executors in order (the first is the primary), its
            # strategy, and the caller's connections that could be added.
            return self._json(200, self.app.project_executors.get(principal, project_id))
        if resource == "tasks":
            if item_id is None:
                tasks = self.app.store.list_tasks(project_id=project_id)
                return self._json(200, {"tasks": [_task_brief(t) for t in tasks]})
            detail = self.app.store.task_detail(item_id)
            if detail["project_id"] != project_id:
                return self._json(404, {"error": "not found"})
            return self._json(200, {"task": detail})
        if resource == "workflows":
            if item_id is None:
                return self._json(200, {"workflows": self.app.store.list_workflows(project_id=project_id)})
            detail = self.app.workflow_detail(item_id)
            if (detail.get("workflow") or {}).get("project_id") != project_id:
                return self._json(404, {"error": "not found"})
            return self._json(200, detail)
        if resource == "evidence":
            if item_id is None:
                return self._json(200, {"evidence": self.app.evidence.list(project_id=project_id)})
            rec = self.app.evidence.get(item_id, project_id=project_id)
            return self._json(200, {"evidence": rec})
        return self._json(404, {"error": "not found"})

    def _project_nested_post(self, project_id: str, resource: str,
                             item_id: str | None, body: Any) -> None:
        """Write a project-scoped resource (repositories, tasks, evidence)."""
        principal = self._project_principal()
        if resource == "repositories" and item_id is None:
            full_name = (body.get("full_name") or "").strip() if isinstance(body, dict) else ""
            if not re.fullmatch(r"[\w.-]+/[\w.-]+", full_name):
                return self._json(400, {"error": "full_name must be owner/name"})
            link = self.app.project_repos.link(principal, project_id, full_name)
            return self._json(201, {"repository": link})
        if resource == "repositories" and item_id == "remove":
            full_name = (body.get("full_name") or "").strip() if isinstance(body, dict) else ""
            if not full_name:
                return self._json(400, {"error": "full_name is required"})
            try:
                removed = self.app.project_repos.unlink(principal, project_id, full_name)
            except AuthorizationError as e:
                # Work is still under way on it: said, not a server error.
                return self._json(409, {"error": str(e)})
            if not removed:
                return self._json(404, {"error": "repository not linked to this project"})
            return self._json(200, {"unlinked": full_name})
        # POST /api/v1/projects/{id}/executors  — set the whole list, in order.
        #   {"executors": [{"account_id": uuid, "enabled": bool} | uuid, ...],
        #    "strategy": "failover" | "distribute"}
        #   The first entry is the primary. {"primary": uuid|null,
        #   "fallbacks": [uuid, ...]} is accepted as the same thing.
        if resource == "executors" and item_id is None:
            if not isinstance(body, dict):
                return self._json(400, {"error": "body must be a JSON object"})
            executors = body.get("executors")
            if executors is None and ("primary" in body or "fallbacks" in body):
                primary, fallbacks = body.get("primary"), body.get("fallbacks") or []
                if primary is not None and not isinstance(primary, str):
                    return self._json(400, {"error": "primary must be a uuid or null"})
                if not isinstance(fallbacks, list):
                    return self._json(400, {"error": "fallbacks must be a list of uuids"})
                if primary is None and fallbacks:
                    return self._json(400, {"error": "a list of backups needs a primary"})
                executors = ([primary] if primary else []) + list(fallbacks)
            if executors is not None:
                if not isinstance(executors, list):
                    return self._json(400, {"error": "executors must be a list"})
                for item in executors:
                    account = item.get("account_id") if isinstance(item, dict) else item
                    if not (isinstance(account, str) and UUID_RE.fullmatch(account)):
                        return self._json(400, {"error": "each executor must name an "
                                                         "account_id (a uuid)"})
            try:
                assigned = self.app.project_executors.set(
                    principal, project_id, executors=executors,
                    strategy=body.get("strategy"))
            except NotFound as e:
                return self._json(404, {"error": str(e) or "not found"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, assigned)
        if resource == "tasks" and item_id is None:
            if not isinstance(body, dict):
                return self._json(400, {"error": "body must be a JSON object"})
            return self._create_project_task(principal, project_id, body)
        if resource == "evidence" and item_id is None:
            if not isinstance(body, dict):
                return self._json(400, {"error": "body must be a JSON object"})
            rec = self.app.evidence.record(
                subject_kind=body.get("subject_kind") or "manual",
                subject_id=body.get("subject_id"),
                project_id=project_id,
                repository_full_name=body.get("repository"),
                commit_sha=body.get("commit_sha"),
                branch=body.get("branch"),
                summary=body.get("summary") or "",
                detail=body.get("detail") or {},
                created_by=principal,
            )
            return self._json(201, {"evidence": rec})
        return self._json(404, {"error": "not found"})

    @staticmethod
    def _project_evidence_verify(path: str) -> tuple[str, str] | None:
        """Parse ``/api/v1/projects/<uuid>/evidence/<uuid>/verify``."""
        parts = path.strip("/").split("/")
        if (len(parts) == 7 and parts[:3] == ["api", "v1", "projects"]
                and parts[4] == "evidence" and parts[6] == "verify"
                and UUID_RE.fullmatch(parts[3]) and UUID_RE.fullmatch(parts[5])):
            return parts[3], parts[5]
        return None

    def _mcp_http(self) -> None:
        """Serve one MCP Streamable-HTTP request at ``POST /mcp``.

        Authentication is the server's own: a session with CSRF, or the machine
        bearer. The MCP caller therefore has exactly the caller's authority, and
        the tool surface re-checks project membership. A JSON-RPC notification
        returns 202 with no body; a parse error is a 400.
        """
        from .mcp_server import McpServer, handle_http

        n = int(self.headers.get("Content-Length", 0))
        if n < 0 or n > 1024 * 1024:
            return self._json(400, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32600, "message": "invalid request"}})
        raw = self.rfile.read(n) if n else b""
        try:
            message = json.loads(raw) if raw else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json(400, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700, "message": "parse error"}})
        server = McpServer(self.app.store, self._project_principal())
        status, payload = handle_http(server, message)
        if status == 202:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self._json(status, payload)

    def _create_project_task(self, principal: str, project_id: str, body: dict) -> None:
        """Create a task inside a project and start its workflow (Sr).

        This is the MVP path: the project, the repository link, the branch and
        the goal are all bound here, and the workflow inherits the project so the
        Sr-created child tasks do too.
        """
        goal = (body.get("goal") or "").strip()
        if not goal:
            return self._json(400, {"error": "goal is required"})
        repository = body.get("repository")
        branch = body.get("branch")
        if repository is not None:
            if not isinstance(repository, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
                return self._json(400, {"error": "repository must be owner/name"})
            if not self.app.store.project_repository_linked(project_id, repository):
                return self._json(409, {"error": "repository is not linked to this project"})
            repo = self.app.store.allowed_repository(repository)
            if repo is None:
                return self._json(409, {"error": "repository is not enabled"})
            allowed = repo.get("allowed_branches") or []
            if branch:
                if (allowed and branch not in allowed) or (
                        not allowed and branch != repo["default_branch"]):
                    return self._json(409, {"error": "branch is not allowed"})
        request_id = (self.headers.get("Idempotency-Key")
                      or (body.get("request_id") or "").strip() or None)
        out = self.app.start_workflow(
            goal, repository, branch, request_id=request_id,
            project_id=project_id, principal=principal,
        )
        return self._json(200, out)


    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            # --- public ---
            if path == "/login":
                if self._session() is not None:
                    return self._redirect("/")
                return self._html(200, web_ui.login_page(""))
            if path == "/healthz":
                return self._json(200, HEALTHZ)
            if path == "/readyz":
                # Readiness: liveness plus a real database round-trip. A process
                # that is up but cannot reach PostgreSQL is not ready to serve.
                # Public on purpose (a load balancer must not need a credential),
                # and it exposes no configuration.
                try:
                    self.app.store.dashboard_counts()
                    return self._json(200, {"status": "ready", "database": True})
                except Exception:  # noqa: BLE001 - readiness must not raise
                    return self._json(503, {"status": "not_ready", "database": False})
            if path in PWA_ASSETS:
                name, content_type, cache_control = PWA_ASSETS[path]
                return self._static(200, (_PWA_DIR / name).read_bytes(), content_type,
                                    cache_control=cache_control)
            if path in web_ui.STATIC:
                # A closed mapping, not a directory lookup: a path that is not a
                # key here is never read from disk (see web_ui.STATIC).
                body, content_type, cache_control = web_ui.STATIC[path]
                return self._static(200, body, content_type, cache_control=cache_control)

            # --- everything below requires authentication ---
            if path == "/":
                if self._session() is None and not self._bearer_ok():
                    return self._redirect("/login")
                return self._html(200, web_ui.APP_HTML)
            if not self._require_read():
                return

            file_path = self._file_path(path)
            if file_path:
                project_id, file_id, is_delete = file_path
                if is_delete:
                    return self._json(404, {"error": "not found"})
                # Two upload designs share this route. A row written by the
                # opaque service (raw body + X-File-Name) carries ``name``/
                # ``content`` and is served as a raw attachment; a row written
                # by the File Lab (multipart) carries ``filename``/``data`` and
                # is served as a JSON preview. Disambiguate by provenance.
                principal = self._project_principal()
                if self.app.opaque_files.is_opaque(principal, project_id, file_id):
                    file = self.app.opaque_files.download(principal, project_id, file_id)
                    return self._download(file["name"], bytes(file["content"]))
                return self._json(200, {"file": self.app.files.preview(principal, project_id, file_id)})
            plan_path = self._plan_path(path)
            if plan_path:
                project_id, kind, item_id = plan_path
                principal = self._project_principal()
                if kind == "goal-plans":
                    return self._json(200, {"plans": self.app.plans.list(principal, project_id, item_id)})
                if kind == "plan":
                    return self._json(200, {"plan": self.app.plans.get(principal, project_id, item_id)})
                return self._json(404, {"error": "not found"})
            if path == "/api/v1/projects":
                return self._json(200, {"projects": self.app.projects.list(self._project_principal())})
            nested = self._project_nested(path)
            if nested is not None:
                return self._project_nested_get(*nested)
            if path.startswith("/api/v1/projects/"):
                parsed = self._project_path(path)
                if parsed is None:
                    # Deeper routes (files/workspaces/voice/actions/github) have
                    # more segments than ``_project_path`` accepts; let the
                    # subpath dispatcher decide, and 404 only if it is unmatched.
                    sub = self._project_subpath(path)
                    if sub is not None:
                        handled = self._get_project_resource(path, sub[0], self._project_principal())
                        if handled is not None:
                            status, payload, raw = handled
                            if raw is not None:
                                body, content_type, filename = raw
                                return self._attachment(200, body, content_type, filename)
                            return self._json(status, payload)
                    return self._json(404, {"error": "not found"})
                project_id, resource = parsed
                principal = self._project_principal()
                if not resource:
                    return self._json(200, {"project": self.app.projects.get(principal, project_id)})
                if resource == "goals":
                    return self._json(200, {"goals": self.app.projects.list_goals(principal, project_id)})
                if resource == "files":
                    return self._json(200, {"files": self.app.files.list_files(principal, project_id)})
                if resource == "events":
                    return self._json(200, {"events": self.app.projects.events(principal, project_id)})
                # File lab / workspaces / voice / guarded actions. These use the
                # deeper path split so the resource id is part of the route.
                handled = self._get_project_resource(path, project_id, principal)
                if handled is not None:
                    status, payload, raw = handled
                    if raw is not None:
                        body, content_type, filename = raw
                        return self._attachment(200, body, content_type, filename)
                    return self._json(status, payload)
                return self._json(404, {"error": "not found"})
            if path == "/api/session":
                s = self._session()
                return self._json(200, {
                    "username": s.username if s else "api",
                    "csrf_token": s.csrf_token if s else "",
                })
            if path == "/api/studio":
                return self._json(200, self.app.studio_status())
            if path == "/api/dashboard":
                return self._json(200, {"counts": self.app.store.dashboard_counts()})
            if path == "/api/system":
                return self._json(200, self.app.system_info())
            if path == "/api/security/policy":
                # The NEXUS permission policy: roles and their scopes, the scope
                # every MCP tool needs, and the SSRF/redaction configuration.
                # Names and booleans only; never a secret value.
                return self._json(200, policy_report())
            if path == "/api/github/accounts":
                # Token-free views only; the PAT never leaves the server.
                return self._json(200, {"accounts": self.app.github_accounts.list_accounts(
                    self._project_principal())})
            if path == "/api/openhands/accounts":
                # API-key-free views only; the key never leaves the server.
                return self._json(200, {"accounts": self.app.openhands_accounts.list_accounts(
                    self._project_principal())})
            if path == "/api/openhands/diagnostics":
                # The diagnostic report: every check VERIFIED/FAILED/BLOCKED/
                # UNVERIFIED. Read-only unless live=true is passed to the POST.
                principal = self._project_principal()
                query = urllib.parse.parse_qs(urlparse(self.path).query)
                return self._json(200, self.app.openhands_diagnostics(
                    principal,
                    live=(query.get("live") or ["0"])[0].lower() in ("1", "true", "yes"),
                    account_id=(query.get("account_id") or [None])[0],
                    project_id=(query.get("project_id") or [None])[0]))
            if path == "/api/providers":
                # The orchestrator cascade: configured provider names and the
                # order they are tried in. No key is returned; ?live=1 also
                # probes each configured provider with a read-only request.
                query = urllib.parse.parse_qs(urlparse(self.path).query)
                return self._json(200, self.app.provider_status(
                    live=(query.get("live") or ["0"])[0].lower() in ("1", "true", "yes")))
            if path == "/api/openhands/tools":
                # Which tools are available to NEXUS and which to OpenHands.
                # ?live=1 reads the agent-server's own catalog (it may provision
                # a sandbox on the direct transport); without it the OpenHands
                # check is UNVERIFIED. No key is returned.
                query = urllib.parse.parse_qs(urlparse(self.path).query)
                return self._json(200, self.app.tool_inventory(
                    self._project_principal(),
                    live=(query.get("live") or ["0"])[0].lower() in ("1", "true", "yes"),
                    account_id=(query.get("account_id") or [None])[0],
                    project_id=(query.get("project_id") or [None])[0]))
            if path == "/api/openhands/pool":
                principal = self._project_principal()
                live = self.app.account_pool.live_load()
                candidates = []
                for a in self.app.account_pool.candidates(principal):
                    observed = int(a.get("active_tasks", 0) or 0) + live.get(str(a["id"]), 0)
                    candidates.append({
                        "id": a["id"], "label": a["label"], "priority": a.get("priority"),
                        "health": a.get("health"), "active_tasks": a.get("active_tasks", 0),
                        "load": observed,
                        "enabled": a.get("enabled", True), "is_default": a.get("is_default"),
                        "max_concurrent_tasks": a.get("max_concurrent_tasks"),
                        "daily_conversation_limit": a.get("daily_conversation_limit"),
                        "api_key_hint": a.get("api_key_hint", ""),
                        "connection_ok": a.get("connection_ok"),
                        "last_connection_at": a.get("last_connection_at"),
                        "error_count": a.get("error_count", 0)})
                return self._json(200, {
                    "config": self.app.account_pool.get_config(principal),
                    "candidates": candidates,
                })
            if path.startswith("/api/github/accounts/"):
                return self._github_account_get(path)
            if path.startswith("/api/openhands/accounts/"):
                return self._openhands_account_get(path)
            if path == "/api/config":
                return self._json(200, {
                    "cloud_key": bool(os.environ.get("OPENHANDS_API_KEY")),
                    "cloud_url": self.app.client.base_url,
                    "transport": os.environ.get("OPENHANDS_TRANSPORT", "direct"),
                    # Whether this transport can select a repository natively.
                    # The UI and the operator need to know: a False here with a
                    # repository task means the direct transport's clone-by-
                    # instruction mode (or refusal) applies.
                    "supports_repository_selection": getattr(
                        self.app.client, "supports_repository_selection", False),
                    "repository_mode": os.environ.get(
                        "OPENHANDS_DIRECT_REPO_MODE", "prompt"),
                })
            if path == "/api/repositories":
                return self._json(200, {
                    "repositories": self.app.store.list_repositories()
                })
            if path == "/api/github/status":
                return self._json(200, {"configured": self.app.github.configured,
                                        "writes_enabled": self.app.github_writes_enabled})
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
            server_branches = re.fullmatch(
                r"/api/github/repositories/([^/]+)/([^/]+)/branches", path)
            if server_branches:
                # The same list with the server's GITHUB_TOKEN, for a project
                # that is connected without a stored account.
                from urllib.parse import unquote
                full_name = "/".join(unquote(part) for part in server_branches.groups())
                try:
                    return self._json(200, {"branches": self.app.github.list_branches(full_name)})
                except GitHubAuthError as e:
                    return self._json(400, {"error": f"github auth: {e}"})
                except GitHubUnavailable as e:
                    return self._json(503, {"error": f"github unavailable: {e}"})
                except GitHubError as e:
                    return self._json(502, {"error": f"github: {e}"})
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
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
        except StoreUnavailable:
            # The database could not be reached and the connection could not be
            # replaced. Say so plainly; the driver's text names hosts and ports.
            return self._json(503, {"error": "database unavailable"})
        except Exception as e:  # noqa: BLE001
            log.exception("GET %s failed", path)
            error = "internal error" if path.startswith("/api/v1/projects") else f"{type(e).__name__}: {e}"
            return self._json(500, {"error": error})

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
            if path == "/mcp":
                return self._mcp_http()
            # Raw multipart uploads are not JSON, so they are handled before the
            # JSON body is read. The content type is checked first so a plain
            # JSON body is never mistaken for an upload.
            if (path.startswith("/api/v1/projects/")
                    and self.headers.get("Content-Type", "").startswith("multipart/form-data")):
                return self._project_upload(path)
            parsed = self._project_path(path)
            if parsed and parsed[1] == "files":
                # Legacy opaque upload: raw body + X-File-Name. Uses the simpler
                # FileService; the File Lab path above handles multipart.
                name = urllib.parse.unquote(self.headers.get("X-File-Name", ""), errors="strict")
                file = self.app.opaque_files.upload(self._project_principal(), parsed[0], name, self._file_body())
                return self._json(201, {"file": file})
            file_path = self._file_path(path)
            if file_path and file_path[2]:
                if self._body(max_bytes=1024):
                    raise ValueError("delete body must be empty")
                self.app.opaque_files.delete(self._project_principal(), file_path[0], file_path[1])
                return self._json(200, {"deleted": True})
            if path in ("/api/github/prepare", "/api/github/confirm"):
                # A machine bearer can never approve changes on the owner's behalf.
                session = self._session()
                if session is None or not self.app.auth.csrf_ok(session, self.headers.get(CSRF_HEADER)):
                    return self._json(403, {"error": "human session and CSRF required"})
                if not self.app.github_writes_enabled:
                    return self._json(403, {"error": "GitHub writes are disabled"})
                body = self._body(max_bytes=4096)
                try:
                    if path.endswith("/prepare"):
                        preview = self.app.github_confirmations.prepare(self.app.github, session.token, body)
                        return self._json(200, preview)
                    if not isinstance(body, dict) or set(body) != {"confirmation"}:
                        raise ValueError("confirmation required")
                    result = self.app.github_confirmations.confirm(self.app.github, session.token, body["confirmation"])
                    return self._json(200, {"repository": result})
                except GitHubAuthError as e:
                    return self._json(403, {"error": str(e)})
                except GitHubUnavailable as e:
                    return self._json(503, {"error": str(e) + "; check GitHub before retrying"})
                except GitHubError as e:
                    return self._json(502, {"error": str(e) + "; check GitHub before retrying"})
            body = self._body(max_bytes=_json_body_cap(path))
            if path == "/api/v1/projects" or path.startswith("/api/v1/projects/"):
                if not isinstance(body, dict):
                    return self._json(400, {"error": "body must be a JSON object"})
                principal = self._project_principal()
                plan_path = self._plan_path(path)
                if plan_path:
                    project_id, kind, item_id = plan_path
                    if kind == "goal-plans":
                        plan, created = self.app.plans.create(principal, project_id, item_id, body,
                                                               self.headers.get("Idempotency-Key"))
                        return self._json(201 if created else 200, {"plan": plan, "created": created})
                    if kind == "approve":
                        if body:
                            return self._json(400, {"error": "approval body must be empty"})
                        return self._json(200, {"plan": self.app.plans.approve(principal, project_id, item_id)})
                    if kind == "goal-plan-draft":
                        return self._plan_draft(principal, project_id, item_id)
                    return self._json(404, {"error": "not found"})
                if path == "/api/v1/projects":
                    project = self.app.projects.create(principal, body.get("name"), body.get("description", ""))
                    return self._json(201, {"project": project})
                verify = self._project_evidence_verify(path)
                if verify is not None:
                    project_id, evidence_id = verify
                    rec = self.app.evidence.reverify(
                        evidence_id, project_id=project_id,
                        expected_commit=body.get("expected_commit"))
                    return self._json(200, {"evidence": rec})
                nested = self._project_nested(path)
                if nested is not None:
                    return self._project_nested_post(*nested, body)
                parsed = self._project_path(path)
                if parsed is None or parsed[1] != "goals":
                    # Deeper project routes (workspaces, voice, actions).
                    if self._post_project_resource(path, principal, body):
                        return
                    return self._json(404, {"error": "not found"})
                # The text is kept exactly as sent, as a specification; the goal
                # row carries a label of at most 20 000 characters and points at it.
                goal, spec, created = self.app.specs.create_with_goal(
                    principal, parsed[0], body.get("text"), source="api",
                    key=self.headers.get("Idempotency-Key"))
                return self._json(201 if created else 200, {
                    "goal": goal, "created": created, "spec": SpecService.summary(spec)})
            if path == "/api/github/accounts":
                try:
                    account = self.app.github_accounts.add(
                        self._project_principal(),
                        label=body.get("label"), token=body.get("token"),
                        base_url=(body.get("base_url") or None))
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                return self._json(201, {"account": account})
            if path == "/api/openhands/accounts":
                try:
                    account = self.app.openhands_accounts.add(
                        self._project_principal(),
                        label=body.get("label"), api_key=body.get("api_key"),
                        base_url=(body.get("base_url") or None),
                        transport=(body.get("transport") or "direct"),
                        default_model=(body.get("default_model") or None),
                        make_default=bool(body.get("make_default")),
                        max_concurrent_tasks=body.get("max_concurrent_tasks"),
                        daily_conversation_limit=body.get("daily_conversation_limit"))
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                return self._json(201, {"account": account})
            if path.startswith("/api/github/accounts/"):
                return self._github_account_post(path, body)
            if path.startswith("/api/openhands/accounts/"):
                return self._openhands_account_post(path, body)
            if path == "/api/openhands/pool":
                principal = self._project_principal()
                try:
                    config = self.app.account_pool.set_config(
                        principal,
                        selection_mode=body.get("selection_mode"),
                        fallback_order=body.get("fallback_order"))
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                return self._json(200, {"config": config})
            if path == "/api/openhands/diagnostics":
                # The opt-in live probe: creates and deletes one conversation.
                return self._json(200, self.app.openhands_diagnostics(
                    self._project_principal(),
                    live=bool(body.get("live")),
                    account_id=body.get("account_id"),
                    project_id=body.get("project_id")))
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
                if not UUID_RE.fullmatch(workflow_id):
                    return self._json(404, {"error": "not found"})
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
        except ConfirmationMismatch as e:
            # A wrong confirmation token is an authorization failure, not a
            # malformed request; keep it distinct from a 400.
            return self._json(403, {"error": str(e)})
        except ActionStateError as e:
            return self._json(409, {"error": str(e)})
        except IdempotencyConflict as e:
            return self._json(409, {"error": str(e)})
        except VisibilityError as e:
            return self._json(400, {"error": str(e)})
        except GitHubAuthError as e:
            return self._json(400, {"error": f"github auth: {e}"})
        except GitHubUnavailable as e:
            return self._json(503, {"error": f"github unavailable: {e}"})
        except GitHubError as e:
            return self._json(502, {"error": f"github: {e}"})
        except MultipartError as e:
            return self._json(400, {"error": str(e)})
        except (BodyTooLarge, SpecTooLarge) as e:
            # Too much to accept is its own answer, with both numbers in it.
            self._json(413, {"error": str(e)})
            return self._linger()
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except InvalidTransition as e:
            return self._json(409, {"error": str(e)})
        except SpecCorrupted as e:
            return self._json(500, {"error": str(e)})
        except CloudError as e:
            return self._json(502, {"error": f"cloud: {e}"})
        except StoreUnavailable:
            # The database could not be reached and the connection could not be
            # replaced. Say so plainly; the driver's text names hosts and ports.
            return self._json(503, {"error": "database unavailable"})
        except Exception as e:  # noqa: BLE001
            log.exception("POST %s failed", path)
            error = "internal error" if path.startswith("/api/v1/projects") else f"{type(e).__name__}: {e}"
            return self._json(500, {"error": error})

    # --------------------------------------------------------------- DELETE
    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        try:
            if not self._require_write():
                return
            parsed = self._project_subpath(path)
            if parsed is None:
                return self._json(404, {"error": "not found"})
            project_id, rest = parsed
            principal = self._project_principal()
            files = self.app.files
            # DELETE /api/v1/projects/{id}/files/{file_id}
            if len(rest) == 2 and rest[0] == "files" and UUID_RE.fullmatch(rest[1]):
                if files.delete_file(principal, project_id, rest[1]):
                    return self._json(200, {"deleted": rest[1]})
                return self._json(404, {"error": "not found"})
            # DELETE /api/v1/projects/{id}/workspaces/{ws}
            if len(rest) == 2 and rest[0] == "workspaces" and UUID_RE.fullmatch(rest[1]):
                if files.delete_workspace(principal, project_id, rest[1]):
                    return self._json(200, {"deleted": rest[1]})
                return self._json(404, {"error": "not found"})
            # DELETE /api/v1/projects/{id}/workspaces/{ws}/files/{path...}
            if (len(rest) >= 4 and rest[0] == "workspaces" and UUID_RE.fullmatch(rest[1])
                    and rest[2] == "files"):
                rel = "/".join(rest[3:])
                if files.delete_workspace_file(principal, project_id, rest[1], rel):
                    return self._json(200, {"deleted": rel})
                return self._json(404, {"error": "not found"})
            # DELETE /api/v1/projects/{id}/conversations/{cid}
            if len(rest) == 2 and rest[0] == "conversations" and UUID_RE.fullmatch(rest[1]):
                self.app.conversations.set_archived(principal, project_id, rest[1], True)
                return self._json(200, {"archived": rest[1]})
            return self._json(404, {"error": "not found"})
        except NotFound:
            return self._json(404, {"error": "not found"})
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except StoreUnavailable:
            # The database could not be reached and the connection could not be
            # replaced. Say so plainly; the driver's text names hosts and ports.
            return self._json(503, {"error": "database unavailable"})
        except Exception:  # noqa: BLE001
            log.exception("DELETE %s failed", path)
            return self._json(500, {"error": "internal error"})

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        try:
            if not self._require_write():
                return
            # PATCH /api/v1/projects/{id}/conversations/{cid}  (rename / mode)
            sub = self._project_subpath(path)
            if sub is not None:
                project_id, rest = sub
                if (len(rest) == 2 and rest[0] == "conversations"
                        and UUID_RE.fullmatch(rest[1])):
                    body = self._body(max_bytes=64 * 1024)
                    if not isinstance(body, dict):
                        return self._json(400, {"error": "body must be a JSON object"})
                    principal = self._project_principal()
                    conversation = self.app.conversations.get(principal, project_id, rest[1])
                    if "title" in body:
                        conversation = self.app.conversations.rename(
                            principal, project_id, rest[1], body["title"])
                    if "mode" in body:
                        conversation = self.app.conversations.set_mode(
                            principal, project_id, rest[1], body["mode"])
                    if "archived" in body:
                        # DELETE archives; this is the way back. Without it an
                        # archived conversation could never be restored.
                        if not isinstance(body["archived"], bool):
                            return self._json(400, {"error": "archived must be a boolean"})
                        conversation = self.app.conversations.set_archived(
                            principal, project_id, rest[1], body["archived"])
                    return self._json(200, {"conversation": conversation})
                # Any other sub-resource falls through to the project PATCH below.
            parsed = self._project_path(path)
            if parsed is None or parsed[1]:
                return self._json(404, {"error": "not found"})
            body = self._body(max_bytes=4 * 1024 * 1024)
            if not isinstance(body, dict):
                return self._json(400, {"error": "body must be a JSON object"})
            project = self.app.projects.update(self._project_principal(), parsed[0], body)
            return self._json(200, {"project": project})
        except NotFound:
            return self._json(404, {"error": "not found"})
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except StoreUnavailable:
            # The database could not be reached and the connection could not be
            # replaced. Say so plainly; the driver's text names hosts and ports.
            return self._json(503, {"error": "database unavailable"})
        except Exception:  # noqa: BLE001
            log.exception("PATCH %s failed", path)
            return self._json(500, {"error": "internal error"})

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
                web_ui.login_page("Вход не настроен на сервере: "
                    + "; ".join(reasons)
                    + ". Сгенерируйте хеш командой "
                    "`python -m orchestration.web_auth hash` и задайте "
                    "AUTH_PASSWORD_HASH в Render.",
                ),
            )
        client_ip = self._client_ip()
        # Coarse per-IP ceiling first. The durable lockout is keyed by
        # (username, IP), which a caller defeats by presenting a fresh username;
        # this caps attempts from one source regardless of what it claims.
        wait = self.app.login_throttle.check(client_ip)
        if wait > 0:
            log.warning("login throttled by IP (retry in %ss)", wait)
            return self._html(
                429,
                web_ui.login_page("Слишком много попыток входа с этого адреса. Повторите "
                    f"через {max(1, (wait + 59) // 60)} мин.",
                ),
            )
        try:
            session = self.app.auth.login(username, password, client_ip=client_ip)
        except LoginLocked as e:
            minutes = max(1, (e.retry_after + 59) // 60)
            return self._html(
                429,
                web_ui.login_page("Слишком много неудачных попыток. Повторите через "
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
                web_ui.login_page("Неверный логин или пароль. Попробуйте ещё раз."
                ),
            )
        cookie = session_cookie_header(
            session.token, ttl=self.app.auth.session_ttl, secure=self._cookie_secure()
        )
        # A successful login clears this IP's throttle, so a legitimate owner
        # who mistyped a few times is not held back after getting in.
        self.app.login_throttle.reset(client_ip)
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
    # Scrub credential-looking values from every log record, so a stray key in a
    # message or a traceback never reaches a log file.
    install_log_redaction()

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
