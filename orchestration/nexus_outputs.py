"""Reading a run task's full terminal output.

The agent-server cuts a terminal ``ObservationEvent`` at 30 000 characters and
writes the whole output to a file beside the conversation; the event names only
a directory. ``truncated_outputs`` lists which events were cut and
``full_terminal_output`` reads the file whose preview one of them showed.

This module is the shared logic for the HTTP routes and the MCP tools: it
resolves the task, honours the project membership (a non-member and a foreign
task are both ``NotFound``, never ``Forbidden``), and reads through the client
of the account the task ran on. Nothing here writes to the database - a full
output can be megabytes and is not duplicated in PostgreSQL.
"""
from __future__ import annotations

from typing import Any, Callable

from .cloud_store import NotFound
from .openhands_tools import ArtifactsUnsupported

#: ``client_for(principal, account_id) -> a CloudClient-shaped client``. The
#: caller supplies it because the server and the MCP process build it
#: differently (the server has its own account service and process client).
ClientFor = Callable[[str, "str | None"], Any]


def run_task(store: Any, principal: str, project_id: str, run_id: str,
             task_id: str) -> dict[str, Any]:
    """A task of *run_id* in *project_id*, or ``NotFound``.

    Membership is checked through the run, so a non-member never learns whether
    the task exists.
    """
    from .nexus_lab_store import LabStore

    if not _is_uuid(task_id):
        raise NotFound("task id")
    lab = LabStore(store)
    lab.get_run(principal, project_id, run_id)  # membership + existence
    task = next((t for t in lab.list_tasks(principal, project_id, run_id)
                 if str(t["id"]) == task_id), None)
    if task is None:
        raise NotFound("task not found in this run")
    return task


def run_task_outputs(store: Any, principal: str, project_id: str, run_id: str,
                     task_id: str, client_for: ClientFor) -> dict[str, Any]:
    """Which terminal outputs of the task's conversation were cut off.

    An empty list (never an error) when the task has no conversation yet, when
    the transport cannot reach output files (``unsupported`` says so), or when
    nothing was cut.
    """
    task = run_task(store, principal, project_id, run_id, task_id)
    conversation_id = task.get("conversation_id")
    base = {"task_id": task_id, "conversation_id": conversation_id, "outputs": []}
    if not conversation_id:
        return base
    from .cloud_client import CloudNotFound

    client = client_for(principal, task.get("openhands_account_id"))
    try:
        outputs = client.truncated_outputs(str(conversation_id))
    except ArtifactsUnsupported as e:
        return {**base, "unsupported": True, "reason": str(e)}
    except CloudNotFound as e:
        raise NotFound(str(e) or "conversation not found") from None
    return {**base, "outputs": outputs}


def run_task_output(store: Any, principal: str, project_id: str, run_id: str,
                    task_id: str, event_id: str, client_for: ClientFor) -> dict[str, Any]:
    """The complete terminal output whose preview one event showed.

    The event is read first, so the file returned is the one its preview named -
    not a file guessed by name. ``NotFound`` when the event is unknown or the
    full output is gone; ``ValueError`` when the transport cannot read it.
    """
    from .cloud_client import CloudNotFound

    task = run_task(store, principal, project_id, run_id, task_id)
    conversation_id = task.get("conversation_id")
    if not conversation_id:
        raise NotFound("the task has no conversation yet")
    client = client_for(principal, task.get("openhands_account_id"))
    prefix = None
    for event in client.list_events(str(conversation_id)):
        if event.get("id") != event_id:
            continue
        obs = event.get("observation") or {}
        prefix = "".join(
            c.get("text", "")
            for c in (obs.get("content") or [])
            if isinstance(c, dict)
        )
        break
    if prefix is None:
        raise NotFound("no such event in the task's conversation")
    try:
        full = client.full_terminal_output(str(conversation_id), prefix)
    except ArtifactsUnsupported as e:
        raise ValueError(str(e)) from None
    except CloudNotFound as e:
        raise NotFound(str(e) or "the full output is not available") from None
    return {"task_id": task_id, "conversation_id": conversation_id,
            "event_id": event_id, "shown_chars": len(prefix), **full}


def _is_uuid(value: str) -> bool:
    import re

    return bool(re.fullmatch(r"[0-9a-fA-F-]{36}", value or ""))
