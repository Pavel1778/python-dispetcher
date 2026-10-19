"""Abstract state-store interface.

The dispatcher only ever talks to this interface, so it can run against
PostgreSQL (production) or the in-memory backend (tests) unchanged.

Concurrency model: tasks are claimed with a time-bounded *lease*. A claim is
atomic and only succeeds if the row is unleased/expired, which is what makes
repeated runs idempotent and safe across process restarts.
"""

from __future__ import annotations

import abc
from datetime import datetime
from typing import Any, Sequence

from .models import EventRecord, Notification, Task, Workflow


class StoreUnavailableError(RuntimeError):
    """Raised when the backing database cannot be reached."""


class StateStore(abc.ABC):
    """Explicit interface for durable dispatcher state."""

    # --- lifecycle ---
    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...

    @abc.abstractmethod
    async def healthcheck(self) -> bool:
        """Return True if the store is reachable. Must not raise."""

    # --- workflows ---
    @abc.abstractmethod
    async def create_workflow(
        self, *, name: str, idempotency_key: str, tags: dict[str, Any] | None = None
    ) -> Workflow: ...

    @abc.abstractmethod
    async def get_workflow(self, workflow_id: str) -> Workflow | None: ...

    @abc.abstractmethod
    async def get_workflow_by_idempotency_key(self, key: str) -> Workflow | None: ...

    @abc.abstractmethod
    async def update_workflow_status(
        self,
        workflow_id: str,
        status: str,
        *,
        sr_task_id: str | None = None,
        plan_summary: str | None = None,
        result_summary: str | None = None,
        expected_jr_count: int | None = None,
    ) -> None: ...

    @abc.abstractmethod
    async def list_workflows(self, *, statuses: Sequence[str] | None = None) -> list[Workflow]: ...

    # --- tasks ---
    @abc.abstractmethod
    async def create_task(
        self,
        *,
        workflow_id: str,
        role: str,
        title: str | None,
        prompt: str | None,
        idempotency_key: str,
        parent_task_id: str | None = None,
        selected_repository: str | None = None,
        selected_branch: str | None = None,
        max_attempts: int = 5,
        tags: dict[str, Any] | None = None,
    ) -> tuple[Task, bool]:
        """Create a task idempotently.

        Returns ``(task, created)`` where ``created`` is False when a task with
        the same idempotency key already existed.
        """

    @abc.abstractmethod
    async def get_task(self, task_id: str) -> Task | None: ...

    @abc.abstractmethod
    async def get_task_by_idempotency_key(self, key: str) -> Task | None: ...

    @abc.abstractmethod
    async def list_tasks_for_workflow(self, workflow_id: str) -> list[Task]: ...

    @abc.abstractmethod
    async def claim_due_tasks(
        self, *, worker_id: str, lease_seconds: int, limit: int
    ) -> list[Task]:
        """Atomically lease due, non-terminal tasks. Safe under concurrency."""

    @abc.abstractmethod
    async def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        start_task_id: str | None = None,
        app_conversation_id: str | None = None,
        sandbox_id: str | None = None,
        last_error: str | None = None,
        result_summary: str | None = None,
        next_attempt_at: datetime | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        release_lease: bool = False,
    ) -> None: ...

    @abc.abstractmethod
    async def schedule_retry(
        self,
        task_id: str,
        *,
        error: str,
        next_attempt_at: datetime,
        release_lease: bool = True,
    ) -> None:
        """Increment attempts, record the error, and set the next attempt time."""

    @abc.abstractmethod
    async def list_tasks_by_status(self, statuses: Sequence[str]) -> list[Task]: ...

    # --- events ---
    @abc.abstractmethod
    async def record_event(
        self,
        *,
        event_type: str,
        workflow_id: str | None = None,
        task_id: str | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int: ...

    @abc.abstractmethod
    async def list_events(
        self, *, workflow_id: str | None = None, task_id: str | None = None, limit: int = 100
    ) -> list[EventRecord]: ...

    # --- notifications ---
    @abc.abstractmethod
    async def enqueue_notification(
        self,
        *,
        kind: str,
        payload: dict[str, Any] | None = None,
        workflow_id: str | None = None,
        task_id: str | None = None,
    ) -> int: ...

    @abc.abstractmethod
    async def list_pending_notifications(self, limit: int = 50) -> list[Notification]: ...

    @abc.abstractmethod
    async def mark_notification_sent(self, notification_id: int) -> None: ...
