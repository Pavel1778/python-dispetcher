"""Validated, versioned project plans. No task in this module is executable.

The application, not a model, validates every plan; approval records a human's
intent but does not start OpenHands or imply that the work is completed.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from typing import Any

from .cloud_store import NotFound, PostgresStore
from .nexus_projects import IdempotencyConflict, ProjectService, _text

_TASK_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def validate_plan(payload: Any) -> list[dict[str, Any]]:
    """Validate an untrusted plan and return normalized tasks in input order."""
    if not isinstance(payload, dict) or set(payload) != {"tasks"}:
        raise ValueError("plan must contain only tasks")
    tasks = payload["tasks"]
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 100:
        raise ValueError("plan requires between 1 and 100 tasks")
    normalized = []
    keys: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict) or set(task) - {
            "id", "title", "description", "acceptance", "dependencies", "max_attempts"
        }:
            raise ValueError("task contains unknown fields or is not an object")
        key = task.get("id")
        if not isinstance(key, str) or not _TASK_KEY.fullmatch(key):
            raise ValueError("task id must be 1-64 letters, digits, _ or -, starting with a letter or digit")
        if key in keys:
            raise ValueError("duplicate task id")
        keys.add(key)
        title = _text(task.get("title"), "title", maximum=200)
        description = _text(task.get("description", ""), "description", maximum=4000, required=False)
        acceptance = task.get("acceptance")
        if not isinstance(acceptance, list) or not 1 <= len(acceptance) <= 20:
            raise ValueError("each task needs 1-20 acceptance criteria")
        criteria = [_text(item, "acceptance criterion", maximum=1000) for item in acceptance]
        deps = task.get("dependencies", [])
        if (not isinstance(deps, list) or len(deps) > 20
                or any(not isinstance(d, str) or not _TASK_KEY.fullmatch(d) for d in deps)):
            raise ValueError("dependencies must be a list of at most 20 task ids")
        if len(set(deps)) != len(deps) or key in deps:
            raise ValueError("duplicate or self dependency")
        attempts = task.get("max_attempts", 3)
        if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 10:
            raise ValueError("max_attempts must be an integer between 1 and 10")
        normalized.append({"id": key, "title": title, "description": description,
                           "acceptance": criteria, "dependencies": deps, "max_attempts": attempts})

    indegree = {t["id"]: len(t["dependencies"]) for t in normalized}
    outgoing: dict[str, list[str]] = {key: [] for key in keys}
    for task in normalized:
        for dep in task["dependencies"]:
            if dep not in keys:
                raise ValueError("dependency references an unknown task")
            outgoing[dep].append(task["id"])
    ready = deque(key for key, degree in indegree.items() if degree == 0)
    count = 0
    while ready:
        key = ready.popleft()
        count += 1
        for dependent in outgoing[key]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if count != len(normalized):
        raise ValueError("plan contains a dependency cycle")
    return normalized


class PlanService:
    def __init__(self, store: PostgresStore):
        self.store = store

    @staticmethod
    def _goal(cur: Any, principal: str, project_id: str, goal_id: str) -> None:
        ProjectService._project(cur, principal, project_id)
        # Serialize plan versions and approvals, including across processes.
        cur.execute("SELECT id FROM nexus_goals WHERE id=%s AND project_id=%s FOR UPDATE", (goal_id, project_id))
        if cur.fetchone() is None:
            raise NotFound("goal not found")

    def create(self, principal: str, project_id: str, goal_id: str, payload: Any,
               key: str | None = None) -> tuple[dict, bool]:
        tasks = validate_plan(payload)
        if key is not None:
            key = _text(key, "Idempotency-Key", maximum=200)
        digest = hashlib.sha256(json.dumps(tasks, sort_keys=True, ensure_ascii=False,
                                          separators=(",", ":")).encode()).hexdigest()
        with self.store._tx() as cur:
            self._goal(cur, principal, project_id, goal_id)
            if key is not None:
                cur.execute("SELECT id, payload_hash, created_by FROM nexus_plans "
                            "WHERE project_id=%s AND goal_id=%s AND idempotency_key=%s", (project_id, goal_id, key))
                prior = cur.fetchone()
                if prior:
                    if prior["payload_hash"] != digest or prior["created_by"] != principal:
                        raise IdempotencyConflict("idempotency key already used for a different plan")
                    return self._get(cur, principal, project_id, str(prior["id"])), False
            cur.execute("SELECT COALESCE(MAX(version),0)+1 AS version FROM nexus_plans WHERE goal_id=%s", (goal_id,))
            version = cur.fetchone()["version"]
            cur.execute("INSERT INTO nexus_plans(project_id,goal_id,version,created_by,idempotency_key,payload_hash) "
                        "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id", (project_id, goal_id, version, principal, key, digest))
            plan_id = cur.fetchone()["id"]
            for position, task in enumerate(tasks):
                cur.execute("INSERT INTO nexus_plan_tasks(plan_id,task_key,position,title,description,acceptance,max_attempts) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                            (plan_id, task["id"], position, task["title"], task["description"],
                             json.dumps(task["acceptance"], ensure_ascii=False), task["max_attempts"]))
            for task in tasks:
                for dependency in task["dependencies"]:
                    cur.execute("INSERT INTO nexus_plan_dependencies(plan_id,task_key,depends_on) VALUES (%s,%s,%s)",
                                (plan_id, task["id"], dependency))
            ProjectService._event(cur, project_id, principal, "plan.created", plan_id)
            return self._get(cur, principal, project_id, str(plan_id)), True

    def list(self, principal: str, project_id: str, goal_id: str) -> list[dict]:
        with self.store._tx() as cur:
            self._goal(cur, principal, project_id, goal_id)
            cur.execute("SELECT id, project_id, goal_id, version, state, created_by, created_at, approved_at, approved_by "
                        "FROM nexus_plans WHERE project_id=%s AND goal_id=%s ORDER BY version DESC LIMIT 200",
                        (project_id, goal_id))
            return [dict(row) for row in cur.fetchall()]

    def get(self, principal: str, project_id: str, plan_id: str) -> dict:
        with self.store._tx() as cur:
            return self._get(cur, principal, project_id, plan_id)

    @staticmethod
    def _get(cur: Any, principal: str, project_id: str, plan_id: str) -> dict:
        ProjectService._project(cur, principal, project_id)
        cur.execute("SELECT id, project_id, goal_id, version, state, created_by, created_at, approved_at, approved_by "
                    "FROM nexus_plans WHERE id=%s AND project_id=%s", (plan_id, project_id))
        row = cur.fetchone()
        if row is None:
            raise NotFound("plan not found")
        plan = dict(row)
        cur.execute("SELECT task_key AS id, title, description, acceptance, max_attempts "
                    "FROM nexus_plan_tasks WHERE plan_id=%s ORDER BY position", (plan_id,))
        tasks = [dict(task) for task in cur.fetchall()]
        cur.execute("SELECT task_key, depends_on FROM nexus_plan_dependencies WHERE plan_id=%s ORDER BY depends_on", (plan_id,))
        deps: dict[str, list[str]] = {t["id"]: [] for t in tasks}
        for edge in cur.fetchall():
            deps[edge["task_key"]].append(edge["depends_on"])
        for task in tasks:
            task["dependencies"] = deps[task["id"]]
        plan["tasks"] = tasks
        return plan

    def approve(self, principal: str, project_id: str, plan_id: str) -> dict:
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("SELECT role FROM nexus_project_members WHERE project_id=%s AND principal=%s", (project_id, principal))
            if cur.fetchone()["role"] != "owner":
                raise NotFound("plan not found")
            cur.execute("SELECT goal_id FROM nexus_plans WHERE id=%s AND project_id=%s", (plan_id, project_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("plan not found")
            goal_id = row["goal_id"]
            self._goal(cur, principal, project_id, goal_id)
            cur.execute("SELECT state FROM nexus_plans WHERE id=%s AND project_id=%s FOR UPDATE", (plan_id, project_id))
            state = cur.fetchone()["state"]
            if state == "approved":
                return self._get(cur, principal, project_id, plan_id)
            if state != "draft":
                raise ValueError("superseded plan cannot be approved")
            cur.execute("UPDATE nexus_plans SET state='superseded' WHERE goal_id=%s AND state='approved'", (goal_id,))
            cur.execute("UPDATE nexus_plans SET state='approved',approved_by=%s,approved_at=now() WHERE id=%s", (principal, plan_id))
            ProjectService._event(cur, project_id, principal, "plan.approved", plan_id)
            return self._get(cur, principal, project_id, plan_id)
