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

import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

from .cloud_client import (
    CloudClient,
    CloudConversation,
    CloudError,
    CloudNotFound,
    CloudUnavailable,
    StartStatus,
    StartTask,
)
from .cloud_store import PostgresStore
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
        timeout: float = 60.0,
    ):
        self.cloud = cloud
        self.store = store
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
        self._http = OpenHandsClient("", api_key="", timeout=timeout)
        self._cache: dict[str, SandboxRoute] = {}

    # ------------------------------------------------------------- sandboxes
    def _new_sandbox(
        self, *, reuse: bool | None = None, force_new: bool = False
    ) -> SandboxRoute:
        """A usable sandbox: an existing running one, or a freshly provisioned one.

        ``force_new`` is used by the Sr start path when the caller wants a
        deterministic, self-contained conversation: reusing a sandbox is fine for
        Jr tasks but an Sr is long-lived and should not share one that may be
        reclaimed when a Jr conversation is cleaned up.
        """
        if reuse is None:
            reuse = self.reuse_sandboxes
        if reuse and not force_new:
            row = self.store.running_sandbox()
            if row:
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
            sandbox_id, agent_server_url=url, session_api_key=key, status="RUNNING"
        )
        return SandboxRoute(sandbox_id=sandbox_id, agent_server_url=url, session_api_key=key)

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
        selected_repository: str | None = None,
        selected_branch: str | None = None,
        title: str | None = None,
        parent_conversation_id: str | None = None,
        tags: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        client_tools: list[dict[str, Any]] | None = None,
        agent_profile_id: str | None = None,
        agent_type: str | None = None,
        plugins: list[dict[str, Any]] | None = None,
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
        """
        initial_message = self._apply_repository_mode(
            initial_message, selected_repository, selected_branch
        )
        route = self._new_sandbox(
            force_new=client_tools is not None,  # an Sr gets its own sandbox
        )
        conversation_id = self._conversation_id(initial_message, title)
        agent_settings = self._sandbox_agent_settings(route)
        body: dict[str, Any] = {
            "conversation_id": conversation_id,
            "workspace": {"kind": "LocalWorkspace", "working_dir": self.working_dir},
            "max_iterations": self.max_iterations,
            "confirmation_policy": {"kind": "NeverConfirm"},
            "agent_settings": agent_settings,
        }
        if title:
            body["title"] = title
        if tags:
            body["tags"] = tags
        if client_tools is not None:
            body["client_tools"] = client_tools
        if initial_message:
            body["initial_message"] = {
                "role": "user",
                "content": [{"type": "text", "text": initial_message}],
                "run": True,
            }
        status, payload = self._send(route, "POST", "/api/conversations", body)
        if status == 429:
            raise CloudUnavailable("sandbox conversation capacity full")
        if status == 422:
            raise CloudError(
                f"create on sandbox rejected: {str(payload)[:300]}"
            )
        if status not in (200, 201):
            raise CloudError(f"create on sandbox -> {status}")
        actual_id = str((payload or {}).get("id") or conversation_id)
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
        )

    def _conversation_id(self, initial_message: str, title: str | None) -> str:
        # Deterministic so a retried create on the same sandbox is idempotent
        # (the agent-server returns the existing conversation for the same id).
        seed = f"cloud-direct:{title or ''}\x00{initial_message or ''}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))

    #: The direct transport has no native repository-selection field on the
    #: agent-server create call. Callers (and the UI diagnostics) read this.
    supports_repository_selection = False

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
        if self.llm_model:
            llm = dict(settings.get("llm") or {})
            llm["model"] = self.llm_model
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
        for row in self.store.list_sandboxes():
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
        route = self._route_for(conversation_id)
        status, _ = self._send(route, "DELETE", f"/api/conversations/{conversation_id}")
        return status in (200, 204)

    def pause(self, conversation_id: str) -> None:
        route = self._route_for(conversation_id)
        status, _ = self._send(
            route, "POST", f"/api/conversations/{conversation_id}/pause"
        )
        if status not in (200, 201):
            raise CloudError(f"pause -> {status}")

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

    def get_settings(self) -> dict[str, Any]:
        return self.cloud.get_settings()

    def account_capabilities(self) -> dict[str, Any]:
        return self.cloud.account_capabilities()

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
    **kwargs: Any,
) -> CloudDirectClient:
    """Construct a :class:`CloudDirectClient` from the environment.

    ``llm_model`` (or ``OPENHANDS_DEFAULT_MODEL``) is applied to every sandbox
    conversation so a sandbox whose default model has no key still runs.
    """
    import os

    return CloudDirectClient(
        cloud or CloudClient(),
        store,
        reuse_sandboxes=reuse_sandboxes,
        llm_model=llm_model or os.environ.get("OPENHANDS_DEFAULT_MODEL"),
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
        return CloudClient()
    if choice not in ("direct", "sandbox", "sandboxes"):
        raise ValueError(
            f"unknown OPENHANDS_TRANSPORT {choice!r} (expected 'direct' or 'app')"
        )
    return build_direct_client(store, **kwargs)
