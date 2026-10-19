"""Sr runtime: the real tool bridge that lets Sr create and manage Jr.

Why this module exists
----------------------
OpenHands Cloud creates a conversation and returns its status; it does **not**
make an agent able to call back into our system. A text plan written by Sr is
not orchestration. To let Sr act, the conversation must be started with
``client_tools`` specs (the documented ``ClientToolSpec`` field). When the agent
calls one, the server emits an ``ActionEvent`` in the conversation's event
stream and gives the agent only an acknowledgment observation.

This module closes the loop:

1. **Start Sr** with ``client_tools=SrTools.tool_schemas()`` and a system
   instruction to delegate and then stop.
2. **Poll** the Sr conversation's event stream for ``ActionEvent`` s whose
   ``tool_name`` is one of the Sr tools.
3. **Claim** each event id in ``sr_tool_calls`` so a call is executed exactly
   once, even across polls, restarts and concurrent workers.
4. **Execute** the real operation through :class:`~orchestration.sr_tools.SrTools`
   (which writes durable state).
5. **Deliver** the result back into the Sr conversation with a message, so Sr
   can verify and continue.

Verified against the live API: a conversation started with ``client_tools``
really does emit ``ActionEvent`` with ``action.kind == "ClientAction_<name>"``
and an ``ObservationEvent`` acknowledgment. See ``CLOUD-INTEGRATION-MAP.md``.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .cloud_client import CloudClient, CloudError, CloudNotFound, CloudUnavailable
from .cloud_store import NotFound, PostgresStore
from .sr_tools import SrTools, ToolResult

log = logging.getLogger(__name__)

#: The instruction that turns a plain Sr conversation into a delegating Sr.
#: Without it the agent may just answer in prose instead of calling the tools.
SR_INSTRUCTION = (
    "You are the senior (Sr) agent for this workflow. Delegate every unit of "
    "work to junior (Jr) tasks using the provided tools; do not attempt the work "
    "yourself. Call sr_create_task for each unit, then call sr_get_task_status or "
    "sr_get_task_report to check results, sr_request_task_revision when a result "
    "is insufficient, and sr_accept_task_result only after the acceptance criteria "
    "are met. After a tool call you may stop; the result is delivered back to you "
    "as a message.\n\n"
    "Only these repositories are allowed; never pass any other value:\n"
    "{repositories}\n\nWorkflow goal:\n{goal}"
)


def _repositories_clause(repos: list[dict[str, Any]]) -> str:
    if not repos:
        return "  (none - create tasks without a repository)"
    lines = []
    for r in repos:
        branches = r.get("allowed_branches") or []
        where = ", ".join(branches) if branches else f"default only ({r['default_branch']})"
        lines.append(f"  - {r['full_name']} (branches: {where})")
    return "\n".join(lines)


@dataclass
class SrTickReport:
    handled: list[dict] = field(default_factory=list)
    started: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "handled": self.handled,
            "started": self.started,
            "errors": self.errors,
            "unavailable": self.unavailable,
        }


class SrRuntime:
    """Drives the Sr conversation and executes the tools it calls."""

    def __init__(
        self,
        store: PostgresStore,
        client: CloudClient,
        tools: SrTools,
        *,
        worker_id: str = "sr-runtime",
        start_poll_attempts: int = 30,
        start_poll_interval: float = 2.0,
    ):
        self.store = store
        self.client = client
        self.tools = tools
        self.worker_id = worker_id
        self.start_poll_attempts = start_poll_attempts
        self.start_poll_interval = start_poll_interval
        # Map the prefixed client-tool names back to the real Sr operations.
        self._ops = {
            "sr_create_task": self.tools.create_task,
            "sr_list_child_tasks": self.tools.list_child_tasks,
            "sr_get_task_status": self.tools.get_task_status,
            "sr_get_task_report": self.tools.get_task_report,
            "sr_request_task_revision": self.tools.request_task_revision,
            "sr_accept_task_result": self.tools.accept_task_result,
            "sr_cancel_task": self.tools.cancel_task,
        }
        self._tool_names = set(self._ops)

    # ------------------------------------------------------------------ start
    def ensure_sr(self, workflow_id: str, *, repository: str | None = None) -> str:
        """Start the Sr conversation for a workflow, or return the existing one.

        Idempotent: ``workflows.sr_conversation_id`` is checked first, so a
        restart or a second call never creates a second Sr.
        """
        existing = self.store.sr_for(workflow_id)
        if existing and existing.get("sr_conversation_id"):
            return existing["sr_conversation_id"]
        # A crash after start_conversation but before bind_sr leaves a real Sr
        # conversation with no local record. Find it by its tag instead of
        # starting a second Sr for the same workflow.
        orphan = self._find_orphan_sr(workflow_id)
        if orphan is not None:
            self.store.bind_sr(workflow_id, conversation_id=orphan, status="running")
            return orphan
        goal = (existing or {}).get("goal") or "workflow"
        repos = self.store.list_repositories()
        start = self.client.start_conversation(
            initial_message=SR_INSTRUCTION.format(
                goal=goal, repositories=_repositories_clause(repos)
            ),
            title=f"Sr.{goal[:40]}",
            tags={"orchestratorsr": workflow_id.replace("-", "")},
            client_tools=SrTools.tool_schemas(),
        )
        # Record the start task before polling so a crash here is recoverable.
        self.store.bind_sr(workflow_id, start_task_id=start.id, status="starting")
        conversation_id = self._await_start(start)
        self.store.bind_sr(
            workflow_id, conversation_id=conversation_id, status="running"
        )
        return conversation_id

    def _find_orphan_sr(self, workflow_id: str) -> str | None:
        """A previously started Sr conversation tagged for this workflow."""
        value = workflow_id.replace("-", "")
        try:
            items, _ = self.client.search_conversations(
                limit=50, tag=("orchestratorsr", value)
            )
        except CloudError:
            return None
        for conv in items:
            if conv.tags.get("orchestratorsr") == value:
                return conv.id
        return None

    def _await_start(self, start) -> str:
        current = start
        for _ in range(self.start_poll_attempts):
            if current.app_conversation_id:
                return current.app_conversation_id
            if current.failed:
                raise CloudError(f"Sr start failed: {current.detail or current.status}")
            time.sleep(self.start_poll_interval)
            nxt = self.client.get_start_task(current.id)
            if nxt is None:
                raise CloudUnavailable("Sr start task disappeared")
            current = nxt
        if current.app_conversation_id:
            return current.app_conversation_id
        raise CloudUnavailable("Sr start task not READY")

    # ------------------------------------------------------------------- tick
    def tick(self) -> SrTickReport:
        """Handle every unclaimed Sr tool call for every workflow with an Sr."""
        report = SrTickReport()
        self._redeliver(report)
        for workflow in self.store.list_workflows():
            workflow_id = workflow["id"]
            sr = self.store.sr_for(workflow_id)
            if not sr or not sr.get("sr_conversation_id"):
                continue
            if workflow.get("state") in ("completed", "cancelled", "failed"):
                continue
            try:
                events = self.client.list_events(sr["sr_conversation_id"])
            except CloudNotFound:
                report.unavailable.append(workflow_id)
                continue
            except CloudUnavailable:
                report.unavailable.append(workflow_id)
                continue
            for event in events:
                self._maybe_handle(workflow_id, sr["sr_conversation_id"], event, report)
        return report

    def _redeliver(self, report: SrTickReport) -> None:
        """Re-send results whose delivery was lost to a crash.

        A tool call can be executed and recorded but never delivered (the process
        died between ``finish_tool_call`` and ``send_message``). Without this, Sr
        waits forever. Delivery is therefore at-least-once: a rare duplicate
        message is preferable to a stalled workflow.
        """
        for row in self.store.pending_deliveries():
            text = (
                f"[{row['name']} result]\n"
                f"{json.dumps(row['result'], default=str)}"
            )
            try:
                self.client.send_message(row["sr_conversation_id"], text)
            except CloudError as e:
                report.errors.append({"name": row["name"], "error": str(e)})
                continue
            self.store.mark_delivered(row["event_id"])

    def _maybe_handle(
        self,
        workflow_id: str,
        conversation_id: str,
        event: dict[str, Any],
        report: SrTickReport,
    ) -> None:
        if event.get("kind") != "ActionEvent":
            return
        name = event.get("tool_name")
        if name not in self._tool_names:
            return
        event_id = event.get("id")
        if not event_id:
            return
        args = _action_args(event.get("action"))
        if not self.store.claim_tool_call(
            event_id, workflow_id=workflow_id, name=name, args=args
        ):
            return  # already executed by an earlier poll
        result = self._execute(name, args, workflow_id, event_id)
        report.handled.append({"event_id": event_id, "name": name, "ok": result.ok})
        self.store.finish_tool_call(
            event_id,
            result=result.as_dict(),
            is_error=not result.ok,
            task_id=result.data.get("task_id"),
        )
        self._deliver(event_id, conversation_id, name, result, report)

    def _execute(
        self, name: str, args: dict[str, Any], workflow_id: str, event_id: str
    ) -> ToolResult:
        """Dispatch to the real Sr operation, forcing the workflow from context.

        Only the seven known tools are reachable, and only with their declared
        arguments, so a malformed call cannot reach arbitrary code. The model
        cannot be trusted to know the workflow id (observed live: it invented
        one), so ``workflow_id`` is always replaced with the real value.

        ``create_task`` receives an idempotency key derived from the Cloud event
        id. If the process crashed after the claim and this call is re-executed,
        the store returns the task it already created instead of a second one.
        """
        args = dict(args)
        args.pop("kind", None)
        args.pop("workflow_id", None)  # never trust the model's workflow id
        op = self._ops.get(name)
        if op is None:
            return ToolResult(False, {}, f"unknown tool: {name}")
        try:
            if name == "sr_create_task":
                args.setdefault("idempotency_key", f"sr:{event_id}")
            # ``workflow_id`` always comes from trusted context, never the model.
            return op(workflow_id=workflow_id, **args)
        except TypeError as e:  # bad arguments from the model
            return ToolResult(False, {}, f"invalid arguments for {name}: {e}")
        except NotFound as e:
            return ToolResult(False, {}, str(e))
        except Exception as e:  # noqa: BLE001 - tool boundary: never crash the loop
            log.warning("tool %s failed: %s: %s", name, type(e).__name__, e)
            return ToolResult(False, {}, f"{type(e).__name__}: {e}")

    def _deliver(
        self,
        event_id: str,
        conversation_id: str,
        name: str,
        result: ToolResult,
        report: SrTickReport,
    ) -> None:
        text = f"[{name} result]\n{json.dumps(result.as_dict(), default=str)}"
        try:
            self.client.send_message(conversation_id, text)
        except CloudError as e:
            # Execution is recorded; delivery is retried by ``_redeliver`` on the
            # next tick, so a transient failure does not stall Sr.
            report.errors.append({"name": name, "error": str(e)})
            return
        self.store.mark_delivered(event_id)


def _action_args(action: Any) -> dict[str, Any]:
    """Extract tool arguments from an ``ActionEvent.action`` payload.

    The payload carries the arguments plus a ``kind`` discriminator
    (``ClientAction_<name>``), which is not a tool argument.
    """
    if not isinstance(action, dict):
        return {}
    return {k: v for k, v in action.items() if k != "kind"}
