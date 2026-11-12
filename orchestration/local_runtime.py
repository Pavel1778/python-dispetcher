"""Local agent-server transport: run the real Sr/Jr mechanism on one sandbox.

Why this module exists
----------------------
The Sr/Jr orchestration in :mod:`orchestration.sr_runtime`,
:mod:`orchestration.cloud_dispatcher` and :mod:`orchestration.sr_tools` is
written against the OpenHands **Cloud V1** surface
(:class:`~orchestration.cloud_client.CloudClient`). That is the production
target, but it cannot always be exercised end-to-end: creating a *running*
Cloud conversation is a multi-stage, account-side operation
(``SETTING_UP_SKILLS`` -> ``STARTING_CONVERSATION``) that can fail inside the
runtime for reasons outside this repository's control.

The **local agent-server** (v1.50.1, the same build the Cloud sandboxes run)
speaks the SDK-level surface and supports every primitive the mechanism needs,
including ``client_tools`` -- the field that makes a real Sr able to call back
into a real Jr. This module adapts that surface to the ``CloudClient``
interface so the *same* dispatcher and Sr bridge run unchanged, turning the
mechanism from "covered by tests against a fake" into "verified against a real
LLM agent".

It is a transport adapter, not a second implementation: the state machine,
idempotency, leases and tool semantics all live in the shared code.

Design notes
------------
* Cloud V1's two-phase creation (start task -> ``READY``) does not exist
  locally: ``POST /api/conversations`` answers ``201`` with the conversation
  immediately. :meth:`LocalCloudClient.start_conversation` therefore returns a
  :class:`~orchestration.cloud_client.StartTask` that is already ``READY``, so
  the shared dispatcher's ``_await_start`` returns at once.
* Cloud V1 has no ``GET .../agent_final_response``; the local server does, so
  the answer is read the same way the dispatcher expects.
* Cloud ``search`` filters by tag server-side; the local server does not, so the
  filter is applied client-side over the returned window.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from .cloud_client import (
    CloudConversation,
    CloudError,
    CloudNotFound,
    CloudUnavailable,
    StartStatus,
    StartTask,
)
from .cloud_dispatcher import CloudDispatcher
from .cloud_store import PostgresStore
from .openhands_client import ApiUnavailable, ConversationGone, OpenHandsClient
from .sr_runtime import SrRuntime
from .sr_tools import SrTools

log = logging.getLogger(__name__)

#: Root for per-conversation working directories. A separate subdirectory per
#: conversation stops two agents from sharing (and overwriting) one workspace.
DEFAULT_WORKSPACE_ROOT = "/tmp/ohp-workspaces"


def _conversation_from_local(payload: dict[str, Any]) -> CloudConversation:
    """Map a local agent-server conversation payload onto ``CloudConversation``.

    Only the fields the dispatcher and verifier read are mapped; ``raw`` keeps
    the whole payload for callers that need more (e.g. loaded skills).
    """
    data = payload if isinstance(payload, dict) else {}
    return CloudConversation(
        id=str(data.get("id", "")),
        title=data.get("title"),
        sandbox_id=str(data.get("id", "")),
        execution_status=data.get("execution_status"),
        sandbox_status="RUNNING",
        selected_repository=None,
        selected_branch=None,
        conversation_url=None,
        sub_conversation_ids=list(data.get("sub_conversation_ids") or []),
        tags=dict(data.get("tags") or {}),
        raw=data,
    )


class LocalCloudClient(OpenHandsClient):
    """``CloudClient``-shaped adapter over a local agent-server.

    Subclasses :class:`~orchestration.openhands_client.OpenHandsClient` for the
    HTTP plumbing and the endpoints the two surfaces share (settings, events,
    final response, delete). Only the request/response *shapes* differ, so only
    those are overridden.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:60000",
        *,
        api_key: str | None = None,
        timeout: float = 60.0,
        workspace_root: str = DEFAULT_WORKSPACE_ROOT,
        max_iterations: int = 50,
        llm_model: str | None = None,
    ):
        super().__init__(base_url, api_key=api_key, timeout=timeout)
        self.workspace_root = workspace_root
        self.max_iterations = max_iterations
        # When set, every conversation uses this model instead of the server's
        # default. The server's own default may point at a provider whose key is
        # not present in the sandbox; a real-LLM E2E needs a model it can reach.
        self.llm_model = llm_model or os.environ.get("OHP_LOCAL_LLM_MODEL")

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
        """Create a local conversation and return an already-``READY`` start task.

        ``selected_repository`` and the account-selection fields are accepted for
        interface parity and ignored: a local sandbox has no Cloud repository
        provider and inherits the server's own agent settings.
        """
        conversation_id = self._conversation_id(initial_message, title)
        working_dir = os.path.join(self.workspace_root, conversation_id)
        os.makedirs(working_dir, exist_ok=True)
        agent_settings = self.agent_settings()
        if self.llm_model:
            agent_settings = {**agent_settings, "llm": {**(agent_settings.get("llm") or {}), "model": self.llm_model}}
        _view, created = self.create_conversation(
            conversation_id=conversation_id,
            working_dir=working_dir,
            title=(title or initial_message[:60] or None),
            max_iterations=self.max_iterations,
            agent_settings=agent_settings,
            client_tools=client_tools,
        )
        if tags and created:
            self._patch_tags(conversation_id, tags)
        if initial_message:
            try:
                self.send_task(conversation_id, initial_message, run=True)
            except ApiUnavailable as e:
                raise CloudUnavailable(str(e)) from e
        return StartTask(
            id=conversation_id,
            status=StartStatus.READY.value,
            app_conversation_id=conversation_id,
            sandbox_id=conversation_id,
            agent_server_url=self.base_url,
        )

    def _conversation_id(self, initial_message: str, title: str | None) -> str:
        # A deterministic id keeps a retried start from creating a second sandbox.
        # Seeded on the full title and prompt (not a prefix) so two genuinely
        # different starts do not collide on a shared opening line.
        seed = f"local-ohp:{title or ''}\x00{initial_message or ''}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))

    def _patch_tags(self, conversation_id: str, tags: dict[str, str]) -> None:
        status, _ = self._request(
            "PATCH", f"/api/conversations/{conversation_id}", {"tags": tags}
        )
        if status not in (200, 201):
            log.warning("could not set tags on %s: %s", conversation_id, status)

    # --------------------------------------------------------------- reads
    def get_start_task(self, start_task_id: str) -> StartTask | None:
        """There are no local start tasks; ``None`` lets callers fall through."""
        return None

    def get_conversation(self, conversation_id: str) -> CloudConversation:
        status, payload = self._request("GET", f"/api/conversations/{conversation_id}")
        if status == 404:
            raise CloudNotFound(conversation_id)
        if status != 200:
            raise CloudError(f"get_conversation -> {status}")
        return _conversation_from_local(payload or {})

    def search_conversations(
        self, *, limit: int = 50, page_id: str | None = None,
        tag: tuple[str, str] | None = None,
    ) -> tuple[list[CloudConversation], str | None]:
        """Search local conversations, applying the Cloud tag filter client-side."""
        path = f"/api/conversations/search?limit={limit}"
        if page_id:
            path += f"&page_id={page_id}"
        status, payload = self._request("GET", path)
        if status != 200 or not isinstance(payload, dict):
            raise CloudError(f"search_conversations -> {status}")
        items = [_conversation_from_local(c) for c in (payload.get("items") or [])]
        if tag is not None:
            key, value = tag
            items = [c for c in items if c.tags.get(key) == value]
        return items, payload.get("next_page_id")

    def list_events(
        self, conversation_id: str, *, limit: int = 100, max_pages: int = 10
    ) -> list[dict[str, Any]]:
        """All events, walking ``next_page_id`` (the local page size is 100)."""
        collected: list[dict[str, Any]] = []
        page_id: str | None = None
        for _ in range(max_pages):
            path = f"/api/conversations/{conversation_id}/events/search?limit={limit}"
            if page_id:
                path += f"&page_id={page_id}"
            status, payload = self._request("GET", path)
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
        """Deliver a message (and optionally run) to a local conversation."""
        try:
            self.send_task(conversation_id, text, run=run)
        except ConversationGone:
            raise CloudNotFound(conversation_id) from None
        except ApiUnavailable as e:
            raise CloudUnavailable(str(e)) from e
        return True

    def update_conversation(self, conversation_id: str, *, title: str | None = None) -> bool:
        if title is None:
            return True
        status, _ = self._request(
            "PATCH", f"/api/conversations/{conversation_id}", {"title": title}
        )
        return status in (200, 201)

    def delete_conversation(self, conversation_id: str) -> bool:
        status, _ = self._request("DELETE", f"/api/conversations/{conversation_id}")
        return status in (200, 204)

    def stop_conversation(self, conversation_id: str) -> bool:
        """``POST /api/conversations/{id}/pause``: stop the loop, keep the
        conversation. The local agent-server exposes the same conversation-scoped
        pause as the direct transport."""
        status, _ = self._request(
            "POST", f"/api/conversations/{conversation_id}/pause", {}
        )
        return status in (200, 201, 204)

    def health(self) -> bool:
        return super().health()

    # ---------------------------------------------------- account metadata
    def get_settings(self) -> dict[str, Any]:
        return super().settings()

    def account_capabilities(self) -> dict[str, Any]:
        """Local capability snapshot, shaped like the Cloud one.

        The local server has no marketplaces or agent-profile store, so those
        blocks are empty; the LLM block is derived from ``agent_settings``.
        """
        agent_settings = (self.get_settings() or {}).get("agent_settings") or {}
        mcp = agent_settings.get("mcp_config") or {}
        return {
            "llm_api_key_set": bool(agent_settings.get("llm")),
            "active_agent_profile_id": None,
            "mcp_servers": sorted(mcp.keys()) if isinstance(mcp, dict) else [],
            "registered_marketplaces": 0,
            "inherited_marketplaces": 0,
            "disabled_skills": [],
            "provider_tokens_set": {},
            "agent_profiles": [],
        }

    def conversation_skills(self, conversation_id: str) -> list[str]:
        """Skill names recorded on a local conversation, if any."""
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


class LocalCloudDispatcher(CloudDispatcher):
    """``CloudDispatcher`` bound to a local agent-server.

    The only Cloud-specific behaviour is the asynchronous start task; the local
    adapter reports ``READY`` immediately, so the shared ``_await_start`` works
    unchanged. The subclass exists to name the transport explicitly.
    """

    def __init__(self, store: PostgresStore, client: LocalCloudClient, **kwargs: Any):
        super().__init__(store, client, **kwargs)


class LocalSrBridge(SrRuntime):
    """``SrRuntime`` bound to a local agent-server.

    ``SrRuntime`` starts Sr with ``client_tools`` and a ``sr_``-prefixed tool
    set; the local server supports both, so no behaviour is overridden. The
    subclass documents that the real delegation path -- Sr calls a client tool,
    the bridge executes it, a real Jr conversation is created and run -- is
    exercised by this transport and not only by the fake.
    """

    def __init__(self, store: PostgresStore, client: LocalCloudClient, **kwargs: Any):
        tools = kwargs.pop("tools", None)
        if tools is None:
            tools = SrTools(
                store,
                LocalCloudDispatcher(
                    store, client, worker_id=kwargs.get("worker_id", "sr-runtime")
                ),
            )
        super().__init__(store, client, tools, **kwargs)


def build_local_stack(
    store: PostgresStore,
    *,
    base_url: str | None = None,
    worker_id: str = "local",
    workspace_root: str = DEFAULT_WORKSPACE_ROOT,
) -> tuple[LocalCloudClient, LocalCloudDispatcher, LocalSrBridge]:
    """Construct the three collaborators for a local end-to-end run.

    Returns ``(client, dispatcher, sr_bridge)`` wired to *store*, mirroring how
    ``orchestration.web_server.App`` wires the Cloud stack.
    """
    client = LocalCloudClient(
        base_url or os.environ.get("LOCAL_AGENT_SERVER_URL", "http://127.0.0.1:60000"),
        workspace_root=workspace_root,
    )
    dispatcher = LocalCloudDispatcher(
        store, client, worker_id=worker_id, start_poll_interval=2.0,
        start_poll_attempts=5,
    )
    sr = LocalSrBridge(
        store, client, worker_id=worker_id, start_poll_interval=2.0,
        start_poll_attempts=5,
    )
    return client, dispatcher, sr
