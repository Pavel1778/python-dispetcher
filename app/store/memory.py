"""In-memory state store.

Used for unit tests and for local runs without a database. It deliberately
mirrors the PostgreSQL semantics (idempotency keys, atomic lease claiming,
attempt counting) so the dispatcher behaves identically against both backends.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from .base import StateStore, StoreUnavailableError
from .models import EventRecord, Notification, Task, Workflow
from ..status import TERMINAL_TASK_STATUSES


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryStateStore(StateStore):
    def __init__(self, *, fail_healthcheck: bool = False) -> None:
        self._lock = asyncio.Lock()
        self._workflows: dict[str, Workflow] = {}
        self._workflow_keys: dict[str, str] = {}
        self._tasks: dict[str, Task] = {}
        self._task_keys: dict[str, str] = {}
        self._events: list[EventRecord] = []
        self._notifications: dict[int, Notification] = {}
        self._event_seq = 0
        self._notification_seq = 0
        self._fail_healthcheck = fail_healthcheck
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def healthcheck(self) -> bool:
        if self._fail_healthcheck:
            return False
        return self._connected

    # --- workflows ---

    async def create_workflow(
        self, *, name: str, idempotency_key: str, tags: dict[str, Any] | None = None
    ) -> Workflow:
        async with self._lock:
            existing_id = self._workflow_keys.get(idempotency_key)
            if existing_id is not None:
                return self._workflows[existing_id]
            wf = Workflow(
                id=str(uuid.uuid4()),
                name=name,
                status="QUEUED",
                idempotency_key=idempotency_key,
                created_at=_utcnow(),
                updated_at=_utcnow(),
                tags=dict(tags or {}),
            )
            self._workflows[wf.id] = wf
            self._workflow_keys[idempotency_key] = wf.id
            return wf

    async def get_workflow(self, workflow_id: str) -> Workflow | None:
        return self._workflows.get(workflow_id)

    async def get_workflow_by_idempotency_key(self, key: str) -> Workflow | None:
        wid = self._workflow_keys.get(key)
        return self._workflows.get(wid) if wid else None

    async def update_workflow_status(
        self,
        workflow_id: str,
        status: str,
        *,
        sr_task_id: str | None = None,
        plan_summary: str | None = None,
        result_summary: str | None = None,
        expected_jr_count: int | None = None,
    ) -> None:
        async with self._lock:
            wf = self._workflows.get(workflow_id)
            if wf is None:
                raise StoreUnavailableError(f"workflow {workflow_id} not found")
            wf.status = status
            if sr_task_id is not None:
                wf.sr_task_id = sr_task_id
            if plan_summary is not None:
                wf.plan_summary = plan_summary
            if result_summary is not None:
                wf.result_summary = result_summary
            if expected_jr_count is not None:
                wf.expected_jr_count = expected_jr_count
            wf.updated_at = _utcnow()

    async def list_workflows(self, *, statuses: Sequence[str] | None = None) -> list[Workflow]:
        items = list(self._workflows.values())
        if statuses:
            wanted = set(statuses)
            items = [w for w in items if w.status in wanted]
        return items

    # --- tasks ---

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
        async with self._lock:
            existing_id = self._task_keys.get(idempotency_key)
            if existing_id is not None:
                return self._tasks[existing_id], False
            now = _utcnow()
            task = Task(
                id=str(uuid.uuid4()),
                workflow_id=workflow_id,
                role=role,
                status="QUEUED",
                idempotency_key=idempotency_key,
                title=title,
                prompt=prompt,
                parent_task_id=parent_task_id,
                selected_repository=selected_repository,
                selected_branch=selected_branch,
                max_attempts=max_attempts,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
                tags=dict(tags or {}),
            )
            self._tasks[task.id] = task
            self._task_keys[idempotency_key] = task.id
            return task, True

    async def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    async def get_task_by_idempotency_key(self, key: str) -> Task | None:
        tid = self._task_keys.get(key)
        return self._tasks.get(tid) if tid else None

    async def list_tasks_for_workflow(self, workflow_id: str) -> list[Task]:
        return [t for t in self._tasks.values() if t.workflow_id == workflow_id]

    async def claim_due_tasks(
        self, *, worker_id: str, lease_seconds: int, limit: int
    ) -> list[Task]:
        now = _utcnow()
        claimed: list[Task] = []
        async with self._lock:
            for task in self._tasks.values():
                if len(claimed) >= limit:
                    break
                if task.status in TERMINAL_TASK_STATUSES:
                    continue
                if task.next_attempt_at and task.next_attempt_at > now:
                    continue
                if task.lease_expires_at and task.lease_expires_at > now:
                    continue
                if task.attempts >= task.max_attempts and task.status == "QUEUED":
                    continue
                task.lease_owner = worker_id
                task.lease_expires_at = now + timedelta(seconds=lease_seconds)
                task.updated_at = now
                claimed.append(task)
        return claimed

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
    ) -> None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise StoreUnavailableError(f"task {task_id} not found")
            if status is not None:
                task.status = status
            if start_task_id is not None:
                task.start_task_id = start_task_id
            if app_conversation_id is not None:
                task.app_conversation_id = app_conversation_id
            if sandbox_id is not None:
                task.sandbox_id = sandbox_id
            if last_error is not None:
                task.last_error = last_error
            if result_summary is not None:
                task.result_summary = result_summary
            if next_attempt_at is not None:
                task.next_attempt_at = next_attempt_at
            if started_at is not None:
                task.started_at = started_at
            if finished_at is not None:
                task.finished_at = finished_at
            if release_lease:
                task.lease_owner = None
                task.lease_expires_at = None
            task.updated_at = _utcnow()

    async def schedule_retry(
        self,
        task_id: str,
        *,
        error: str,
        next_attempt_at: datetime,
        release_lease: bool = True,
    ) -> None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise StoreUnavailableError(f"task {task_id} not found")
            task.attempts += 1
            task.last_error = error
            task.next_attempt_at = next_attempt_at
            if release_lease:
                task.lease_owner = None
                task.lease_expires_at = None
            task.updated_at = _utcnow()

    async def list_tasks_by_status(self, statuses: Sequence[str]) -> list[Task]:
        wanted = set(statuses)
        return [t for t in self._tasks.values() if t.status in wanted]

    # --- events ---

    async def record_event(
        self,
        *,
        event_type: str,
        workflow_id: str | None = None,
        task_id: str | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        async with self._lock:
            self._event_seq += 1
            self._events.append(
                EventRecord(
                    id=self._event_seq,
                    workflow_id=workflow_id,
                    task_id=task_id,
                    event_type=event_type,
                    message=message,
                    payload=dict(payload or {}),
                    created_at=_utcnow(),
                )
            )
            return self._event_seq

    async def list_events(
        self, *, workflow_id: str | None = None, task_id: str | None = None, limit: int = 100
    ) -> list[EventRecord]:
        items = self._events
        if workflow_id is not None:
            items = [e for e in items if e.workflow_id == workflow_id]
        if task_id is not None:
            items = [e for e in items if e.task_id == task_id]
        return items[-limit:]

    # --- notifications ---

    async def enqueue_notification(
        self,
        *,
        kind: str,
        payload: dict[str, Any] | None = None,
        workflow_id: str | None = None,
        task_id: str | None = None,
    ) -> int:
        async with self._lock:
            self._notification_seq += 1
            nid = self._notification_seq
            self._notifications[nid] = Notification(
                id=nid,
                kind=kind,
                payload=dict(payload or {}),
                workflow_id=workflow_id,
                task_id=task_id,
                status="PENDING",
                created_at=_utcnow(),
            )
            return nid

    async def list_pending_notifications(self, limit: int = 50) -> list[Notification]:
        items = [n for n in self._notifications.values() if n.status == "PENDING"]
        return items[:limit]

    async def mark_notification_sent(self, notification_id: int) -> None:
        async with self._lock:
            n = self._notifications.get(notification_id)
            if n is not None:
                n.status = "SENT"
                n.sent_at = _utcnow()
                n.attempts += 1
