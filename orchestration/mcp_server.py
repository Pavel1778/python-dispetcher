"""NEXUS MCP server: a small, authorized tool surface over stdio.

This speaks the Model Context Protocol (JSON-RPC 2.0, newline-delimited
messages on stdin/stdout) and exposes a **limited** set of useful tools. It is
deliberately not a general-purpose executor:

* No arbitrary shell, SQL, or ``eval`` tool exists.
* Every write tool re-checks project membership through the same service layer
  the HTTP API uses. A caller cannot reach a project it is not a member of, and
  cannot dispatch into a repository the project does not own.
* Reads are project-scoped; a cross-project id is reported as not found, not as
  a disclosure.
* Secrets are never returned: ``get_execution_status`` reports presence of
  configuration, never a key.

Transport: stdio (``python -m orchestration.mcp_server``) and, when mounted on
the web server, Streamable HTTP at ``POST /mcp`` (JSON-RPC 2.0 over HTTP). The
HTTP mount reuses the server's own session/bearer authentication and CSRF, so an
MCP caller has exactly the authority of an authenticated NEXUS user -- it cannot
reach a project the caller is not a member of.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Callable

from .cloud_store import NotFound, PostgresStore, StoreError
from .nexus_binding import AuthorizationError, authorize_dispatch
from .nexus_policy import (
    MCP_TOOL_SCOPES,
    ROLE_OWNER,
    PolicyViolation,
    install_log_redaction,
    require_tool_scope,
)
from .nexus_projects import ProjectService

log = logging.getLogger("mcp_server")

#: The MCP protocol revision this server implements.
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "nexus"
SERVER_VERSION = "2.0.0"


class McpError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class McpServer:
    """The tool surface. One instance per process; ``principal`` is the caller."""

    def __init__(self, store: PostgresStore, principal: str, *,
                 role: str = ROLE_OWNER):
        self.store = store
        self.principal = principal
        #: The caller's role, which decides which tool scopes it carries. The
        #: stdio/HTTP entry points pass the authenticated user's role; the
        #: default is the owner, matching the owner-level machine bearer.
        self.role = role
        self.projects = ProjectService(store)
        self.tools: dict[str, Callable[[dict], Any]] = {
            "list_projects": self._list_projects,
            "get_project": self._get_project,
            "list_repositories": self._list_repositories,
            "list_tasks": self._list_tasks,
            "get_task": self._get_task,
            "create_task": self._create_task,
            "dispatch_task": self._dispatch_task,
            "get_execution_status": self._get_execution_status,
            "list_evidence": self._list_evidence,
            "verify_evidence": self._verify_evidence,
            "list_runs": self._list_runs,
            "get_run": self._get_run,
            "cancel_run": self._cancel_run,
            "list_project_executors": self._list_project_executors,
            "assign_project_executor": self._assign_project_executor,
        }

    # -------------------------------------------------------------- schemas
    @staticmethod
    def tool_schemas() -> list[dict[str, Any]]:
        return [
            _tool("list_projects", "List projects the caller is a member of.", {}),
            _tool("get_project", "Get one project by id.", {"project_id": _s("string")},
                  ["project_id"]),
            _tool("list_repositories", "List repositories linked to a project.",
                  {"project_id": _s("string")}, ["project_id"]),
            _tool("list_tasks", "List tasks in a project.",
                  {"project_id": _s("string")}, ["project_id"]),
            _tool("get_task", "Get one task with its events.",
                  {"project_id": _s("string"), "task_id": _s("string")},
                  ["project_id", "task_id"]),
            _tool("create_task", "Create a task in a project and start its workflow.",
                  {"project_id": _s("string"), "goal": _s("string"),
                   "repository": _s("string"), "branch": _s("string")},
                  ["project_id", "goal"]),
            _tool("dispatch_task", "Requeue a task for another attempt.",
                  {"task_id": _s("string")}, ["task_id"]),
            _tool("get_execution_status", "Report execution/component status (no secrets).", {}),
            _tool("list_evidence", "List project evidence records.",
                  {"project_id": _s("string")}, ["project_id"]),
            _tool("verify_evidence", "Re-verify one evidence record.",
                  {"project_id": _s("string"), "evidence_id": _s("string"),
                   "expected_commit": _s("string")},
                  ["project_id", "evidence_id"]),
            _tool("list_runs", "List a project's autonomous runs (cancelled ones are hidden).",
                  {"project_id": _s("string")}, ["project_id"]),
            _tool("get_run", "Get one autonomous run with its tasks and its report.",
                  {"project_id": _s("string"), "run_id": _s("string")},
                  ["project_id", "run_id"]),
            _tool("cancel_run", "Stop an autonomous run for good (terminal, idempotent).",
                  {"project_id": _s("string"), "run_id": _s("string")},
                  ["project_id", "run_id"]),
            _tool("list_project_executors",
                  "List the OpenHands executors assigned to a project.",
                  {"project_id": _s("string")}, ["project_id"]),
            _tool("assign_project_executor",
                  "Assign an OpenHands account to a project (primary or fallback).",
                  {"project_id": _s("string"), "account_id": _s("string"),
                   "role": _s("string")}, ["project_id", "account_id"]),
        ]

    # ---------------------------------------------------------------- tools
    def _member(self, project_id: str) -> None:
        self.projects.get(self.principal, project_id)  # raises NotFound if not a member

    def _list_projects(self, args: dict) -> Any:
        return {"projects": self.projects.list(self.principal)}

    def _get_project(self, args: dict) -> Any:
        return {"project": self.projects.get(self.principal, args["project_id"])}

    def _list_repositories(self, args: dict) -> Any:
        pid = args["project_id"]
        self._member(pid)
        with self.store._tx() as cur:
            cur.execute(
                "SELECT r.full_name, r.provider, r.default_branch, r.allowed_branches, r.enabled "
                "FROM nexus_project_repositories pr JOIN repositories r ON r.id = pr.repository_id "
                "WHERE pr.project_id=%s ORDER BY r.full_name", (pid,))
            return {"repositories": [dict(r) for r in cur.fetchall()]}

    def _list_tasks(self, args: dict) -> Any:
        pid = args["project_id"]
        self._member(pid)
        tasks = self.store.list_tasks(project_id=pid)
        return {"tasks": [
            {"id": t.id, "goal": t.goal, "state": t.state.value,
             "repository": t.selected_repository, "branch": t.selected_branch}
            for t in tasks
        ]}

    def _get_task(self, args: dict) -> Any:
        pid = args["project_id"]
        self._member(pid)
        detail = self.store.task_detail(args["task_id"])
        if detail["project_id"] != pid:
            raise McpError(-32004, "not found")
        return {"task": detail}

    def _create_task(self, args: dict) -> Any:
        pid = args["project_id"]
        self._member(pid)
        repository = args.get("repository")
        branch = args.get("branch")
        if repository:
            if not self.store.project_repository_linked(pid, repository):
                raise McpError(-32005, "repository is not linked to this project")
            repo = self.store.allowed_repository(repository)
            if repo is None:
                raise McpError(-32005, "repository is not enabled")
            allowed = repo.get("allowed_branches") or []
            if branch and ((allowed and branch not in allowed)
                           or (not allowed and branch != repo["default_branch"])):
                raise McpError(-32005, "branch is not allowed")
        task = self.store.submit_task(
            args["goal"], project_id=pid, selected_repository=repository,
            selected_branch=branch)
        return {"task_id": task.id, "state": task.state.value,
                "note": "task queued; the dispatcher will start a conversation"}

    def _dispatch_task(self, args: dict) -> Any:
        task = self.store.try_get_task(args["task_id"])
        if task is None:
            raise McpError(-32004, "not found")
        if task.project_id:
            self._member(task.project_id)
            # Re-check the same authorization the dispatcher uses, so MCP cannot
            # requeue work the caller is not entitled to run.
            authorize_dispatch(self.store, task=task, principal=self.principal)
        moved = self.store.retry_task(task.id)
        return {"task_id": moved.id, "state": moved.state.value}

    def _get_execution_status(self, args: dict) -> Any:
        # Presence only. Never a key or a token.
        counts = self.store.dashboard_counts()
        return {
            "counts": counts,
            "openhands_configured": bool(os.environ.get("OPENHANDS_API_KEY")),
            "github_configured": bool(os.environ.get("GITHUB_TOKEN")),
        }

    def _list_runs(self, args: dict) -> Any:
        pid = args["project_id"]
        self._member(pid)
        from .nexus_lab_store import LabStore
        return {"runs": LabStore(self.store).list_runs(self.principal, pid)}

    def _get_run(self, args: dict) -> Any:
        pid = args["project_id"]
        self._member(pid)
        from .nexus_lab_store import LabStore
        lab = LabStore(self.store)
        run = lab.get_run(self.principal, pid, args["run_id"])
        return {"run": run, "tasks": lab.list_tasks(self.principal, pid, args["run_id"]),
                "report": lab.report_for_run(args["run_id"])}

    def _cancel_run(self, args: dict) -> Any:
        pid = args["project_id"]
        self._member(pid)
        from .nexus_lab_store import LabStore
        run = LabStore(self.store).cancel_run(self.principal, pid, args["run_id"])
        return {"run": run}

    def _list_project_executors(self, args: dict) -> Any:
        pid = args["project_id"]
        self._member(pid)
        from .nexus_project_executors import ProjectExecutorService
        return {"executors": ProjectExecutorService(self.store).list(self.principal, pid)}

    def _assign_project_executor(self, args: dict) -> Any:
        pid = args["project_id"]
        self._member(pid)  # membership; the service re-checks ownership
        from .nexus_project_executors import ProjectExecutorService
        role = args.get("role") or "fallback"
        if role not in ("primary", "fallback"):
            raise McpError(-32602, "role must be 'primary' or 'fallback'")
        executor = ProjectExecutorService(self.store).set_executor(
            self.principal, pid, args["account_id"], role=role)
        return {"executor": executor}

    def _list_evidence(self, args: dict) -> Any:
        pid = args["project_id"]
        self._member(pid)
        return {"evidence": self.store.list_evidence(project_id=pid)}

    def _verify_evidence(self, args: dict) -> Any:
        from .evidence_trust import EvidenceTrustEngine, Keyring, signer_from_env
        from .nexus_evidence import EvidenceService

        pid = args["project_id"]
        self._member(pid)
        service = EvidenceService(
            self.store,
            EvidenceTrustEngine(Keyring.from_env(), signer_from_env()),
        )
        rec = service.reverify(args["evidence_id"], project_id=pid,
                               expected_commit=args.get("expected_commit"))
        return {"evidence": rec}

    # ------------------------------------------------------------ dispatch
    def handle(self, method: str, params: dict) -> Any:
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        if method == "tools/list":
            return {"tools": self.tool_schemas()}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            fn = self.tools.get(name)
            if fn is None:
                raise McpError(-32601, f"unknown tool: {name}")
            # The permission policy decides whether this caller's role may run
            # this tool. A tool that declares no scope is refused, so a tool
            # added without an authority is not silently callable.
            try:
                require_tool_scope(self.role, name)
            except PolicyViolation as e:
                raise McpError(-32003, f"permission denied: {e}") from None
            try:
                result = fn(arguments)
            except NotFound:
                raise McpError(-32004, "not found") from None
            except AuthorizationError as e:
                raise McpError(-32003, str(e)) from None
            except StoreError as e:
                raise McpError(-32000, str(e)) from None
            except ValueError as e:
                raise McpError(-32602, str(e)) from None
            return {"content": [{"type": "text", "text": json.dumps(result, default=str)}],
                    "isError": False}
        raise McpError(-32601, f"unknown method: {method}")


def _s(type_name: str) -> dict[str, str]:
    return {"type": type_name}


def _tool(name: str, description: str, properties: dict[str, Any],
          required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    # The scope the tool needs, from the permission policy. It is advertised so a
    # client can see the authority a tool demands; the check itself is enforced
    # in ``handle``.
    scope = MCP_TOOL_SCOPES.get(name)
    tool: dict[str, Any] = {"name": name, "description": description,
                            "inputSchema": schema}
    if scope:
        tool["scope"] = scope
    return tool


def serve(server: McpServer, stdin: Any = None, stdout: Any = None) -> None:
    """Read newline-delimited JSON-RPC messages and write responses."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _write(stdout, {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "parse error"}})
            continue
        msg_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        # Notifications (no id) get no response.
        if msg_id is None:
            continue
        try:
            result = server.handle(method, params)
            _write(stdout, {"jsonrpc": "2.0", "id": msg_id, "result": result})
        except McpError as e:
            _write(stdout, {"jsonrpc": "2.0", "id": msg_id,
                            "error": {"code": e.code, "message": str(e)}})
        except Exception as e:  # noqa: BLE001 - never crash the transport
            log.exception("mcp handler failed")
            _write(stdout, {"jsonrpc": "2.0", "id": msg_id,
                            "error": {"code": -32603, "message": f"internal error: {type(e).__name__}"}})


def _write(stdout: Any, payload: dict) -> None:
    stdout.write(json.dumps(payload) + "\n")
    stdout.flush()


def handle_http(server: McpServer, message: dict) -> tuple[int, dict]:
    """Handle one JSON-RPC message over HTTP. Returns ``(http_status, body)``.

    MCP Streamable HTTP sends a single JSON-RPC object (or a batch) and expects
    a JSON-RPC response. A notification (no id) is acknowledged with 202 and no
    body, per the spec. A malformed message is a JSON-RPC error, still HTTP 200
    (the transport succeeded; the message was bad) except a true parse error,
    which the web server maps to 400 before calling this.
    """
    if not isinstance(message, dict):
        return 400, {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32600, "message": "invalid request"}}
    msg_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if msg_id is None:
        # A notification: no response body.
        return 202, {}
    try:
        result = server.handle(method, params)
        return 200, {"jsonrpc": "2.0", "id": msg_id, "result": result}
    except McpError as e:
        return 200, {"jsonrpc": "2.0", "id": msg_id,
                     "error": {"code": e.code, "message": str(e)}}
    except Exception as e:  # noqa: BLE001 - never crash the transport
        log.exception("mcp http handler failed")
        return 200, {"jsonrpc": "2.0", "id": msg_id,
                     "error": {"code": -32603,
                               "message": f"internal error: {type(e).__name__}"}}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    install_log_redaction()
    dsn = os.environ.get("NEXUS_DB_URL") or os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        print("set NEXUS_DB_URL (or SUPABASE_DB_URL)", file=sys.stderr)
        return 3
    principal = os.environ.get("NEXUS_MCP_PRINCIPAL") or "machine:default"
    store = PostgresStore(dsn)
    try:
        serve(McpServer(store, principal))
    finally:
        store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
