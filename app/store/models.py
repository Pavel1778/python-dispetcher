"""Domain records shared by all state-store backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Task:
    id: str
    workflow_id: str
    role: str  # SR | JR
    status: str
    idempotency_key: str
    title: str | None = None
    prompt: str | None = None
    parent_task_id: str | None = None
    start_task_id: str | None = None
    app_conversation_id: str | None = None
    sandbox_id: str | None = None
    selected_repository: str | None = None
    selected_branch: str | None = None
    attempts: int = 0
    max_attempts: int = 5
    last_error: str | None = None
    result_summary: str | None = None
    next_attempt_at: datetime | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    tags: dict[str, Any] = field(default_factory=dict)


@dataclass
class Workflow:
    id: str
    name: str
    status: str
    idempotency_key: str
    sr_task_id: str | None = None
    plan_summary: str | None = None
    result_summary: str | None = None
    expected_jr_count: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    tags: dict[str, Any] = field(default_factory=dict)


@dataclass
class EventRecord:
    id: int | None
    workflow_id: str | None
    task_id: str | None
    event_type: str
    message: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass
class Notification:
    id: int | None
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    workflow_id: str | None = None
    task_id: str | None = None
    status: str = "PENDING"
    attempts: int = 0
    created_at: datetime | None = None
    sent_at: datetime | None = None
