"""``nexus`` CLI: a thin client over the running NEXUS HTTP API.

Design rules (from docs/CLI_REFERENCE.md):

* **Thin client.** It calls the same HTTP endpoints the browser does; it never
  reaches into the store directly and never duplicates business logic. Every
  authorization check the server performs therefore applies to the CLI too.
* **Machine-readable.** Every command supports ``--json`` and returns a stable
  exit code (see :data:`EXIT`). Scripts can branch on the code, not on prose.
* **No secret leakage.** The token is read from ``NEXUS_TOKEN`` /
  ``WEB_AUTH_TOKEN`` and attached as a header; it is never printed, logged, or
  included in ``--json`` output. Diagnostics report presence only.
* **Works without a TTY.** No interactive prompt except a destructive action
  without ``--yes``, which is refused (exit 8) rather than guessed.

Usage::

    NEXUS_URL=https://nexus.example NEXUS_TOKEN=... nexus project list --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

#: Stable, machine-readable exit codes. Do not renumber: scripts depend on them.
EXIT = {
    "ok": 0,
    "error": 1,
    "usage": 2,
    "config": 3,
    "not_found": 4,
    "conflict": 5,
    "unavailable": 6,
    "timeout": 7,
    "refused": 8,
}


class CliError(RuntimeError):
    def __init__(self, message: str, code: int = EXIT["error"]):
        super().__init__(message)
        self.code = code


class Client:
    """Minimal bearer-authenticated JSON client for the NEXUS API."""

    def __init__(self, base_url: str, token: str | None, *, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None,
                 *, idempotency_key: str | None = None) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if idempotency_key:
            req.add_header("Idempotency-Key", idempotency_key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                payload = {"error": raw.decode(errors="replace")[:200]}
            return e.code, payload
        except urllib.error.URLError as e:
            raise CliError(f"cannot reach {self.base_url}: {e.reason}",
                           EXIT["unavailable"]) from e
        except TimeoutError as e:
            raise CliError("request timed out", EXIT["timeout"]) from e

    def get(self, path: str) -> Any:
        status, payload = self._request("GET", path)
        return self._ok(status, payload)

    def post(self, path: str, body: dict | None = None,
             *, idempotency_key: str | None = None) -> Any:
        status, payload = self._request("POST", path, body, idempotency_key=idempotency_key)
        return self._ok(status, payload)

    @staticmethod
    def _ok(status: int, payload: Any) -> Any:
        if 200 <= status < 300:
            return payload
        message = (payload or {}).get("error") if isinstance(payload, dict) else str(payload)
        if status == 404:
            raise CliError(message or "not found", EXIT["not_found"])
        if status in (401, 403):
            raise CliError(message or "unauthorized", EXIT["config"])
        if status == 409:
            raise CliError(message or "conflict", EXIT["conflict"])
        if status in (502, 503):
            raise CliError(message or "dependency unavailable", EXIT["unavailable"])
        raise CliError(message or f"HTTP {status}", EXIT["error"])


def _emit(args: argparse.Namespace, data: Any, *, human: str | None = None) -> None:
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    elif human is not None:
        print(human)
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False))


def _client(args: argparse.Namespace) -> Client:
    url = args.url or os.environ.get("NEXUS_URL") or os.environ.get("NEXUS_BASE_URL")
    if not url:
        raise CliError("set NEXUS_URL (or pass --url)", EXIT["config"])
    token = os.environ.get("NEXUS_TOKEN") or os.environ.get("WEB_AUTH_TOKEN")
    if not token:
        raise CliError("set NEXUS_TOKEN (a bearer token); the CLI never prompts",
                       EXIT["config"])
    return Client(url, token, timeout=args.timeout)


# --------------------------------------------------------------------- commands
def cmd_version(args: argparse.Namespace) -> int:
    from . import __version__ as v
    _emit(args, {"version": v}, human=v)
    return EXIT["ok"]


def cmd_doctor(args: argparse.Namespace) -> int:
    """Diagnose connectivity and configuration without printing any secret."""
    report: dict[str, Any] = {
        "nexus_url": args.url or os.environ.get("NEXUS_URL"),
        "token_present": bool(os.environ.get("NEXUS_TOKEN") or os.environ.get("WEB_AUTH_TOKEN")),
        "evidence_signer_present": bool(os.environ.get("NEXUS_EVIDENCE_PRIVATE_KEY")),
        "evidence_keyring_entries": len(
            [p for p in (os.environ.get("NEXUS_EVIDENCE_KEYRING") or "").split(",") if p.strip()]
        ),
        "checks": {},
    }
    code = EXIT["ok"]
    try:
        client = _client(args)
    except CliError as e:
        report["checks"]["client"] = {"ok": False, "detail": str(e)}
        _emit(args, report, human=f"doctor: {e}")
        return e.code
    try:
        report["checks"]["healthz"] = client.get("/healthz")
    except CliError as e:
        report["checks"]["healthz"] = {"ok": False, "detail": str(e)}
        code = max(code, EXIT["unavailable"])
    try:
        system = client.get("/api/system")
        report["checks"]["system"] = system
        # Surface the exact reason a subsystem is unavailable, without values.
        for name, entry in (system.get("components") or {}).items():
            report["checks"][name] = entry
    except CliError as e:
        report["checks"]["system"] = {"ok": False, "detail": str(e)}
        code = max(code, EXIT["unavailable"])
    human = "\n".join(
        f"{name}: {'ok' if (v is True or (isinstance(v, dict) and v.get('ok')) ) else v}"
        for name, v in report["checks"].items()
    )
    _emit(args, report, human=human)
    return code


def cmd_status(args: argparse.Namespace) -> int:
    client = _client(args)
    dash = client.get("/api/dashboard")
    health = client.get("/api/health")
    data = {"counts": dash.get("counts"), "health": health}
    counts = dash.get("counts") or {}
    human = "\n".join(f"{k}: {v}" for k, v in counts.items())
    _emit(args, data, human=human)
    return EXIT["ok"]


def cmd_project_list(args: argparse.Namespace) -> int:
    client = _client(args)
    projects = client.get("/api/v1/projects").get("projects", [])
    human = "\n".join(f"{p['id']}  {p['name']}" for p in projects) or "(no projects)"
    _emit(args, {"projects": projects}, human=human)
    return EXIT["ok"]


def cmd_project_create(args: argparse.Namespace) -> int:
    client = _client(args)
    body = {"name": args.name, "description": args.description or ""}
    out = client.post("/api/v1/projects", body, idempotency_key=args.idempotency_key)
    project = out.get("project", {})
    _emit(args, out, human=f"created {project.get('id')}  {project.get('name')}")
    return EXIT["ok"]


def cmd_project_show(args: argparse.Namespace) -> int:
    client = _client(args)
    project = client.get(f"/api/v1/projects/{args.project_id}").get("project", {})
    repos = client.get(f"/api/v1/projects/{args.project_id}/repositories").get("repositories", [])
    tasks = client.get(f"/api/v1/projects/{args.project_id}/tasks").get("tasks", [])
    data = {"project": project, "repositories": repos, "tasks": tasks}
    human = f"{project.get('name')} ({project.get('id')})\n" \
            f"repositories: {len(repos)}  tasks: {len(tasks)}"
    _emit(args, data, human=human)
    return EXIT["ok"]


def cmd_project_repo_add(args: argparse.Namespace) -> int:
    client = _client(args)
    out = client.post(f"/api/v1/projects/{args.project_id}/repositories",
                      {"full_name": args.full_name})
    _emit(args, out, human=f"linked {args.full_name} to {args.project_id}")
    return EXIT["ok"]


def cmd_project_repo_remove(args: argparse.Namespace) -> int:
    client = _client(args)
    out = client.post(f"/api/v1/projects/{args.project_id}/repositories/remove",
                      {"full_name": args.full_name})
    _emit(args, out, human=f"unlinked {args.full_name}")
    return EXIT["ok"]


def cmd_task_list(args: argparse.Namespace) -> int:
    client = _client(args)
    if args.project_id:
        tasks = client.get(f"/api/v1/projects/{args.project_id}/tasks").get("tasks", [])
    else:
        tasks = client.get("/api/tasks").get("tasks", [])
    human = "\n".join(f"{t['id']}  {t['state']:<12} {t.get('goal','')[:60]}" for t in tasks) or "(no tasks)"
    _emit(args, {"tasks": tasks}, human=human)
    return EXIT["ok"]


def cmd_task_create(args: argparse.Namespace) -> int:
    client = _client(args)
    if not args.project_id:
        raise CliError("--project is required (a task must have project context)",
                       EXIT["usage"])
    body = {"goal": args.goal, "repository": args.repository, "branch": args.branch}
    out = client.post(f"/api/v1/projects/{args.project_id}/tasks", body,
                      idempotency_key=args.idempotency_key)
    _emit(args, out, human=f"workflow {out.get('workflow_id')} started")
    return EXIT["ok"]


def cmd_task_show(args: argparse.Namespace) -> int:
    client = _client(args)
    if not args.project_id:
        raise CliError("--project is required", EXIT["usage"])
    detail = client.get(f"/api/v1/projects/{args.project_id}/tasks/{args.task_id}").get("task", {})
    _emit(args, detail, human=f"{detail.get('id')}  {detail.get('state')}")
    return EXIT["ok"]


def cmd_task_dispatch(args: argparse.Namespace) -> int:
    """Requeue a task for another attempt (the dispatcher picks it up)."""
    client = _client(args)
    out = client.post(f"/api/tasks/{args.task_id}/retry")
    _emit(args, out, human=f"task {args.task_id} requeued")
    return EXIT["ok"]


def cmd_workflow_list(args: argparse.Namespace) -> int:
    client = _client(args)
    if args.project_id:
        workflows = client.get(f"/api/v1/projects/{args.project_id}/workflows").get("workflows", [])
    else:
        workflows = client.get("/api/workflows").get("workflows", [])
    human = "\n".join(f"{w['id']}  {w['state']:<12} {w.get('goal','')[:60]}" for w in workflows) or "(no workflows)"
    _emit(args, {"workflows": workflows}, human=human)
    return EXIT["ok"]


def cmd_workflow_show(args: argparse.Namespace) -> int:
    client = _client(args)
    detail = client.get(f"/api/workflows/{args.workflow_id}")
    _emit(args, detail, human=f"{args.workflow_id}  {detail.get('workflow',{}).get('state')}")
    return EXIT["ok"]


def cmd_evidence_list(args: argparse.Namespace) -> int:
    client = _client(args)
    if not args.project_id:
        raise CliError("--project is required (evidence is project-scoped)",
                       EXIT["usage"])
    evidence = client.get(f"/api/v1/projects/{args.project_id}/evidence").get("evidence", [])
    human = "\n".join(
        f"{e['id']}  {e['verdict']:<10} {e.get('commit_sha') or '-'}  {e.get('summary','')[:40]}"
        for e in evidence
    ) or "(no evidence)"
    _emit(args, {"evidence": evidence}, human=human)
    return EXIT["ok"]


def cmd_evidence_verify(args: argparse.Namespace) -> int:
    client = _client(args)
    if not args.project_id:
        raise CliError("--project is required", EXIT["usage"])
    body = {"expected_commit": args.expected_commit} if args.expected_commit else {}
    out = client.post(
        f"/api/v1/projects/{args.project_id}/evidence/{args.evidence_id}/verify", body)
    rec = out.get("evidence", {})
    verdict = rec.get("verdict")
    _emit(args, out, human=f"{args.evidence_id}: {verdict}")
    return EXIT["ok"]


def cmd_agent_status(args: argparse.Namespace) -> int:
    client = _client(args)
    caps = client.get("/api/capabilities")
    health = client.get("/api/health")
    data = {"capabilities": caps, "health": health}
    cloud = health.get("cloud")
    _emit(args, data, human=f"cloud: {cloud}")
    return EXIT["ok"]


def cmd_qa_report(args: argparse.Namespace) -> int:
    """Summarize real system state: counts, health and integration status."""
    client = _client(args)
    dash = client.get("/api/dashboard").get("counts", {})
    system = client.get("/api/system")
    report = {"counts": dash, "components": system.get("components"),
              "build": system.get("build")}
    human = "\n".join(f"{k}: {v}" for k, v in (dash or {}).items())
    _emit(args, report, human=human)
    return EXIT["ok"]


def cmd_openhands_diagnostics(args: argparse.Namespace) -> int:
    """Run the OpenHands diagnostic report and exit non-zero on any FAILED.

    ``--live`` opts into the end-to-end conversation probe (creates and deletes
    one conversation). Without it, checks that need a live conversation are
    reported UNVERIFIED, never assumed.
    """
    client = _client(args)
    body: dict[str, Any] = {"live": bool(args.live)}
    if getattr(args, "account", None):
        body["account_id"] = args.account
    if getattr(args, "project", None):
        body["project_id"] = args.project
    report = client.post("/api/openhands/diagnostics", body)
    counts = report.get("counts", {})
    human_lines = [
        f"{c['name']}: {c['status']} — {c['detail']}" for c in report.get("checks", [])]
    human = "\n".join(human_lines) + "\n" + "  ".join(
        f"{k}={v}" for k, v in counts.items())
    _emit(args, report, human=human)
    return EXIT["ok"] if report.get("ok") else EXIT["unavailable"]


def cmd_openhands_accounts(args: argparse.Namespace) -> int:
    client = _client(args)
    accounts = client.get("/api/openhands/accounts").get("accounts", [])
    human = "\n".join(
        f"{a['id']}  {a['label']}  {a['state']}"
        f"{'  default' if a.get('is_default') else ''}"
        f"{'' if a.get('enabled', True) else '  DISABLED'}"
        f"  health={a.get('health', 'unknown')}"
        f"  key={a.get('api_key_hint') or '(none)'}"
        f"  errors={a.get('error_count', 0)}" for a in accounts) or "(no accounts)"
    _emit(args, {"accounts": accounts}, human=human)
    return EXIT["ok"]


def cmd_openhands_account_errors(args: argparse.Namespace) -> int:
    """The account's observed failures and recoveries, newest first."""
    client = _client(args)
    errors = client.get(
        f"/api/openhands/accounts/{args.account_id}/errors").get("errors", [])
    human = "\n".join(
        f"{e['created_at']}  {e['kind']}  {e['detail']}" for e in errors) or "(no errors)"
    _emit(args, {"errors": errors}, human=human)
    return EXIT["ok"]


def cmd_openhands_pool_show(args: argparse.Namespace) -> int:
    client = _client(args)
    data = client.get("/api/openhands/pool")
    cfg = data.get("config", {})
    human = f"mode: {cfg.get('selection_mode')}\n" + "\n".join(
        f"{a['id']}  {a['label']}  priority={a['priority']}  "
        f"health={a['health']}  active={a['active_tasks']}"
        for a in data.get("candidates", []))
    _emit(args, data, human=human)
    return EXIT["ok"]


def cmd_openhands_pool_set(args: argparse.Namespace) -> int:
    client = _client(args)
    body: dict[str, Any] = {}
    if args.mode:
        body["selection_mode"] = args.mode
    if args.order is not None:
        body["fallback_order"] = [x for x in args.order.split(",") if x]
    result = client.post("/api/openhands/pool", body)
    _emit(args, result, human=f"mode: {result['config']['selection_mode']}")
    return EXIT["ok"]


def cmd_openhands_switch(args: argparse.Namespace) -> int:
    """Hand a running task to a named OpenHands account (manual switch)."""
    client = _client(args)
    result = client.post(
        f"/api/v1/projects/{args.project_id}/runs/{args.run_id}/tasks/{args.task_id}/switch",
        {"account_id": args.account_id})
    _emit(args, result,
          human=f"task {args.task_id} will run on {result['target_account']['label']}")
    return EXIT["ok"]


def cmd_openhands_add(args: argparse.Namespace) -> int:
    """Add an OpenHands account. The key is verified, then stored encrypted."""
    client = _client(args)
    body: dict[str, Any] = {"label": args.label, "api_key": args.api_key}
    if args.base_url:
        body["base_url"] = args.base_url
    if args.transport:
        body["transport"] = args.transport
    if args.default_model:
        body["default_model"] = args.default_model
    if args.max_concurrent is not None:
        body["max_concurrent_tasks"] = args.max_concurrent
    if args.daily_limit is not None:
        body["daily_conversation_limit"] = args.daily_limit
    if args.default:
        body["make_default"] = True
    result = client.post("/api/openhands/accounts", body)
    account = result["account"]
    _emit(args, result,
          human=f"added {account['id']}  {account['label']}  key={account.get('api_key_hint', '')}")
    return EXIT["ok"]


def cmd_openhands_edit(args: argparse.Namespace) -> int:
    """Edit an account's non-secret fields. The API key is not changed here."""
    client = _client(args)
    body: dict[str, Any] = {}
    if args.label is not None:
        body["label"] = args.label
    if args.base_url is not None:
        body["base_url"] = args.base_url
    if args.transport is not None:
        body["transport"] = args.transport
    if args.default_model is not None:
        # ``none`` clears the field, matching the server's explicit-null contract.
        body["default_model"] = None if args.default_model == "none" else args.default_model
    if args.max_concurrent is not None:
        body["max_concurrent_tasks"] = None if args.max_concurrent == 0 else args.max_concurrent
    if args.daily_limit is not None:
        body["daily_conversation_limit"] = None if args.daily_limit == 0 else args.daily_limit
    result = client.post(f"/api/openhands/accounts/{args.account_id}/edit", body)
    account = result["account"]
    _emit(args, result, human=f"edited {account['id']}  {account['label']}")
    return EXIT["ok"]


def cmd_openhands_connection(args: argparse.Namespace) -> int:
    """Probe an account's endpoint reachability (distinct from authorization)."""
    client = _client(args)
    result = client.post(f"/api/openhands/accounts/{args.account_id}/connection", {})
    account = result["account"]
    state = "reachable" if account.get("reachable") else "unreachable"
    _emit(args, result, human=f"{account['label']}: {state}")
    return EXIT["ok"] if account.get("reachable") else EXIT["unavailable"]


def cmd_openhands_account_action(args: argparse.Namespace) -> int:
    """verify / default / enable / disable / remove an account by id."""
    client = _client(args)
    action = args.action
    if action == "verify":
        out = client.post(f"/api/openhands/accounts/{args.account_id}/verify", {})
        account = out["account"]
        _emit(args, out, human=f"{account['label']}: state={account['state']}")
        return EXIT["ok"]
    if action == "default":
        out = client.post(f"/api/openhands/accounts/{args.account_id}/default", {})
        _emit(args, out, human=f"default: {out['account']['label']}")
        return EXIT["ok"]
    if action in ("enable", "disable"):
        out = client.post(f"/api/openhands/accounts/{args.account_id}/enable",
                          {"enabled": action == "enable"})
        _emit(args, out, human=f"{out['account']['label']}: enabled={out['account']['enabled']}")
        return EXIT["ok"]
    if action == "remove":
        out = client.post(f"/api/openhands/accounts/{args.account_id}/remove",
                          {"force": bool(args.force)})
        _emit(args, out, human=f"removed {out['removed']}")
        return EXIT["ok"]
    if action == "priority":
        out = client.post(f"/api/openhands/accounts/{args.account_id}/priority",
                          {"priority": args.priority})
        _emit(args, out, human=f"{out['account']['label']}: priority={out['account']['priority']}")
        return EXIT["ok"]
    raise CliError(f"unknown action {action}", EXIT["usage"])


def cmd_run_show(args: argparse.Namespace) -> int:
    """Show an autonomous run with its tasks and its report, if any."""
    client = _client(args)
    data = client.get(f"/api/v1/projects/{args.project_id}/runs/{args.run_id}")
    run = data["run"]
    lines = [f"{run['id']}  {run['state']}  {run.get('policy', 'bounded')}  "
             f"cycle {run.get('cycle', 0)}  starts {run.get('total_iterations', 0)}"]
    if run.get("wait_reason"):
        lines.append(f"waiting: {run['wait_reason']}")
    if run.get("last_error"):
        lines.append(f"reason: {run['last_error']}")
    for task in data.get("tasks", []):
        lines.append(f"  {task['task_key']:>8}  {task['state']}  "
                     f"attempt {task['attempt']}/{task['max_attempts']}  cycle {task.get('cycle', 0)}")
    report = data.get("report")
    if report and report.get("body"):
        lines.append("")
        lines.append(f"report: {report['state']} ({report.get('outcome')})")
        lines.append(report["body"])
    _emit(args, data, human="\n".join(lines))
    return EXIT["ok"]


def cmd_run_cancel(args: argparse.Namespace) -> int:
    """Stop a run for good (terminal and idempotent)."""
    client = _client(args)
    result = client.post(f"/api/v1/projects/{args.project_id}/runs/{args.run_id}/cancel", {})
    _emit(args, result, human=f"run {result['run']['id']} -> {result['run']['state']}")
    return EXIT["ok"]


def cmd_run_hide(args: argparse.Namespace) -> int:
    """Hide a terminal run from the active lists without deleting it."""
    client = _client(args)
    result = client.post(f"/api/v1/projects/{args.project_id}/runs/{args.run_id}/hide", {})
    _emit(args, result, human=f"run {result['run']['id']} hidden")
    return EXIT["ok"]


def cmd_project_executors_show(args: argparse.Namespace) -> int:
    client = _client(args)
    data = client.get(f"/api/v1/projects/{args.project_id}/executors")
    human = "\n".join(
        f"{e['account_id']}  {e['role']:<8}  {e['label']}"
        f"{'' if e.get('enabled', True) else '  DISABLED'}"
        f"{'' if e.get('usable', True) else '  (cannot take work now)'}"
        for e in data["executors"]) \
        or "(no executors assigned: the project uses the default rule)"
    _emit(args, data, human=human)
    return EXIT["ok"]


def cmd_project_executors_set(args: argparse.Namespace) -> int:
    """Set the whole executor assignment: one primary, ordered fallbacks."""
    client = _client(args)
    primary = args.primary or None
    fallbacks = [x for x in (args.fallbacks or "").split(",") if x]
    result = client.post(f"/api/v1/projects/{args.project_id}/executors",
                         {"primary": primary, "fallbacks": fallbacks})
    _emit(args, result, human=f"{len(result['executors'])} executor(s) assigned")
    return EXIT["ok"]


def cmd_project_repos_primary(args: argparse.Namespace) -> int:
    client = _client(args)
    owner, name = args.full_name.split("/", 1)
    result = client.post(
        f"/api/v1/projects/{args.project_id}/repositories/{owner}/{name}/primary", {})
    _emit(args, result, human=f"{result['repository']['full_name']} is now primary")
    return EXIT["ok"]


def cmd_qa_verify(args: argparse.Namespace) -> int:
    """Verify the evidence hash chain for a project."""
    client = _client(args)
    if not args.project_id:
        raise CliError("--project is required", EXIT["usage"])
    evidence = client.get(f"/api/v1/projects/{args.project_id}/evidence").get("evidence", [])
    verdicts: dict[str, int] = {}
    for e in evidence:
        verdicts[e["verdict"]] = verdicts.get(e["verdict"], 0) + 1
    ok = verdicts.get("FAILED", 0) == 0
    data = {"count": len(evidence), "verdicts": verdicts, "ok": ok}
    _emit(args, data, human=f"{len(evidence)} record(s): {verdicts}")
    return EXIT["ok"] if ok else EXIT["conflict"]


def cmd_restore_point_create(args: argparse.Namespace) -> int:
    """Create a restore point (git bundle) for the repository. Local, no API."""
    from .restore_point import create_restore_point

    try:
        record = create_restore_point(
            args.repo, args.out, label=args.label,
        )
    except Exception as exc:  # RestorePointError and OS errors alike
        raise CliError(str(exc), EXIT["error"]) from exc
    human = (
        f"restore point created\n"
        f"  HEAD:   {record.head}\n"
        f"  branch: {record.branch or '(detached)'}\n"
        f"  bundle: {record.bundle_path} ({record.bundle_size} bytes)\n"
        f"  sha256: {record.bundle_sha256}\n"
        f"  dirty:  {record.dirty}"
        + (f" ({len(record.untracked)} untracked path(s))" if record.untracked else "")
    )
    _emit(args, record.to_dict(), human=human)
    return EXIT["ok"]


def cmd_restore_point_list(args: argparse.Namespace) -> int:
    from .restore_point import DEFAULT_DIR, list_restore_points
    from pathlib import Path

    out = args.out or str(Path(args.repo) / DEFAULT_DIR)
    points = list_restore_points(out)
    human = "\n".join(
        f"{p.created_at}  {p.head[:12]}  {'dirty' if p.dirty else 'clean'}  {p.bundle}"
        for p in points
    ) or "(no restore points)"
    _emit(args, {"count": len(points), "restore_points": [p.to_dict() for p in points]},
          human=human)
    return EXIT["ok"]


def cmd_restore_point_verify(args: argparse.Namespace) -> int:
    from .restore_point import verify_restore_point

    try:
        report = verify_restore_point(args.path, args.repo)
    except Exception as exc:
        raise CliError(str(exc), EXIT["error"]) from exc
    human = f"{report['status']}" + (
        "" if report["ok"] else "\n" + "\n".join(report["reasons"])
    )
    _emit(args, report, human=human)
    return EXIT["ok"] if report["ok"] else EXIT["conflict"]


# ----------------------------------------------------------------------- parser
def _add_global_flags(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Add the global options to *parser*.

    ``suppress`` is used on the shared parent given to every subparser: with
    ``SUPPRESS`` a subparser only writes the value when the flag was actually
    given, so ``nexus doctor --json`` works without clobbering the top-level
    default parsed from ``nexus --json doctor``.
    """
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument("--url", default=default, help="base URL of the NEXUS server (or NEXUS_URL)")
    parser.add_argument("--json", action="store_true",
                        default=(argparse.SUPPRESS if suppress else False),
                        help="machine-readable output")
    parser.add_argument("--no-color", action="store_true",
                        default=(argparse.SUPPRESS if suppress else False),
                        help="disable colour")
    parser.add_argument("--timeout", type=float,
                        default=(argparse.SUPPRESS if suppress else 30.0),
                        help="request timeout (seconds)")
    parser.add_argument("--yes", action="store_true",
                        default=(argparse.SUPPRESS if suppress else False),
                        help="confirm a destructive action")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_global_flags(common, suppress=True)

    parser = argparse.ArgumentParser(
        prog="nexus",
        description="NEXUS command-line client (thin client over the HTTP API).",
    )
    _add_global_flags(parser, suppress=False)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", parents=[common], help="print the version").set_defaults(func=cmd_version)
    sub.add_parser("doctor", parents=[common], help="diagnose connectivity and configuration").set_defaults(func=cmd_doctor)
    sub.add_parser("status", parents=[common], help="dashboard counts and health").set_defaults(func=cmd_status)

    project = sub.add_parser("project", parents=[common], help="manage projects").add_subparsers(dest="sub", required=True)
    project.add_parser("list", parents=[common], help="list projects").set_defaults(func=cmd_project_list)
    pc = project.add_parser("create", parents=[common], help="create a project")
    pc.add_argument("name")
    pc.add_argument("--description", default="")
    pc.add_argument("--idempotency-key", default=None)
    pc.set_defaults(func=cmd_project_create)
    ps = project.add_parser("show", parents=[common], help="show a project with repositories and tasks")
    ps.add_argument("project_id")
    ps.set_defaults(func=cmd_project_show)
    repo = project.add_parser("repo", parents=[common], help="manage project repositories").add_subparsers(dest="action", required=True)
    ra = repo.add_parser("add", parents=[common], help="link an allowlisted repository to a project")
    ra.add_argument("project_id")
    ra.add_argument("full_name")
    ra.set_defaults(func=cmd_project_repo_add)
    rr = repo.add_parser("remove", parents=[common], help="unlink a repository from a project")
    rr.add_argument("project_id")
    rr.add_argument("full_name")
    rr.set_defaults(func=cmd_project_repo_remove)
    rp = repo.add_parser("primary", parents=[common],
                         help="make one linked repository the project's primary")
    rp.add_argument("full_name")
    rp.add_argument("--project", dest="project_id", required=True)
    rp.set_defaults(func=cmd_project_repos_primary)

    execs = project.add_parser("executors", parents=[common],
                               help="assign OpenHands executors to a project").add_subparsers(
        dest="action", required=True)
    es = execs.add_parser("show", parents=[common], help="show the project's executors")
    es.add_argument("--project", dest="project_id", required=True)
    es.set_defaults(func=cmd_project_executors_show)
    eset = execs.add_parser("set", parents=[common],
                            help="set the whole assignment (one primary, ordered fallbacks)")
    eset.add_argument("--project", dest="project_id", required=True)
    eset.add_argument("--primary", default=None, help="account id used first")
    eset.add_argument("--fallbacks", default=None, help="comma-separated account ids, in order")
    eset.set_defaults(func=cmd_project_executors_set)

    run = sub.add_parser("run", parents=[common],
                         help="inspect and control autonomous runs").add_subparsers(
        dest="sub", required=True)
    rs = run.add_parser("show", parents=[common], help="show a run, its tasks and its report")
    rs.add_argument("run_id")
    rs.add_argument("--project", dest="project_id", required=True)
    rs.set_defaults(func=cmd_run_show)
    rc = run.add_parser("cancel", parents=[common], help="stop a run for good")
    rc.add_argument("run_id")
    rc.add_argument("--project", dest="project_id", required=True)
    rc.set_defaults(func=cmd_run_cancel)
    rh = run.add_parser("hide", parents=[common], help="hide a terminal run from the lists")
    rh.add_argument("run_id")
    rh.add_argument("--project", dest="project_id", required=True)
    rh.set_defaults(func=cmd_run_hide)

    task = sub.add_parser("task", parents=[common], help="manage tasks").add_subparsers(dest="sub", required=True)
    tl = task.add_parser("list", parents=[common], help="list tasks")
    tl.add_argument("--project", dest="project_id", default=None)
    tl.set_defaults(func=cmd_task_list)
    tc = task.add_parser("create", parents=[common], help="create a task in a project (starts its workflow)")
    tc.add_argument("goal")
    tc.add_argument("--project", dest="project_id", required=True)
    tc.add_argument("--repository", default=None)
    tc.add_argument("--branch", default=None)
    tc.add_argument("--idempotency-key", default=None)
    tc.set_defaults(func=cmd_task_create)
    ts = task.add_parser("show", parents=[common], help="show a task with its events")
    ts.add_argument("task_id")
    ts.add_argument("--project", dest="project_id", required=True)
    ts.set_defaults(func=cmd_task_show)
    td = task.add_parser("dispatch", parents=[common], help="requeue a task for another attempt")
    td.add_argument("task_id")
    td.set_defaults(func=cmd_task_dispatch)

    workflow = sub.add_parser("workflow", parents=[common], help="manage workflows").add_subparsers(dest="sub", required=True)
    wl = workflow.add_parser("list", parents=[common], help="list workflows")
    wl.add_argument("--project", dest="project_id", default=None)
    wl.set_defaults(func=cmd_workflow_list)
    ws = workflow.add_parser("show", parents=[common], help="show a workflow")
    ws.add_argument("workflow_id")
    ws.set_defaults(func=cmd_workflow_show)

    evidence = sub.add_parser("evidence", parents=[common], help="manage evidence").add_subparsers(dest="sub", required=True)
    el = evidence.add_parser("list", parents=[common], help="list project evidence")
    el.add_argument("--project", dest="project_id", required=True)
    el.set_defaults(func=cmd_evidence_list)
    ev = evidence.add_parser("verify", parents=[common], help="re-verify one evidence record")
    ev.add_argument("evidence_id")
    ev.add_argument("--project", dest="project_id", required=True)
    ev.add_argument("--expected-commit", default=None)
    ev.set_defaults(func=cmd_evidence_verify)

    agent = sub.add_parser("agent", parents=[common], help="agent/account status").add_subparsers(dest="sub", required=True)
    agent.add_parser("status", parents=[common], help="account capability and health").set_defaults(func=cmd_agent_status)

    oh = sub.add_parser("openhands", parents=[common], help="OpenHands accounts, pool and diagnostics").add_subparsers(dest="sub", required=True)
    oh.add_parser("accounts", parents=[common], help="list OpenHands accounts").set_defaults(func=cmd_openhands_accounts)
    oha = oh.add_parser("add", parents=[common], help="add an account (verifies the key)")
    oha.add_argument("--label", required=True)
    oha.add_argument("--api-key", required=True, help="the API key (stored encrypted)")
    oha.add_argument("--base-url", default=None)
    oha.add_argument("--transport", choices=["direct", "app"], default=None)
    oha.add_argument("--default-model", default=None)
    oha.add_argument("--max-concurrent", type=int, default=None,
                     help="maximum concurrent tasks (default: no limit)")
    oha.add_argument("--daily-limit", type=int, default=None,
                     help="the account's daily conversation ceiling (default: unknown)")
    oha.add_argument("--default", action="store_true", help="make this the default account")
    oha.set_defaults(func=cmd_openhands_add)
    ohe = oh.add_parser("edit", parents=[common], help="edit an account's non-secret fields")
    ohe.add_argument("--account", dest="account_id", required=True)
    ohe.add_argument("--label", default=None)
    ohe.add_argument("--base-url", default=None)
    ohe.add_argument("--transport", choices=["direct", "app"], default=None)
    ohe.add_argument("--default-model", default=None,
                     help="a model name, or 'none' to clear it")
    ohe.add_argument("--max-concurrent", type=int, default=None,
                     help="a positive integer, or 0 to clear the limit")
    ohe.add_argument("--daily-limit", type=int, default=None,
                     help="a positive integer, or 0 to clear the limit")
    ohe.set_defaults(func=cmd_openhands_edit)
    ohc = oh.add_parser("connection", parents=[common], help="probe an account's endpoint reachability")
    ohc.add_argument("--account", dest="account_id", required=True)
    ohc.set_defaults(func=cmd_openhands_connection)
    ohac = oh.add_parser("account", parents=[common], help="per-account action (verify/default/enable/disable/remove/priority)")
    ohac.add_argument("action", choices=["verify", "default", "enable", "disable", "remove", "priority"])
    ohac.add_argument("--account", dest="account_id", required=True)
    ohac.add_argument("--priority", type=int, default=None, help="required for the priority action")
    ohac.add_argument("--force", action="store_true", help="remove even with active tasks")
    ohac.set_defaults(func=cmd_openhands_account_action)
    ohx = oh.add_parser("errors", parents=[common],
                        help="show an account's error history (failures and recoveries)")
    ohx.add_argument("--account", dest="account_id", required=True)
    ohx.set_defaults(func=cmd_openhands_account_errors)
    ohd = oh.add_parser("diagnostics", parents=[common], help="run the diagnostic report")
    ohd.add_argument("--live", action="store_true",
                     help="also run the end-to-end conversation probe (creates and deletes one conversation)")
    ohd.add_argument("--account", default=None, help="check one account id")
    ohd.add_argument("--project", default=None, help="select the account this project would use")
    ohd.set_defaults(func=cmd_openhands_diagnostics)
    pool = oh.add_parser("pool", parents=[common], help="account pool configuration").add_subparsers(dest="action", required=True)
    pool.add_parser("show", parents=[common], help="show the pool configuration and order").set_defaults(func=cmd_openhands_pool_show)
    pool_set = pool.add_parser("set", parents=[common], help="set the selection mode / fallback order")
    pool_set.add_argument("--mode", choices=["manual", "auto", "failover", "load_balancing"], default=None)
    pool_set.add_argument("--order", default=None,
                          help="comma-separated account ids, in failover order")
    pool_set.set_defaults(func=cmd_openhands_pool_set)
    osw = oh.add_parser("switch", parents=[common],
                        help="hand a running task to a named account")
    osw.add_argument("--project", dest="project_id", required=True)
    osw.add_argument("--run", dest="run_id", required=True)
    osw.add_argument("--task", dest="task_id", required=True)
    osw.add_argument("--account", dest="account_id", required=True)
    osw.set_defaults(func=cmd_openhands_switch)

    rp = sub.add_parser("restore-point", parents=[common],
                        help="safe restore points (local git bundles)")
    rp_sub = rp.add_subparsers(dest="sub", required=True)
    rpc = rp_sub.add_parser("create", parents=[common],
                            help="create a restore point before making changes")
    rpc.add_argument("--repo", default=".", help="path inside the repository")
    rpc.add_argument("--out", default=None, help="output directory (default .restore-points)")
    rpc.add_argument("--label", default=None, help="a short note to record with the point")
    rpc.set_defaults(func=cmd_restore_point_create)
    rpl = rp_sub.add_parser("list", parents=[common], help="list restore points")
    rpl.add_argument("--repo", default=".", help="path inside the repository")
    rpl.add_argument("--out", default=None, help="output directory (default .restore-points)")
    rpl.set_defaults(func=cmd_restore_point_list)
    rpv = rp_sub.add_parser("verify", parents=[common],
                            help="verify a restore point's integrity")
    rpv.add_argument("path", help="a .json metadata file or a .bundle file")
    rpv.add_argument("--repo", default=None, help="repository used for git bundle verify")
    rpv.set_defaults(func=cmd_restore_point_verify)

    qa = sub.add_parser("qa", parents=[common], help="QA and evidence reports").add_subparsers(dest="sub", required=True)
    qa.add_parser("report", parents=[common], help="real system report").set_defaults(func=cmd_qa_report)
    qv = qa.add_parser("verify", parents=[common], help="verify project evidence chain")
    qv.add_argument("--project", dest="project_id", required=True)
    qv.set_defaults(func=cmd_qa_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CliError as e:
        payload = {"error": str(e)}
        if getattr(args, "json", False):
            print(json.dumps(payload))
        else:
            print(f"error: {e}", file=sys.stderr)
        return e.code
    except BrokenPipeError:
        return EXIT["ok"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
