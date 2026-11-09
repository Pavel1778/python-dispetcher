"""Sr tools: the operations a senior agent uses to run child (Jr) tasks.

Each method performs a real action through the store and dispatcher and
persists the outcome. None of them is a stub: ``create_task`` writes a row,
``cancel_task`` moves state, ``accept_task_result`` records acceptance, and so
on.

The tools are deliberately narrow. Sr can create, inspect, revise, accept and
cancel Jr work; it cannot bypass the state machine, run outside the repository
allowlist, or read secrets. Repository and branch are validated against the
``repositories`` allowlist before a task is created.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .cloud_dispatcher import CloudDispatcher
from .cloud_store import (
    InvalidTransition,
    NotFound,
    PostgresStore,
    TaskState,
)


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any]
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out = {"ok": self.ok, **self.data}
        if self.error:
            out["error"] = self.error
        return out


class SrTools:
    """Real Sr operations. All state changes are durable in the store."""

    #: Upper bound on child tasks per workflow. A model that loops on
    #: ``sr_create_task`` cannot exhaust the account's conversation quota.
    MAX_CHILD_TASKS = 25

    def __init__(self, store: PostgresStore, dispatcher: CloudDispatcher):
        self.store = store
        self.dispatcher = dispatcher

    # ------------------------------------------------------------ helpers
    def _in_workflow(self, task_id: str, workflow_id: str | None) -> bool:
        """True when *task_id* belongs to *workflow_id*.

        The model supplies ``task_id`` as free text. Without this check a Sr
        conversation could read, revise, cancel or accept a task from another
        workflow, because the store methods are keyed by task id alone.
        """
        if workflow_id is None:
            return False
        task = self.store.try_get_task(task_id)
        return task is not None and task.workflow_id == workflow_id

    # ------------------------------------------------------------ create_task
    def create_task(
        self,
        goal: str,
        *,
        workflow_id: str,
        request: str = "",
        acceptance: str = "",
        repository: str | None = None,
        branch: str | None = None,
        dependencies: list[str] | None = None,
        priority: int = 0,
        max_attempts: int = 3,
        idempotency_key: str | None = None,
        parent_task_id: str | None = None,
    ) -> ToolResult:
        """Create a child task and enqueue it for a Jr conversation.

        The repository (when given) must be in the allowlist; otherwise the
        task is rejected here, before any conversation is created.
        """
        if not goal or not goal.strip():
            return ToolResult(False, {}, "goal is required")
        if self.store.count_child_tasks(workflow_id) >= self.MAX_CHILD_TASKS:
            return ToolResult(
                False, {},
                f"workflow child-task limit reached ({self.MAX_CHILD_TASKS})",
            )
        # Inherit the workflow's project. A project-scoped workflow may only use
        # repositories linked to that project; a legacy workflow (no project)
        # keeps the old allowlist-only behaviour. This is the second half of the
        # dispatch authorization: even if the model names a repository that is in
        # the global allowlist, it is refused here unless the project owns it.
        project_id = self.store.workflow_project(workflow_id)
        if repository is not None:
            repo = self.store.allowed_repository(repository)
            if repo is None:
                return ToolResult(False, {}, f"repository not allowed: {repository}")
            allowed = repo.get("allowed_branches") or []
            if branch:
                if allowed and branch not in allowed:
                    return ToolResult(False, {}, f"branch not allowed: {branch}")
                if not allowed and branch != repo["default_branch"]:
                    return ToolResult(
                        False, {}, f"branch not allowed (default only): {branch}"
                    )
            if project_id is not None and not self.store.project_repository_linked(
                    project_id, repository):
                return ToolResult(
                    False, {},
                    f"repository not linked to this project: {repository}",
                )
        task = self.store.submit_task(
            goal,
            workflow_id=workflow_id,
            parent_task_id=parent_task_id,
            request=request,
            acceptance=acceptance,
            priority=priority,
            dependencies=dependencies,
            max_attempts=max_attempts,
            idempotency_key=idempotency_key,
            selected_repository=repository,
            selected_branch=branch,
            project_id=project_id,
        )
        return ToolResult(True, {"task_id": task.id, "state": task.state.value})

    # ------------------------------------------------------ list_child_tasks
    def list_child_tasks(self, *, workflow_id: str,
                         parent_task_id: str | None = None) -> ToolResult:
        tasks = self.store.list_tasks(
            workflow_id=workflow_id, parent_task_id=parent_task_id
        )
        return ToolResult(
            True,
            {
                "tasks": [
                    {
                        "id": t.id,
                        "goal": t.goal,
                        "state": t.state.value,
                        "attempt": t.attempt,
                        "repository": t.selected_repository,
                        "branch": t.selected_branch,
                    }
                    for t in tasks
                ]
            },
        )

    # -------------------------------------------------------- get_task_status
    def get_task_status(self, task_id: str, *, workflow_id: str | None = None) -> ToolResult:
        if not self._in_workflow(task_id, workflow_id):
            return ToolResult(False, {}, f"unknown task: {task_id}")
        try:
            task = self.store.get_task(task_id)
        except NotFound:
            return ToolResult(False, {}, f"unknown task: {task_id}")
        conv = self.store.conversation_for(task_id)
        return ToolResult(
            True,
            {
                "task_id": task.id,
                "state": task.state.value,
                "attempt": task.attempt,
                "max_attempts": task.max_attempts,
                "owner": task.owner,
                "conversation_id": (conv or {}).get("external_conversation_id"),
                "repository": task.selected_repository,
                "branch": task.selected_branch,
            },
        )

    # -------------------------------------------------------- get_task_report
    def get_task_report(self, task_id: str, *, workflow_id: str | None = None) -> ToolResult:
        if not self._in_workflow(task_id, workflow_id):
            return ToolResult(False, {}, f"unknown task: {task_id}")
        try:
            task = self.store.get_task(task_id)
        except NotFound:
            return ToolResult(False, {}, f"unknown task: {task_id}")
        return ToolResult(
            True,
            {
                "task_id": task.id,
                "state": task.state.value,
                "result": task.result,
                "evidence": task.evidence,
                "events": self.store.events(task_id=task_id, limit=50),
            },
        )

    # ----------------------------------------------------- request_task_revision
    def request_task_revision(
        self, task_id: str, feedback: str, *, workflow_id: str | None = None
    ) -> ToolResult:
        """Send revision feedback and re-queue the task for another attempt.

        The task is moved to QUEUED (which clears the lease) and the feedback is
        stored as evidence, so the next dispatch runs with it in context.
        """
        if not self._in_workflow(task_id, workflow_id):
            return ToolResult(False, {}, f"unknown task: {task_id}")
        try:
            task = self.store.get_task(task_id)
        except NotFound:
            return ToolResult(False, {}, f"unknown task: {task_id}")
        conv = self.store.conversation_for(task_id)
        if conv and conv.get("external_conversation_id"):
            try:
                self.dispatcher.client.send_message(
                    conv["external_conversation_id"], feedback
                )
            except Exception as e:  # transport errors must not lose the request
                return ToolResult(False, {}, f"could not deliver feedback: {e}")
        try:
            self.store.force_transition(
                task_id, TaskState.QUEUED,
                evidence=[{"reason": "revision requested", "feedback": feedback}],
            )
        except InvalidTransition as e:
            return ToolResult(False, {}, f"cannot revise from {task.state.value}: {e}")
        return ToolResult(True, {"task_id": task_id, "state": TaskState.QUEUED.value})

    # ----------------------------------------------------- accept_task_result
    def accept_task_result(self, task_id: str, *, accepted: bool,
                           note: str = "", workflow_id: str | None = None) -> ToolResult:
        """Record Sr's acceptance decision on a verified task."""
        if not self._in_workflow(task_id, workflow_id):
            return ToolResult(False, {}, f"unknown task: {task_id}")
        try:
            task = self.store.get_task(task_id)
        except NotFound:
            return ToolResult(False, {}, f"unknown task: {task_id}")
        if task.state is not TaskState.VERIFYING and task.state is not TaskState.COMPLETED:
            return ToolResult(
                False, {}, f"task not ready for acceptance (state={task.state.value})"
            )
        if accepted:
            if task.state is TaskState.VERIFYING:
                self.store.transition(
                    task_id, TaskState.COMPLETED, owner=self.dispatcher.worker_id,
                    evidence=[{"reason": "accepted", "note": note}],
                )
            return ToolResult(True, {"task_id": task_id, "state": TaskState.COMPLETED.value})
        # Rejection sends it back for another attempt.
        self.store.force_transition(
            task_id, TaskState.QUEUED,
            evidence=[{"reason": "rejected", "note": note}],
        )
        return ToolResult(True, {"task_id": task_id, "state": TaskState.QUEUED.value})

    # ------------------------------------------------------------ cancel_task
    def cancel_task(self, task_id: str, *, workflow_id: str | None = None) -> ToolResult:
        if not self._in_workflow(task_id, workflow_id):
            return ToolResult(False, {}, f"unknown task: {task_id}")
        try:
            task = self.store.get_task(task_id)
        except NotFound:
            return ToolResult(False, {}, f"unknown task: {task_id}")
        if task.state in (TaskState.COMPLETED, TaskState.CANCELLED):
            return ToolResult(False, {}, f"task already {task.state.value}")
        self.store.transition(task_id, TaskState.CANCELLED)
        return ToolResult(True, {"task_id": task_id, "state": TaskState.CANCELLED.value})

    # --------------------------------------------------------------- schemas
    @staticmethod
    def tool_schemas() -> list[dict[str, Any]]:
        """``ClientToolSpec``-shaped schemas for the Sr conversation.

        Names are prefixed ``sr_`` on purpose: a client tool whose name collides
        with a built-in tool is rejected by the agent server with a 422
        (``create_task`` and ``list_child_tasks`` both collide — confirmed live).
        The prefix keeps the names unique and the mapping is unwrapped in
        :mod:`orchestration.sr_runtime`.
        """
        return [
            {
                "name": "sr_create_task",
                "description": "Create a child (Jr) task in a repository and enqueue it.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string"},
                        "request": {"type": "string"},
                        "acceptance": {"type": "string"},
                        "repository": {"type": "string"},
                        "branch": {"type": "string"},
                        "dependencies": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["goal"],
                },
            },
            {
                "name": "sr_list_child_tasks",
                "description": "List tasks in a workflow, optionally under a parent.",
                "parameters": {
                    "type": "object",
                    "properties": {"parent_task_id": {"type": "string"}},
                },
            },
            {
                "name": "sr_get_task_status",
                "description": "Return the current state and conversation of a task.",
                "parameters": {
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"],
                },
            },
            {
                "name": "sr_get_task_report",
                "description": "Return a task's result, evidence and recent events.",
                "parameters": {
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"],
                },
            },
            {
                "name": "sr_request_task_revision",
                "description": "Send feedback and re-queue a task for another attempt.",
                "parameters": {
                    "type": "object",
                    "properties": {"task_id": {"type": "string"},
                                   "feedback": {"type": "string"}},
                    "required": ["task_id", "feedback"],
                },
            },
            {
                "name": "sr_accept_task_result",
                "description": "Accept or reject a verified task result.",
                "parameters": {
                    "type": "object",
                    "properties": {"task_id": {"type": "string"},
                                   "accepted": {"type": "boolean"},
                                   "note": {"type": "string"}},
                    "required": ["task_id", "accepted"],
                },
            },
            {
                "name": "sr_cancel_task",
                "description": "Cancel a task that is no longer needed.",
                "parameters": {
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"],
                },
            },
        ]
