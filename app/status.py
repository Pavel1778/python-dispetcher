"""Canonical status vocabularies.

Values mirror the OpenHands Cloud V1 API enums verified against the live
OpenAPI spec. Do not invent extra states here; extend only from the spec.
"""

from __future__ import annotations

from enum import Enum


class WorkflowStatus(str, Enum):
    """Lifecycle of a Sr workflow (a planning/coordination task)."""

    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class TaskStatus(str, Enum):
    """Lifecycle of an individual dispatcher task (Sr or Jr)."""

    QUEUED = "QUEUED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class TaskRole(str, Enum):
    SR = "SR"
    JR = "JR"


# Terminal statuses never transition further without an explicit reset.
TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED.value,
        TaskStatus.FAILED.value,
        TaskStatus.BLOCKED.value,
        TaskStatus.CANCELLED.value,
    }
)

TERMINAL_WORKFLOW_STATUSES = frozenset(
    {
        WorkflowStatus.COMPLETED.value,
        WorkflowStatus.FAILED.value,
    }
)

TASK_STATUSES_NON_TERMINAL = tuple(
    s.value for s in TaskStatus if s.value not in TERMINAL_TASK_STATUSES
)

# --- OpenHands Cloud V1 enums (verbatim from the OpenAPI spec) ---

# AppConversationStartTaskStatus
START_TASK_STATUSES = frozenset(
    {
        "WORKING",
        "WAITING_FOR_SANDBOX",
        "PREPARING_REPOSITORY",
        "RUNNING_SETUP_SCRIPT",
        "SETTING_UP_GIT_HOOKS",
        "SETTING_UP_SKILLS",
        "STARTING_CONVERSATION",
        "READY",
        "ERROR",
    }
)

START_TASK_READY = "READY"
START_TASK_ERROR = "ERROR"

# ConversationExecutionStatus
EXECUTION_STATUSES = frozenset(
    {
        "idle",
        "running",
        "paused",
        "waiting_for_confirmation",
        "finished",
        "error",
        "stuck",
        "deleting",
    }
)

EXECUTION_FINISHED = "finished"
EXECUTION_ERROR = "error"
EXECUTION_STUCK = "stuck"

# SandboxStatus
SANDBOX_STATUSES = frozenset({"STARTING", "RUNNING", "PAUSED", "ERROR", "MISSING"})
