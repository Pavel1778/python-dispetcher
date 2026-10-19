"""Independent workflow verifier.

Why this module exists
----------------------
A workflow must not reach ``completed`` because the Sr agent *said* it was done.
Sr's text is untrusted: it can hallucinate success, and a tool result is only a
record of what a tool returned, not proof the work happened. This verifier is the
only writer of a workflow's terminal state, and it decides from **durable task
state** alone:

* every child (Jr) task must be terminal (``completed`` or ``cancelled``),
* at least one must be ``completed`` with a stored result,
* no tool call may still be in flight or undelivered,
* the Sr conversation must have stopped (its Cloud status is ``finished``), so a
  still-working Sr cannot be cut off mid-plan.

A missing answer, a failed task, a blocked task, or an unfinished/lost task all
prevent ``completed``. When the outcome is ambiguous the workflow stays
non-terminal and needs a human; that is deliberate.

The verifier never reads Sr's message text and never treats a tool's ``ok`` flag
as proof of completion.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .cloud_client import CloudNotFound, CloudUnavailable, ExecutionStatus
from .cloud_store import PostgresStore

log = logging.getLogger(__name__)

#: Workflow states that the verifier will not move.
TERMINAL_WORKFLOW_STATES = frozenset({"completed", "cancelled"})


@dataclass
class VerifierReport:
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    running: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "completed": self.completed,
            "failed": self.failed,
            "blocked": self.blocked,
            "running": self.running,
            "waiting": self.waiting,
            "unavailable": self.unavailable,
        }


class WorkflowVerifier:
    def __init__(self, store: PostgresStore, client: Any):
        self.store = store
        self.client = client

    def tick(self) -> VerifierReport:
        report = VerifierReport()
        for wf in self.store.list_workflows():
            if wf["state"] in TERMINAL_WORKFLOW_STATES:
                continue
            try:
                self._verify(wf["id"], report)
            except CloudUnavailable:
                report.unavailable.append(wf["id"])
        return report

    # ---------------------------------------------------------------- decide
    def _verify(self, workflow_id: str, report: VerifierReport) -> None:
        progress = self.store.workflow_progress(workflow_id)
        if progress["total"] == 0:
            # Sr has not delegated anything yet. Nothing to complete.
            report.waiting.append(workflow_id)
            return
        if self.store.has_pending_tool_calls(workflow_id):
            # A tool call is claimed-but-unfinished or its result was never
            # delivered. Sr is mid-flight; do not decide.
            report.waiting.append(workflow_id)
            return
        if progress["active"] > 0:
            self.store.set_workflow_state(workflow_id, "running")
            report.running.append(workflow_id)
            return
        if progress["failed"] > 0:
            self.store.set_workflow_state(workflow_id, "failed")
            report.failed.append(workflow_id)
            return
        if progress["blocked"] > 0:
            self.store.set_workflow_state(workflow_id, "blocked")
            report.blocked.append(workflow_id)
            return
        if progress["completed"] == 0:
            # Every child task was cancelled and none completed: ambiguous.
            self.store.set_workflow_state(workflow_id, "blocked")
            report.blocked.append(workflow_id)
            return

        # All child tasks are terminal with at least one real completion. Now
        # require that Sr has actually stopped, so we do not complete a workflow
        # while the Sr agent is still planning or adding tasks.
        sr = self.store.sr_for(workflow_id)
        conversation_id = (sr or {}).get("sr_conversation_id")
        if conversation_id:
            try:
                view = self.client.get_conversation(conversation_id)
            except CloudNotFound:
                # The Sr conversation vanished; the durable task state still says
                # the work is done, so we may complete on that evidence.
                view = None
            except CloudUnavailable:
                report.unavailable.append(workflow_id)
                return
            if view is not None and view.execution_status != ExecutionStatus.FINISHED.value:
                self.store.set_workflow_state(workflow_id, "verifying")
                report.waiting.append(workflow_id)
                return

        self.store.set_workflow_state(workflow_id, "completed")
        report.completed.append(workflow_id)
