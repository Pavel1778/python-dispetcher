"""Direct Cloud transport: provision a sandbox, then talk to its agent-server.

The problem this solves
-----------------------
The application was written against the Cloud V1 *app-conversation* endpoint
(``POST /api/v1/app-conversations``). That endpoint provisions a sandbox and
then asks the sandbox's agent-server to create the conversation. On the live
service that inner step can fail with **HTTP 500** -- observed as a start task
that walks ``WORKING`` -> ``WAITING_FOR_SANDBOX`` -> ``SETTING_UP_SKILLS`` ->
``STARTING_CONVERSATION`` -> ``ERROR`` with
``Server error '500 Internal Server Error' ... /api/conversations``. The failure
is inside the runtime and, for the account in question, applies to every start
that carries an ``initial_message``; a start without one is ``READY`` but then
``send-message`` returns **502**. Creating a *running* Cloud conversation through
that endpoint was therefore impossible, which made the whole dispatcher inert.

The fix
-------
Provision the sandbox directly and speak to its agent-server ourselves:

1. ``POST /api/v1/sandboxes`` -> a ``SandboxInfo`` with ``id``,
   ``session_api_key`` and (once RUNNING) an exposed ``AGENT_SERVER`` URL.
2. Poll ``GET /api/v1/sandboxes?id=<id>`` until ``RUNNING``.
3. ``POST <agent_server>/api/conversations`` with the sandbox's
   ``session_api_key`` -- the exact SDK surface the local agent-server exposes,
   which does support ``client_tools``.

This is verified end-to-end against the live service: a conversation created
this way answered ``CLOUD-DIRECT-OK``, and the same path drives the Sr/Jr chain
locally.

Routing
-------
Because a conversation is created *on* a particular sandbox, every later read
(status, events, final response) must go back to that sandbox. The mapping is
persisted in ``conversation_routes`` (see migration ``0007``), so it survives a
restart: a client instance can be recreated and still resolve a conversation
started by a previous process. This is what makes the dispatcher's recovery
pass work with the direct transport, not just the in-memory case.

The class mirrors :class:`~orchestration.cloud_client.CloudClient`'s interface
so :class:`~orchestration.cloud_dispatcher.CloudDispatcher`,
:class:`~orchestration.sr_runtime.SrRuntime` and
:class:`~orchestration.workflow_verifier.WorkflowVerifier` run unchanged.
"""

from __future__ import annotations

import hashlib
import json
import io
import logging
import os
import posixpath
import uuid
import zipfile
from dataclasses import dataclass
from typing import Any

from .cloud_client import (
    FILES_DIR_TOKEN,
    CloudClient,
    CloudConversation,
    CloudError,
    CloudNotFound,
    CloudRateLimited,
    CloudRefused,
    CloudUnavailable,
    CloudValidationError,
    DeliveryError,
    SandboxFile,
    StartStatus,
    StartTask,
)
from .cloud_store import PostgresStore
from .net_guard import AddressRefused
from .openhands_client import ApiUnavailable, OpenHandsClient

log = logging.getLogger(__name__)

#: Where an agent works inside its sandbox. Matches the layout the Cloud UI uses.
DEFAULT_SANDBOX_WORKING_DIR = "/workspace/project"


@dataclass(frozen=True)
class SandboxRoute:
    """Where one conversation lives: a sandbox and how to authenticate to it."""

    sandbox_id: str
    agent_server_url: str
    session_api_key: str


class CloudDirectClient:
    """``CloudClient``-shaped client that provisions sandboxes and uses them.

    ``cloud`` does the account-level calls (sandboxes, settings, capabilities);
    an internal :class:`~orchestration.openhands_client.OpenHandsClient` does the
    HTTP to each sandbox's agent-server, reusing its request plumbing with an
    explicit host and key via ``_request_to``.
    """

    def __init__(
        self,
        cloud: CloudClient,
        store: PostgresStore,
        *,
        working_dir: str = DEFAULT_SANDBOX_WORKING_DIR,
        max_iterations: int = 200,
        reuse_sandboxes: bool = True,
        sandbox_spec_id: str | None = None,
        wait_attempts: int = 60,
        wait_interval: float = 5.0,
        llm_model: str | None = None,
        llm_base_url: str | None = None,
        llm_provider: str | None = None,
        timeout: float = 60.0,
        scope: str = "",
    ):
        from .execution_profiles import ExecutionProfiles
        self.execution_profiles = ExecutionProfiles(store)
        self.cloud = cloud
        self.store = store
        #: Whose sandboxes this client may reuse: the id of the OpenHands
        #: connection it was built for, or ``""`` for the server's own key.
        self.scope = scope
        self.working_dir = working_dir
        self.max_iterations = max_iterations
        # Reusing a running sandbox avoids paying to provision one per task. A
        # later roadmap item is a sandbox group with a warm pool; for now the
        # most recently used running sandbox is reused.
        self.reuse_sandboxes = reuse_sandboxes
        self.sandbox_spec_id = sandbox_spec_id
        self.wait_attempts = wait_attempts
        self.wait_interval = wait_interval
        self.llm_model = llm_model
        #: The LLM *provider* endpoint (not the OpenHands API URL) and an
        #: optional provider label. ``None`` means the provider's own default.
        #: These reach a sandbox's ``agent_settings.llm`` so a session runs on
        #: the configured provider, not whatever the bare sandbox defaulted to.
        self.llm_base_url = llm_base_url
        self.llm_provider = llm_provider
        # A sandbox's address comes from the service. For a connection a person
        # added, that service is held to the same rule as the connection's own
        # URL: it may not name an address inside this server's network.
        self._http = OpenHandsClient("", api_key="", timeout=timeout,
                                     address_policy=getattr(cloud, "address_policy", None))
        self._cache: dict[str, SandboxRoute] = {}

    #: This transport can place a file in the sandbox before the agent starts
    #: (the agent-server's ``/api/file`` routes) and read it back to check it.
    supports_dispatch_id = True
    supports_files = True

    def files_dirs(self) -> list[str]:
        """Where delivered files may go inside a sandbox, in order of preference.

        Never inside the working directory: the agent is told to clone the
        repository into it, and ``git clone`` refuses a directory that is not
        empty. The first choice is beside it, on the same volume; ``/tmp`` is
        the fallback for a sandbox where that is not writable. A directory is
        used only if the file read back from it is the file that was sent.
        ``NEXUS_SPEC_SANDBOX_DIR`` names one directory to use instead.
        """
        configured = os.environ.get("NEXUS_SPEC_SANDBOX_DIR", "").strip()
        if configured:
            if not configured.startswith("/"):
                raise DeliveryError("NEXUS_SPEC_SANDBOX_DIR must be an absolute path")
            return [configured.rstrip("/") or "/"]
        beside = posixpath.join(posixpath.dirname(self.working_dir.rstrip("/")) or "/",
                                ".nexus")
        return [beside, "/tmp/.nexus"]

    def _deliver_files(self, route: SandboxRoute,
                       files: list[SandboxFile]) -> tuple[str, tuple[dict[str, Any], ...]]:
        """Place *files* in the sandbox and prove they arrived whole.

        Every file is uploaded, downloaded again and compared by size and
        SHA-256. Returns the directory and what was verified. Raises
        :class:`DeliveryError` when no directory holds exact copies, and
        :class:`CloudUnavailable` when the sandbox cannot be reached at all -
        a different directory would not help with that, a later attempt might.
        """
        for item in files:
            if not item.name or "/" in item.name or item.name in (".", ".."):
                raise DeliveryError(f"{item.name!r} is not a plain file name")
            if hashlib.sha256(item.data).hexdigest() != item.sha256:
                raise DeliveryError(f"{item.name}: the data does not match its own SHA-256")
        refusals: list[str] = []
        for directory in self.files_dirs():
            try:
                placed = tuple(self._deliver_one(route, posixpath.join(directory, item.name), item)
                               for item in files)
            except DeliveryError as e:
                refusals.append(str(e))
                continue
            return directory, placed
        raise DeliveryError("the files could not be placed in the sandbox: "
                            + "; ".join(refusals))

    def _deliver_one(self, route: SandboxRoute, path: str, item: SandboxFile) -> dict[str, Any]:
        try:
            status, payload = self._http._upload_to(
                route.agent_server_url, route.session_api_key, path, item.data,
                filename=item.name)
        except ApiUnavailable as e:
            raise CloudUnavailable(str(e)) from e
        if status in (401, 403):
            raise CloudError(f"upload to the sandbox -> {status}")
        if status not in (200, 201):
            detail = payload.get("detail") if isinstance(payload, dict) else None
            raise DeliveryError(f"{path}: upload -> {status}"
                                + (f" ({str(detail)[:200]})" if detail else ""))
        try:
            status, stored = self._http._download_from(
                route.agent_server_url, route.session_api_key, path, limit=len(item.data))
        except ApiUnavailable as e:
            raise CloudUnavailable(str(e)) from e
        if status != 200:
            raise DeliveryError(f"{path}: reading the file back -> {status}")
        digest = hashlib.sha256(stored).hexdigest()
        if len(stored) != len(item.data) or digest != item.sha256:
            more = "more than " if len(stored) > len(item.data) else ""
            raise DeliveryError(
                f"{path}: the sandbox holds {more}{min(len(stored), len(item.data) + 1)} bytes "
                f"(SHA-256 {digest[:16]}…), {len(item.data)} bytes were sent "
                f"(SHA-256 {item.sha256[:16]}…)")
        return {"path": path, "bytes": len(stored), "sha256": digest}

    # ------------------------------------------------------------- sandboxes
    @staticmethod
    def workspace_key(repository: str | None, branch: str | None) -> str:
        """What a conversation's working directory will hold: the repository
        and branch it is told to clone, or ``""`` when it names none."""
        repository = (repository or "").strip()
        return f"{repository}#{(branch or '').strip()}" if repository else ""

    def _new_sandbox(
        self, *, reuse: bool | None = None, force_new: bool = False, workspace: str = ""
    ) -> SandboxRoute:
        """A usable sandbox: an existing running one, or a freshly provisioned one.

        ``force_new`` is used by the Sr start path when the caller wants a
        deterministic, self-contained conversation: reusing a sandbox is fine for
        Jr tasks but an Sr is long-lived and should not share one that may be
        reclaimed when a Jr conversation is cleaned up.

        *workspace* (``workspace_key``) keeps checkouts apart. Every
        conversation of a sandbox works in the same directory, so a sandbox is
        reused only for the repository and branch it was started for: a task
        on another repository, or on another branch of the same one, gets a
        sandbox of its own and never finds - or changes - someone else's
        working tree. Tasks on one branch do share a sandbox; they never run
        at the same time (``nexus_lab_store``: one task per branch).
        """
        if reuse is None:
            reuse = self.reuse_sandboxes
        if reuse and not force_new:
            # Each pass either returns or takes one sandbox off the list.
            while (row := self.store.running_sandbox(self.scope, workspace)) is not None:
                if self._still_running(row["sandbox_id"]):
                    # Written down as handed out now: until the service has
                    # learnt of the conversation about to be started on it,
                    # the conversations it already holds are not deleted
                    # (``nexus_lab_store.SANDBOX_QUIET_SECONDS``).
                    self.store.mark_sandbox_status(row["sandbox_id"], "RUNNING")
                    return SandboxRoute(
                        sandbox_id=row["sandbox_id"],
                        agent_server_url=row["agent_server_url"],
                        session_api_key=row["session_api_key"],
                    )
        info = self.cloud.start_sandbox(sandbox_spec_id=self.sandbox_spec_id)
        sandbox_id = info.get("id")
        if not sandbox_id:
            raise CloudError("start_sandbox returned no id")
        info = self.cloud.wait_for_sandbox(
            sandbox_id, attempts=self.wait_attempts, interval=self.wait_interval
        )
        url = self.cloud.agent_server_url(info)
        key = info.get("session_api_key")
        if not url or not key:
            raise CloudError(f"sandbox {sandbox_id} has no agent-server url/key")
        self.store.save_sandbox(
            sandbox_id, agent_server_url=url, session_api_key=key, status="RUNNING",
            owner=self.scope, workspace=workspace,
        )
        return SandboxRoute(sandbox_id=sandbox_id, agent_server_url=url, session_api_key=key)

    def _still_running(self, sandbox_id: str) -> bool:
        """Whether the Cloud still runs a sandbox this application recorded.

        "RUNNING" in ``cloud_sandboxes`` is what was true when the sandbox was
        started. The Cloud pauses an idle sandbox and removes an old one, and
        nothing told this table: the row went on saying RUNNING, the sandbox
        was handed out again for every new conversation, and every one of them
        failed against a host that no longer answered. So the Cloud is asked
        before a sandbox is reused, and a sandbox it does not report as running
        is recorded as what it is and not offered again.

        A Cloud that cannot be reached is not an answer: that error is raised,
        and the start is tried again later.
        """
        try:
            status = str(self.cloud.get_sandbox(sandbox_id).get("status") or "UNKNOWN")
        except CloudNotFound:
            status = "MISSING"
        if status == "RUNNING":
            return True
        self.store.mark_sandbox_status(sandbox_id, status)
        return False

    def _route_for(self, conversation_id: str) -> SandboxRoute:
        cached = self._cache.get(conversation_id)
        if cached is not None:
            return cached
        row = self.store.conversation_route(conversation_id)
        if row is None:
            raise CloudNotFound(
                f"no sandbox route for conversation {conversation_id}"
            )
        route = SandboxRoute(
            sandbox_id=row["sandbox_id"],
            agent_server_url=row["agent_server_url"],
            session_api_key=row["session_api_key"],
        )
        self._cache[conversation_id] = route
        return route

    def _send(
        self, route: SandboxRoute, method: str, path: str,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        """One request to a sandbox's agent-server, with its own key."""
        try:
            return self._http._request_to(
                route.agent_server_url, route.session_api_key, method, path, body
            )
        except AddressRefused as e:
            # The service named a sandbox address this server does not talk to.
            raise CloudRefused(f"{method} {path}: {e}") from e
        except ApiUnavailable as e:
            # A 5xx from the agent-server is transient from the caller's view.
            raise CloudUnavailable(str(e)) from e
        except (CloudError, CloudUnavailable):
            raise
        except Exception as e:  # transport failures from the sandbox are transient
            raise CloudUnavailable(f"{method} {path}: {type(e).__name__}") from e

    # ------------------------------------------------------------- creation
    def start_conversation(
        self,
        *,
        initial_message: str,
        dispatch_id: str | None = None,
        selected_repository: str | None = None,
        selected_branch: str | None = None,
        title: str | None = None,
        parent_conversation_id: str | None = None,
        tags: dict[str, str] | None = None,
        secrets: dict[str, Any] | None = None,
        client_tools: list[dict[str, Any]] | None = None,
        agent_profile_id: str | None = None,
        agent_type: str | None = None,
        plugins: list[dict[str, Any]] | None = None,
        files: list[SandboxFile] | None = None,
        profile_scope=(None, None),
    ) -> StartTask:
        """Provision a sandbox (or reuse one) and create the conversation on it.

        Returns an already-``READY`` :class:`~orchestration.cloud_client.StartTask`
        carrying the conversation id, sandbox id and agent-server URL, so the
        shared dispatcher's ``_await_start`` returns immediately and the
        two-phase Cloud start task is not needed.

        **Repository selection is not native here.** A sandbox conversation
        starts in an empty ``LocalWorkspace``; there is no
        ``selected_repository`` field on the agent-server create call. Silently
        ignoring it would run a repository task against the wrong (empty)
        checkout with no error, so the behavior is explicit and controlled by
        ``OPENHANDS_DIRECT_REPO_MODE``:

        * ``prompt`` (default) — prepend a deterministic instruction telling the
          agent to clone ``owner/name`` at the branch and work there. The task is
          functionally correct; the selection is via instruction, not a native
          field, and ``supports_repository_selection`` reports ``False``.
        * ``refuse`` — raise ``CloudError`` so the task is dead-lettered with a
          clear reason. Use this when a repository task must never run without
          native selection.
        * ``allow`` — the legacy behavior: ignore the fields. Only for
          repository-less analysis or a transport that has been verified to
          select the repository itself.

        The account-selection fields (``agent_profile_id``, ``agent_type``,
        ``plugins``) are accepted for interface parity and not forwarded; the
        sandbox uses its own account settings.

        **Files come first.** *files* are placed in the sandbox and read back
        before the conversation is created (see :meth:`_deliver_files`); the
        message refers to their directory as ``FILES_DIR_TOKEN``, which is
        replaced by the directory that was verified. If they cannot be placed
        exactly, nothing is created and :class:`DeliveryError` says why.
        """
        profile = self.execution_profiles.select(profile_scope, self.scope, 'direct')
        if not profile:
            initial_message = self._apply_repository_mode(initial_message, selected_repository, selected_branch)
        elif selected_repository and not os.getenv('NEXUS_MANAGED_CHECKOUT_ROOT'):
            self.execution_profiles.block(profile, 'managed checkout root not configured')
        # A supplied conversation UUID is idempotent only on its ORIGINAL
        # sandbox. The lab serializes send permits; a recovery is a read.
        known = self.store.conversation_route(dispatch_id) if dispatch_id else None
        workspace = self.workspace_key(selected_repository, selected_branch)
        if profile:
            # Never warm-reuse across projects, including a shared account and branch.
            workspace = 'managed:' + hashlib.sha256(json.dumps([profile_scope[0], profile['scope'], workspace]).encode()).hexdigest()
        route = self._route_for(dispatch_id) if known else self._new_sandbox(
            force_new=client_tools is not None or bool(profile and not selected_repository),
            workspace=workspace,
        )
        source = (self.profile_source(profile, route=route, requested_agent=agent_profile_id) if profile
                  else {'agent_settings': self._sandbox_agent_settings(route)})
        working_dir = self.working_dir
        if profile and selected_repository:
            working_dir = self.execution_profiles.checkout(profile, profile_scope, dispatch_id, selected_repository,
                                                          selected_branch, lambda m,p,b: self._send(route,m,p,b))
        delivered: tuple[dict[str, Any], ...] = ()
        if files:
            directory, delivered = self._deliver_files(route, files)
            initial_message = initial_message.replace(FILES_DIR_TOKEN, directory)
        if FILES_DIR_TOKEN in initial_message:
            # A message that points at files nobody delivered would start an
            # agent on a task it cannot read.
            raise DeliveryError("the message refers to delivered files, but none were sent")
        conversation_id = dispatch_id or self._conversation_id(initial_message, title)
        body: dict[str, Any] = {
            "conversation_id": conversation_id,
            "workspace": {"kind": "LocalWorkspace", "working_dir": working_dir},
            "max_iterations": self.max_iterations,
            "confirmation_policy": {"kind": "NeverConfirm"},
            **source,
        }
        if title:
            body["title"] = title
        if tags:
            body["tags"] = tags
        if secrets:
            # The agent-server's StartConversationRequest carries ``secrets``
            # (a name -> value mapping the agent's environment gets), so a task
            # that must authenticate - a private clone, a push - can be given a
            # credential. The app transport forwards this; the direct transport
            # used to accept it and drop it silently, which is worse than
            # refusing: the caller believed the agent had the credential.
            body["secrets"] = secrets
        if client_tools is not None:
            body["client_tools"] = client_tools
        if initial_message:
            body["initial_message"] = {
                "role": "user",
                "content": [{"type": "text", "text": initial_message}],
                "run": True,
            }
        if dispatch_id and not known:
            self.store.save_conversation_route(dispatch_id, sandbox_id=route.sandbox_id,
                                              agent_server_url=route.agent_server_url,
                                              session_api_key=route.session_api_key)
        expected = self.execution_profiles.session_expectation(profile, profile_scope, dispatch_id, working_dir) if profile else None
        status, payload = self._send(route, "POST", "/api/conversations", body)
        if status == 429:
            raise CloudRateLimited("sandbox conversation capacity full")
        if status == 422:
            raise CloudValidationError("create on sandbox rejected (422)")
        if status not in (200, 201):
            raise CloudError(f"create on sandbox -> {status}")
        if dispatch_id and (not isinstance(payload, dict) or str(payload.get('id')) != dispatch_id):
            raise CloudUnavailable('ambiguous conversation receipt')
        actual_id = str((payload or {}).get("id") or conversation_id)
        if expected:
            self._confirm_session(route, conversation_id, expected, tags)
        self.store.save_conversation_route(
            actual_id,
            sandbox_id=route.sandbox_id,
            agent_server_url=route.agent_server_url,
            session_api_key=route.session_api_key,
        )
        self._cache[actual_id] = route
        return StartTask(
            id=actual_id,
            status=StartStatus.READY.value,
            app_conversation_id=actual_id,
            sandbox_id=route.sandbox_id,
            agent_server_url=route.agent_server_url,
            files=delivered,
        )

    def _conversation_id(self, initial_message: str, title: str | None) -> str:
        # Deterministic so a retried create on the same sandbox is idempotent
        # (the agent-server returns the existing conversation for the same id).
        seed = f"cloud-direct:{title or ''}\x00{initial_message or ''}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))

    #: The direct transport has no native repository-selection field on the
    #: agent-server create call. Callers (and the UI diagnostics) read this.
    supports_repository_selection = False

    #: The agent-server's ``StartConversationRequest.secrets`` is a mapping of
    #: name -> ``StaticSecret`` (``{"kind": "StaticSecret", "value": ...}``), and
    #: the server masks the value in the agent's own output. That is how a task
    #: can be given a Git credential (a private clone, a push) without the value
    #: reaching the transcript.
    supports_git_secrets = True

    @staticmethod
    def git_secret(token: str) -> dict[str, Any]:
        """The ``secrets`` payload that gives the agent a GitHub token.

        The name is the conventional one the agent's ``git`` already looks for
        when the repository's clone URL is rewritten; the value is a
        ``StaticSecret`` so the agent-server injects it and masks it.
        """
        return {"GITHUB_TOKEN": {"kind": "StaticSecret", "value": token}}

    @property
    def base_url(self) -> str:
        """The Cloud account URL, as the app transport exposes it.

        ``/api/system`` and ``/api/config`` report ``client.base_url`` so an
        operator can see which Cloud the service is pointed at. This class
        *composes* a :class:`~orchestration.cloud_client.CloudClient` instead of
        subclassing it, so before this property existed both endpoints raised
        ``AttributeError`` and answered 500 — on the default transport, the one
        that is actually running. Delegating keeps one source of truth for the
        URL (the inner client's validated value) rather than a second copy that
        could drift.
        """
        return self.cloud.base_url

    def _apply_repository_mode(self, message: str, repository: str | None,
                               branch: str | None) -> str:
        """Handle repository selection for a sandbox conversation.

        See :meth:`start_conversation` for the modes. Returns the (possibly
        prefixed) initial message.
        """
        if not repository:
            return message
        mode = (os.environ.get("OPENHANDS_DIRECT_REPO_MODE") or "prompt").strip().lower()
        if mode == "refuse":
            raise CloudError(
                "the direct transport cannot select a repository natively; "
                f"task asked for {repository}@{branch or 'default'}. Set "
                "OPENHANDS_DIRECT_REPO_MODE=prompt to clone-by-instruction, or use "
                "OPENHANDS_TRANSPORT=app which carries selected_repository."
            )
        if mode == "allow":
            return message
        # Default: prompt. The workspace starts empty, so instruct the agent to
        # clone the repository and branch first. This is a real, deterministic
        # instruction, not a silent no-op.
        ref = f"{repository}@{branch}" if branch else repository
        prefix = (
            f"Work in the repository {repository}"
            + (f" on branch {branch}" if branch else "")
            + ". The workspace is empty: clone it first (e.g. "
            f"`git clone https://github.com/{repository}.git .`"
            + (f" && `git checkout {branch}`" if branch else "")
            + ") and do all work there.\n\n"
        )
        log.info("direct transport: repository task for %s via instruction", ref)
        return prefix + message

    def _confirm_session(self, route, cid, expected, tags):
        try:
            reply = self._send(route, 'GET', '/api/conversations/' + str(uuid.UUID(cid)))
        except CloudError:
            raise CloudUnavailable('created session not confirmed on original route') from None
        self.execution_profiles.check_session(reply, cid, expected, tags)

    def confirm_session(self, cid, expected, tags):
        self._confirm_session(self._route_for(cid), cid, expected, tags)

    def profile_source(self, profile, *, route=None, test_model=False, requested_agent=None):
        if route is None:
            # A connection can own multiple project sandboxes. There is no authoritative
            # target until dispatch: don't mutate whichever sandbox happens to be newest.
            self.execution_profiles.block(profile, 'sandbox not assigned; reconcile on the selected create target')
        return self.execution_profiles.reconcile(
            profile, lambda m, p, b: self._send(route, m, p, b), test_model=test_model,
            target='sandbox:' + hashlib.sha256(route.sandbox_id.encode()).hexdigest()[:16],
            requested_agent=requested_agent)

    def _sandbox_agent_settings(self, route: SandboxRoute) -> dict[str, Any]:
        """The agent-settings document from the sandbox's own ``/api/settings``.

        Read from the sandbox so the dispatcher never holds an LLM key itself.
        ``llm_model`` overrides the model when the sandbox's default points at a
        provider whose key is not present -- a bare sandbox defaults to a model
        with no key, and a conversation on it then fails with an auth error.
        """
        status, payload = self._send(route, "GET", "/api/settings")
        if status != 200 or not isinstance(payload, dict):
            raise CloudUnavailable(f"sandbox settings -> {status}")
        settings = payload.get("agent_settings")
        if not isinstance(settings, dict):
            raise CloudUnavailable("sandbox returned no agent_settings")
        llm = dict(settings.get("llm") or {})
        if self.llm_model:
            llm["model"] = self.llm_model
        if self.llm_base_url:
            # The provider endpoint the model is reached at. Distinct from the
            # OpenHands API URL: this is where the sandbox's LLM calls go.
            llm["base_url"] = self.llm_base_url
        if llm != (settings.get("llm") or {}):
            settings = {**settings, "llm": llm}
        return settings

    # --------------------------------------------------------------- reads
    def get_start_task(self, start_task_id: str) -> StartTask | None:
        """There are no start tasks on the direct path; ``None`` means "done"."""
        return None

    def get_conversation(self, conversation_id: str) -> CloudConversation:
        route = self._route_for(conversation_id)
        status, payload = self._send(route, "GET", f"/api/conversations/{conversation_id}")
        if status == 404:
            raise CloudNotFound(conversation_id)
        if status != 200:
            raise CloudError(f"get_conversation -> {status}")
        return self._view(payload or {}, route)

    def search_conversations(
        self, *, limit: int = 50, page_id: str | None = None,
        tag: tuple[str, str] | None = None,
    ) -> tuple[list[CloudConversation], str | None]:
        """Search every known sandbox for conversations, filtering by tag.

        The Cloud app-level search also sees these conversations, but it does not
        expose the tags set through the agent-server, so a tag-filtered orphan
        lookup has to run against the sandboxes themselves. The window is bounded
        because this is only used for recovery, not for listing.
        """
        results: list[CloudConversation] = []
        for row in self.store.list_sandboxes(owner=self.scope):
            route = SandboxRoute(
                sandbox_id=row["sandbox_id"],
                agent_server_url=row["agent_server_url"],
                session_api_key=row["session_api_key"],
            )
            try:
                status, payload = self._send(
                    route, "GET", f"/api/conversations/search?limit={limit}"
                )
            except CloudError:
                continue
            if status != 200 or not isinstance(payload, dict):
                continue
            for item in payload.get("items") or []:
                results.append(self._view(item, route))
        if tag is not None:
            key, value = tag
            results = [c for c in results if c.tags.get(key) == value]
        return results, None

    def list_events(
        self, conversation_id: str, *, limit: int = 100, max_pages: int = 10
    ) -> list[dict[str, Any]]:
        route = self._route_for(conversation_id)
        collected: list[dict[str, Any]] = []
        page_id: str | None = None
        for _ in range(max_pages):
            path = f"/api/conversations/{conversation_id}/events/search?limit={limit}"
            if page_id:
                path += f"&page_id={page_id}"
            status, payload = self._send(route, "GET", path)
            if status == 404:
                raise CloudNotFound(conversation_id)
            if status != 200 or not isinstance(payload, dict):
                raise CloudError(f"list_events -> {status}")
            collected.extend(payload.get("items") or [])
            page_id = payload.get("next_page_id")
            if not page_id:
                break
        return collected

    def send_message(self, conversation_id: str, text: str, *, run: bool = True) -> bool:
        route = self._route_for(conversation_id)
        status, _ = self._send(
            route,
            "POST",
            f"/api/conversations/{conversation_id}/events",
            {"role": "user", "content": [{"type": "text", "text": text}], "run": run},
        )
        if status == 404:
            raise CloudNotFound(conversation_id)
        if status == 429:
            raise CloudUnavailable("sandbox conversation capacity full")
        if status not in (200, 201):
            raise CloudError(f"send_message -> {status}")
        return True

    def final_response(self, conversation_id: str) -> str | None:
        """The agent's final text, or ``None`` when there is none.

        An empty response is never treated as success, matching the dispatcher's
        rule that "finished with no answer" is incomplete, not done.
        """
        route = self._route_for(conversation_id)
        status, payload = self._send(
            route, "GET", f"/api/conversations/{conversation_id}/agent_final_response"
        )
        if status == 404:
            raise CloudNotFound(conversation_id)
        if status != 200:
            raise CloudError(f"final_response -> {status}")
        text = (payload or {}).get("response") or ""
        return text.strip() or None

    def event_count(self, conversation_id: str) -> int:
        route = self._route_for(conversation_id)
        status, payload = self._send(
            route, "GET", f"/api/conversations/{conversation_id}/events/count"
        )
        if status != 200:
            raise CloudError(f"event_count -> {status}")
        if isinstance(payload, dict):
            return int(payload.get("count", 0))
        return int(payload or 0)

    def failure_classification(self, conversation_id: str) -> dict[str, Any] | None:
        """The runtime's own classification of the last error, if any."""
        route = self._route_for(conversation_id)
        status, payload = self._send(
            route,
            "GET",
            f"/api/conversations/{conversation_id}/events/search"
            "?limit=50&sort_order=TIMESTAMP_DESC",
        )
        if status == 404:
            raise CloudNotFound(conversation_id)
        if status != 200:
            raise CloudError(f"failure_classification -> {status}")
        for event in (payload or {}).get("items") or []:
            classification = event.get("classification")
            if isinstance(classification, dict):
                return classification
        return None

    def update_conversation(self, conversation_id: str, *, title: str | None = None) -> bool:
        if title is None:
            return True
        route = self._route_for(conversation_id)
        status, _ = self._send(
            route, "PATCH", f"/api/conversations/{conversation_id}", {"title": title}
        )
        return status in (200, 201)

    def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a conversation everywhere it is kept; ``True`` when there
        was one to delete. See :meth:`remove_conversation`."""
        return self.remove_conversation(conversation_id) == "deleted"

    def remove_conversation(self, conversation_id: str) -> str:
        """Delete a conversation and say what became of it: ``deleted`` or
        ``missing`` (it was nowhere to be found).

        A conversation of this transport is kept in three places, and each is
        dealt with here:

        * **the account.** The service records every conversation that is
          started on one of its sandboxes, and that record is what a person
          sees in their OpenHands account. Deleting the conversation on its
          sandbox alone does not remove it (the sandbox reports the deletion
          and the service does nothing with the report), so the account API
          is asked first - with the same question and the same proof as on the
          ``app`` transport (:meth:`CloudClient.remove_conversation`). Deleting
          there also removes the sandbox once it holds no conversation of the
          account. A failure here is raised: nothing else is touched, and the
          whole deletion is tried again.
        * **the sandbox.** While other conversations share the sandbox the
          service leaves this one's copy on it, so it is deleted directly, by
          its own id. It concerns nothing else on the sandbox. A sandbox that
          does not answer - paused, or already removed along with everything
          on it - is not an error: there is nothing to ask of it.
        * **this application.** The route to the conversation holds the
          sandbox's session key. Once the conversation is gone the route is
          dropped, and the key with it.
        """
        outcome = self.cloud.remove_conversation(conversation_id)
        row = self.store.conversation_route(conversation_id)
        if row is not None:
            route = SandboxRoute(sandbox_id=row["sandbox_id"],
                                 agent_server_url=row["agent_server_url"],
                                 session_api_key=row["session_api_key"])
            try:
                status, _ = self._send(route, "DELETE",
                                       f"/api/conversations/{conversation_id}")
            except CloudError:
                status = 0
            if status in (200, 204):
                outcome = "deleted"
            self.store.delete_conversation_route(conversation_id)
        self._cache.pop(conversation_id, None)
        return outcome

    def download_file(self, conversation_id: str, path: str,
                      *, limit: int) -> tuple[int, bytes]:
        """``GET /api/file/download?path=`` on the sandbox the conversation
        lives on, with that sandbox's own key. ``(status, body)`` with at most
        ``limit + 1`` bytes; every HTTP answer is returned, not raised (a 404
        is "not there", a fact). A network failure raises
        :class:`ApiUnavailable`. The route comes from the store, so a path is
        only ever asked of the sandbox the conversation is actually on.
        """
        route = self._route_for(conversation_id)
        return self._http._download_from(
            route.agent_server_url, route.session_api_key, path, limit=limit)

    def pause(self, conversation_id: str) -> None:
        route = self._route_for(conversation_id)
        status, _ = self._send(
            route, "POST", f"/api/conversations/{conversation_id}/pause"
        )
        if status not in (200, 201):
            raise CloudError(f"pause -> {status}")

    #: The sandbox agent-server has a conversation-scoped pause, so a stop here
    #: is real; the branch can be held until it succeeds.
    supports_conversation_stop = True

    def stop_conversation(self, conversation_id: str) -> bool:
        """Stop the agent of one conversation; the sandbox and every other
        conversation on it go on. Returns whether the stop was taken; the
        conversation and its transcript are kept.

        Sandboxes are shared here, so the stop is conversation-scoped. The
        agent-server has two calls for it: ``/interrupt`` cancels a request to
        the model that is in flight, ``/pause`` stops the loop and keeps the
        conversation. ``/interrupt`` is newer (the client OpenHands ships calls
        it; it is not on every server), so it is tried and a refusal of it is
        not an error; ``/pause`` is what decides.

        ``False`` on a failure the caller can tolerate (the conversation is
        gone, the sandbox does not answer): nothing was stopped by this call.
        """
        try:
            route = self._route_for(conversation_id)
            try:
                self._send(route, "POST",
                           f"/api/conversations/{conversation_id}/interrupt", {})
            except CloudError:
                pass
            status, _ = self._send(
                route, "POST", f"/api/conversations/{conversation_id}/pause", {})
        except CloudError:
            return False
        return status in (200, 201, 202, 204)

    def activity_marker(self, conversation_id: str) -> str | None:
        """A value that changes whenever the conversation produces an event:
        how many events it has. ``None`` when that cannot be read now."""
        try:
            return str(self.event_count(conversation_id))
        except CloudNotFound:
            raise
        except CloudError:
            return None

    # ------------------------------------------------------- full-output files
    #: The agent-server cuts a terminal ``ObservationEvent`` at this many
    #: characters (measured on v1.60.1) and writes the whole output to a file
    #: beside the conversation. An observation whose content is exactly this
    #: long was cut; the full text is in ``terminal_output_<sha256[:8]>.txt``
    #: and the truncated content is an exact **prefix** of it. The event names
    #: only the directory (``observation.full_output_save_dir``), which is set
    #: on most observations - it is a configuration value, not a truncation
    #: flag - so the length is what decides.
    TERMINAL_OUTPUT_CAP_CHARS = 30_000
    TERMINAL_OUTPUT_PREFIX = "terminal_output_"
    #: The whole conversation directory as a ZIP; bounded so one request cannot
    #: pull an unbounded archive into memory. 64 MiB holds the longest
    #: conversation measured here (3 790 events) several times over.
    TRAJECTORY_MAX_BYTES = 64 * 1024 * 1024
    #: The decompressed size one output file may have. The compressed archive is
    #: already bounded, but a small archive can expand enormously (a ZIP bomb),
    #: so the member is bounded on its own too - a full terminal output is a
    #: text log, and 64 MiB is far more than any real one.
    OUTPUT_MAX_BYTES = 64 * 1024 * 1024

    def truncated_outputs(self, conversation_id: str) -> list[dict[str, Any]]:
        """Every terminal observation of a conversation whose output was cut.

        An observation is truncated when its content is exactly
        :attr:`TERMINAL_OUTPUT_CAP_CHARS` long - the server's own cut-off - and
        it names a save directory. Returns the event id and the number of
        characters shown; the *content* is read on demand by
        :meth:`full_terminal_output`.
        """
        out: list[dict[str, Any]] = []
        for event in self.list_events(conversation_id):
            if event.get("kind") != "ObservationEvent":
                continue
            obs = event.get("observation")
            if not isinstance(obs, dict) or not obs.get("full_output_save_dir"):
                continue
            text = "".join(
                c.get("text", "")
                for c in (obs.get("content") or [])
                if isinstance(c, dict)
            )
            if len(text) < self.TERMINAL_OUTPUT_CAP_CHARS:
                continue
            out.append({
                "event_id": event.get("id"),
                "shown_chars": len(text),
                "truncated": True,
            })
        return out

    def full_terminal_output(self, conversation_id: str, prefix: str) -> dict[str, Any]:
        """The complete terminal output whose preview is *prefix*.

        The full output is a file the agent-server wrote beside the
        conversation; the only conversation-scoped way to reach it is the
        trajectory archive (``download-trajectory``), which packs the
        conversation's own directory - ``observations/`` included - and refuses
        any path outside it. The archive is read whole (bounded), the output
        file whose bytes start with *prefix* is chosen, and its bytes are
        returned. *prefix* is the truncated text the event showed, so the match
        is exact, not a guess.

        Raises :class:`CloudNotFound` when no file starts with *prefix* (the
        output was never written, or the retention window passed), and
        :class:`CloudUnavailable` when the sandbox cannot be reached or the
        archive is larger than :attr:`TRAJECTORY_MAX_BYTES`.
        """
        route = self._route_for(conversation_id)
        try:
            status, blob = self._http.download_trajectory(
                route.agent_server_url, route.session_api_key, conversation_id,
                limit=self.TRAJECTORY_MAX_BYTES,
            )
        except ApiUnavailable as e:
            raise CloudUnavailable(str(e)) from e
        if status in (404, 422):
            raise CloudNotFound(f"no trajectory for conversation {conversation_id}")
        if status != 200:
            raise CloudError(f"download-trajectory -> {status}")
        if len(blob) > self.TRAJECTORY_MAX_BYTES:
            raise CloudUnavailable(
                f"the conversation archive is larger than "
                f"{self.TRAJECTORY_MAX_BYTES} bytes")
        wanted = prefix.encode("utf-8", "replace")
        try:
            archive = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile as e:
            raise CloudError("the trajectory archive could not be read") from e
        with archive:
            for name in archive.namelist():
                if not name.endswith(".txt") or ".." in name.split("/"):
                    continue
                base = name.rsplit("/", 1)[-1]
                if not base.startswith(self.TERMINAL_OUTPUT_PREFIX):
                    continue
                data = self._http.bounded_zip_member(
                    archive, name, limit=self.OUTPUT_MAX_BYTES)
                if data is None:
                    # A member over the decompressed limit is skipped, never
                    # buffered: a small archive must not expand into memory.
                    continue
                if data.startswith(wanted):
                    return {
                        "name": base,
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "text": data.decode("utf-8", "replace"),
                    }
        raise CloudNotFound("no full-output file matches the truncated event")

    def conversation_skills(self, conversation_id: str) -> list[str]:
        try:
            conv = self.get_conversation(conversation_id)
        except CloudError:
            return []
        raw = conv.raw or {}
        skills = raw.get("activated_knowledge_skills") or raw.get("invoked_skills") or []
        out: list[str] = []
        for item in skills:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and item.get("name"):
                out.append(str(item["name"]))
        return out

    # --------------------------------------------------- account-level calls
    def health(self) -> bool:
        """Healthy when the Cloud account API answers (the app level)."""
        return self.cloud.health()

    def ping(self) -> dict[str, Any]:
        """Reachability of the account API, distinct from health/authorization."""
        return self.cloud.ping()

    def get_settings(self) -> dict[str, Any]:
        return self.cloud.get_settings()

    def tool_catalog(self) -> dict[str, Any]:
        """The agent-server's own tool catalog, read from a real sandbox.

        Reuses a running sandbox when one is recorded (cheap), otherwise
        provisions one — the same policy the live conversation probe uses. The
        catalog comes from the sandbox the agent would actually run in, so it is
        the tools that agent has, not a list this client assumed.

        A 404 means the server predates the catalog route; that is
        ``ToolCatalogUnsupported`` (``BLOCKED``), not a failure — the sandbox
        answered, it just does not publish its tools. Any other non-200 is a
        real error and is raised.
        """
        from .openhands_tools import ToolCatalogUnsupported, normalize_catalog

        route = self._new_sandbox()
        status, payload = self._send(route, "GET", "/api/tools/catalog")
        if status == 404:
            raise ToolCatalogUnsupported("the sandbox exposes no /api/tools/catalog")
        if status != 200 or not isinstance(payload, dict):
            raise CloudUnavailable(f"tool catalog -> {status}")
        return {"tools": normalize_catalog(payload), "raw": payload}

    def account_capabilities(self) -> dict[str, Any]:
        return self.cloud.account_capabilities()

    def compatibility(self) -> dict[str, Any]:
        """What the Cloud account API says of itself (see ``CloudClient``)."""
        return self.cloud.compatibility()

    def web_url(self, conversation_id: str) -> str:
        return self.cloud.web_url(conversation_id)

    # ----------------------------------------------------------------- utils
    @staticmethod
    def _view(payload: dict[str, Any], route: SandboxRoute) -> CloudConversation:
        raw_status = str((payload or {}).get("execution_status", "idle"))
        return CloudConversation(
            id=str((payload or {}).get("id", "")),
            title=(payload or {}).get("title"),
            sandbox_id=route.sandbox_id,
            execution_status=raw_status,
            sandbox_status="RUNNING",
            selected_repository=None,
            selected_branch=None,
            conversation_url=None,
            sub_conversation_ids=list((payload or {}).get("sub_conversation_ids") or []),
            tags=dict((payload or {}).get("tags") or {}),
            raw=payload if isinstance(payload, dict) else {},
        )


def build_direct_client(
    store: PostgresStore,
    *,
    cloud: CloudClient | None = None,
    reuse_sandboxes: bool = True,
    llm_model: str | None = None,
    llm_base_url: str | None = None,
    llm_provider: str | None = None,
    **kwargs: Any,
) -> CloudDirectClient:
    """Construct a :class:`CloudDirectClient` from the environment.

    ``llm_model`` (or ``OPENHANDS_DEFAULT_MODEL``) and ``llm_base_url`` (or
    ``OPENHANDS_LLM_BASE_URL``) are applied to every sandbox conversation, so a
    sandbox whose default model/endpoint has no key still runs on the configured
    provider. ``llm_base_url`` is the LLM provider endpoint, never the OpenHands
    API URL.
    """
    import os

    return CloudDirectClient(
        cloud or CloudClient(),
        store,
        reuse_sandboxes=reuse_sandboxes,
        llm_model=llm_model or os.environ.get("OPENHANDS_DEFAULT_MODEL"),
        llm_base_url=llm_base_url or os.environ.get("OPENHANDS_LLM_BASE_URL") or None,
        llm_provider=llm_provider,
        **kwargs,
    )


def build_cloud_client(
    store: PostgresStore,
    *,
    transport: str | None = None,
    **kwargs: Any,
) -> CloudClient | CloudDirectClient:
    """Pick the Cloud transport from ``OPENHANDS_TRANSPORT``.

    ``direct`` (the default) provisions a sandbox and uses its agent-server, the
    only path verified to create a *running* conversation on the live service.
    ``app`` keeps the legacy app-conversation client, for environments where its
    sandbox provisioning works.

    A ``CloudDirectClient`` still uses a :class:`CloudClient` internally for the
    account-level calls, so both transports share one implementation.
    """
    import os

    choice = (transport or os.environ.get("OPENHANDS_TRANSPORT") or "direct").lower()
    if choice in ("app", "app-conversations", "legacy"):
        from .execution_profiles import ExecutionProfiles
        client = CloudClient()
        client.execution_profiles = ExecutionProfiles(store)
        return client
    if choice == 'local':
        from .local_runtime import LocalCloudClient, DEFAULT_WORKSPACE_ROOT
        return LocalCloudClient(os.environ['LOCAL_AGENT_SERVER_URL'], store=store,
                                workspace_root=os.environ.get('OHP_WORKSPACE_ROOT', DEFAULT_WORKSPACE_ROOT))
    if choice not in ("direct", "sandbox", "sandboxes"):
        raise ValueError(
            f"unknown OPENHANDS_TRANSPORT {choice!r} (expected 'direct', 'app' or 'local')"
        )
    return build_direct_client(store, **kwargs)
