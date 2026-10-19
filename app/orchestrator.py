"""Orchestration operations exposed to a Sr conversation through MCP.

This module is the *only* surface the bridge tools touch. It deliberately does
not expose generic SQL, generic HTTP, or arbitrary repository access. Every
operation is:

* scope-checked (the caller must hold a token for this workflow + Sr task),
* repository-allowlisted,
* bounded (child-task caps, length limits, idempotency keys),
* auditable (every mutation writes a ``task_events`` row).

It is transport-agnostic: the MCP server and the HTTP bridge are thin wrappers
over these methods, so the safety rules cannot be bypassed by choosing one
transport over the other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .logging_config import get_logger, safe_extra
from .status import TaskRole, TaskStatus, WorkflowStatus
from .store.base import StateStore
from .store.models import Task

logger = get_logger(__name__)

# Hard caps. These are safety limits, not tuning knobs: a runaway Sr must not be
# able to spawn unbounded child conversations.
MAX_TITLE_LENGTH = 200
MAX_PROMPT_LENGTH = 20_000
MAX_IDEMPOTENCY_KEY_LENGTH = 128

_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,%d}$" % MAX_IDEMPOTENCY_KEY_LENGTH)


class OrchestratorError(Exception):
    """A caller-visible orchestration failure (invalid input, cap, scope)."""


class ScopeViolation(OrchestratorError):
    """The caller tried to act outside its workflow/task scope."""


class CapExceeded(OrchestratorError):
    """A hard limit (child-task cap, retry cap) was reached."""


class RepositoryNotAllowed(OrchestratorError):
    """The requested repository is not on the allowlist."""


@dataclass(frozen=True)
class JrTaskView:
    task_id: str
    status: str
    title: str | None
    created: bool
    attempts: int
    result_summary: str | None
    last_error: str | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalise_repo(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().lower().removesuffix(".git")


class Orchestrator:
    """Validated orchestration operations, shared by all bridge transports."""

    def __init__(self, *, settings: Settings, store: StateStore) -> None:
        self._settings = settings
        self._store = store
        self._allowlist = {
            _normalise_repo(r) for r in settings.allowed_repositories if r
        }

    # ------------------------------------------------------------------
    # scope helpers
    # ------------------------------------------------------------------

    async def _load_scoped_sr_task(self, workflow_id: str, task_id: str) -> Task:
        """Load the Sr task and assert it belongs to the claimed workflow."""
        task = await self._store.get_task(task_id)
        if task is None:
            raise ScopeViolation("Sr task not found")
        if task.workflow_id != workflow_id:
            raise ScopeViolation("Sr task does not belong to the claimed workflow")
        if task.role != TaskRole.SR.value:
            raise ScopeViolation("only a Sr task may orchestrate Jr children")
        return task

    def _check_repository(self, repo: str | None) -> str | None:
        # Fail closed: an empty allowlist denies *all* children, not just ones
        # naming a repository. Otherwise a repo-less child could still run work
        # against whatever default the sandbox picks.
        if not self._allowlist:
            raise RepositoryNotAllowed(
                "no repositories are allowlisted (set JR_ALLOWED_REPOSITORIES)"
            )
        normalised = _normalise_repo(repo)
        if normalised is None:
            return None
        if normalised not in self._allowlist:
            raise RepositoryNotAllowed("repository is not on the allowlist")
        return repo

    # ------------------------------------------------------------------
    # create_jr_task
    # ------------------------------------------------------------------

    async def create_jr_task(
        self,
        *,
        workflow_id: str,
        task_id: str,
        title: str,
        prompt: str,
        idempotency_key: str,
        selected_repository: str | None = None,
        selected_branch: str | None = None,
    ) -> JrTaskView:
        parent = await self._load_scoped_sr_task(workflow_id, task_id)

        title = (title or "").strip()
        prompt = (prompt or "").strip()
        if not title:
            raise OrchestratorError("title is required")
        if len(title) > MAX_TITLE_LENGTH:
            raise OrchestratorError(f"title exceeds {MAX_TITLE_LENGTH} characters")
        if not prompt:
            raise OrchestratorError("prompt is required")
        if len(prompt) > MAX_PROMPT_LENGTH:
            raise OrchestratorError(f"prompt exceeds {MAX_PROMPT_LENGTH} characters")
        if not _IDEMPOTENCY_KEY_RE.match(idempotency_key or ""):
            raise OrchestratorError(
                "idempotency_key must be 1-128 chars of [A-Za-z0-9._:/-]"
            )

        repo = self._check_repository(selected_repository or parent.selected_repository)
        branch = selected_branch or parent.selected_branch

        # Idempotent create: a repeated tool call with the same key returns the
        # existing child instead of spawning a second conversation.
        existing = await self._store.get_task_by_idempotency_key(idempotency_key)
        if existing is not None:
            if existing.workflow_id != workflow_id or existing.parent_task_id != task_id:
                raise ScopeViolation(
                    "idempotency_key is already used by another task"
                )
            return self._view(existing, created=False)

        # Cap check must happen before creation. Counting includes terminal
        # children so that re-running a workflow cannot bypass the cap.
        children = await self._children_of(workflow_id, task_id)
        if len(children) >= self._settings.jr_max_children_per_workflow:
            raise CapExceeded(
                f"child-task cap reached ({self._settings.jr_max_children_per_workflow})"
            )

        task, created = await self._store.create_task(
            workflow_id=workflow_id,
            role=TaskRole.JR.value,
            title=title,
            prompt=prompt,
            idempotency_key=idempotency_key,
            parent_task_id=task_id,
            selected_repository=repo,
            selected_branch=branch,
            max_attempts=self._settings.dispatcher_max_attempts,
        )
        if created:
            await self._store.record_event(
                event_type="jr_task_created",
                workflow_id=workflow_id,
                task_id=task.id,
                message="Jr child task created via bridge",
                payload={"parent_task_id": task_id, "requested_by": "sr"},
            )
            # Workflow moves from PLANNING to RUNNING as soon as real work exists.
            workflow = await self._store.get_workflow(workflow_id)
            if workflow and workflow.status in (
                WorkflowStatus.QUEUED.value,
                WorkflowStatus.PLANNING.value,
            ):
                await self._store.update_workflow_status(
                    workflow_id, WorkflowStatus.RUNNING.value
                )
            logger.info(
                "jr task created via bridge",
                **safe_extra(workflow_id=workflow_id, task_id=task.id),
            )
        return self._view(task, created=created)

    # ------------------------------------------------------------------
    # read operations
    # ------------------------------------------------------------------

    async def get_jr_task_status(
        self, *, workflow_id: str, task_id: str, jr_task_id: str
    ) -> JrTaskView:
        await self._load_scoped_sr_task(workflow_id, task_id)
        child = await self._load_child(workflow_id, task_id, jr_task_id)
        return self._view(child, created=False)

    async def get_jr_task_result(
        self, *, workflow_id: str, task_id: str, jr_task_id: str
    ) -> JrTaskView:
        """Return a child's result.

        ``result_summary`` is populated by the dispatcher from the remote
        conversation, never from text a Sr conversation supplied.
        """
        await self._load_scoped_sr_task(workflow_id, task_id)
        child = await self._load_child(workflow_id, task_id, jr_task_id)
        return self._view(child, created=False)

    async def list_workflow_tasks(self, *, workflow_id: str, task_id: str) -> list[JrTaskView]:
        await self._load_scoped_sr_task(workflow_id, task_id)
        tasks = await self._store.list_tasks_for_workflow(workflow_id)
        return [self._view(t, created=False) for t in tasks]

    # ------------------------------------------------------------------
    # request_workflow_verification
    # ------------------------------------------------------------------

    async def request_workflow_verification(
        self, *, workflow_id: str, task_id: str, summary: str | None = None
    ) -> dict[str, Any]:
        """Record that the Sr believes its work is done.

        This is a *request*, not a completion. It only moves the Sr task to
        VERIFYING and marks the workflow VERIFYING. The dispatcher's own
        ``verify_workflows`` decides whether COMPLETED is legitimate, by
        checking that every child conversation actually finished. A Sr cannot
        mark a workflow COMPLETED by asserting success.
        """
        await self._load_scoped_sr_task(workflow_id, task_id)

        children = await self._children_of(workflow_id, task_id)
        unfinished = [
            c.id
            for c in children
            if c.status not in (TaskStatus.COMPLETED.value,)
        ]
        await self._store.record_event(
            event_type="verification_requested",
            workflow_id=workflow_id,
            task_id=task_id,
            message=(summary or "")[:MAX_PROMPT_LENGTH] or None,
            payload={"child_count": len(children), "unfinished_count": len(unfinished)},
        )
        await self._store.update_task(
            task_id,
            status=TaskStatus.VERIFYING.value,
            result_summary=(summary or "")[:MAX_PROMPT_LENGTH] or None,
            release_lease=True,
        )
        await self._store.update_workflow_status(
            workflow_id, WorkflowStatus.VERIFYING.value
        )
        logger.info(
            "workflow verification requested",
            **safe_extra(workflow_id=workflow_id, unfinished=len(unfinished)),
        )
        return {
            "accepted": True,
            "child_count": len(children),
            "unfinished_children": len(unfinished),
            "note": (
                "Verification requested. The dispatcher will mark the workflow "
                "COMPLETED only after every child conversation is confirmed "
                "finished."
            ),
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _children_of(self, workflow_id: str, parent_task_id: str) -> list[Task]:
        tasks = await self._store.list_tasks_for_workflow(workflow_id)
        return [
            t
            for t in tasks
            if t.role == TaskRole.JR.value and t.parent_task_id == parent_task_id
        ]

    async def _load_child(self, workflow_id: str, parent_task_id: str, jr_task_id: str) -> Task:
        child = await self._store.get_task(jr_task_id)
        if child is None:
            raise ScopeViolation("Jr task not found")
        if child.workflow_id != workflow_id or child.parent_task_id != parent_task_id:
            raise ScopeViolation("Jr task is outside this Sr task's scope")
        if child.role != TaskRole.JR.value:
            raise ScopeViolation("referenced task is not a Jr task")
        return child

    @staticmethod
    def _view(task: Task, *, created: bool) -> JrTaskView:
        return JrTaskView(
            task_id=task.id,
            status=task.status,
            title=task.title,
            created=created,
            attempts=task.attempts,
            result_summary=task.result_summary,
            last_error=task.last_error,
        )
