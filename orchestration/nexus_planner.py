"""Planning: turn a goal + context + memory into run tasks.

The plan comes from the provider cascade (``providers.py``), but the *shape* and
the safety of the plan are enforced here, by the application, never by the
model:

* Every task gets a stable key, acceptance criteria and a dependency list.
* Dependencies must reference keys in the same plan; self- and duplicate
  dependencies and cycles are rejected (topological check).
* A task is only persisted if the whole plan validates, so a bad plan leaves no
  half-written run.

The planner never dispatches: it writes ``pending`` tasks. Dispatch is the
scheduler's job, and only after a dependency is *verified*.
"""
from __future__ import annotations

import re
from collections import deque
from typing import Any

from .providers import ProviderCascade

_TASK_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def validate_tasks(payload: Any) -> list[dict[str, Any]]:
    """Validate a plan's task list and return it normalized, in input order."""
    tasks = payload.get("tasks") if isinstance(payload, dict) else payload
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 50:
        raise ValueError("plan must contain between 1 and 50 tasks")
    normalized: list[dict[str, Any]] = []
    keys: set[str] = set()
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"task {i} is not an object")
        key = task.get("id") or f"task-{i + 1}"
        if not isinstance(key, str) or not _TASK_KEY.fullmatch(key):
            raise ValueError(f"task {i} has an invalid id")
        if key in keys:
            raise ValueError(f"duplicate task id {key}")
        keys.add(key)
        title = task.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"task {key} has no title")
        acceptance = task.get("acceptance", [])
        if isinstance(acceptance, str):
            acceptance = [acceptance]
        if not isinstance(acceptance, list) or any(not isinstance(a, str) for a in acceptance):
            raise ValueError(f"task {key} acceptance must be a list of strings")
        deps = task.get("dependencies", [])
        if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
            raise ValueError(f"task {key} dependencies must be a list of ids")
        if len(set(deps)) != len(deps) or key in deps:
            raise ValueError(f"task {key} has a duplicate or self dependency")
        normalized.append({
            "id": key,
            "title": title.strip()[:200],
            "description": (task.get("description") or "")[:4000]
            if isinstance(task.get("description"), str) else "",
            "acceptance": [a[:1000] for a in acceptance][:20],
            "dependencies": deps[:20],
        })
    # Cycle + unknown-dependency check.
    outgoing: dict[str, list[str]] = {t["id"]: [] for t in normalized}
    indegree: dict[str, int] = {t["id"]: 0 for t in normalized}
    for task in normalized:
        for dep in task["dependencies"]:
            if dep not in keys:
                raise ValueError(f"task {task['id']} depends on unknown task {dep}")
            outgoing[dep].append(task["id"])
            indegree[task["id"]] += 1
    ready = deque(k for k, d in indegree.items() if d == 0)
    seen = 0
    while ready:
        key = ready.popleft()
        seen += 1
        for nxt in outgoing[key]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
    if seen != len(normalized):
        raise ValueError("plan contains a dependency cycle")
    return normalized


PLANNER_SYSTEM = (
    "You are a planning component. Return ONLY a JSON object of the form "
    '{"tasks": [{"id": "t1", "title": "...", "description": "...", '
    '"acceptance": ["..."], "dependencies": ["t1"]}]}. '
    "Use short stable ids. Dependencies must reference ids in the same plan. "
    "Do not include any prose outside the JSON."
)


class Planner:
    """Produce validated tasks for a goal using the provider cascade."""

    def __init__(self, cascade: ProviderCascade):
        self.cascade = cascade

    def plan(self, *, goal: str, project_name: str, context: dict | None = None,
             memory: list[dict] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return ``(tasks, meta)`` where meta names the provider and failovers.

        Raises :class:`ProviderError` when no provider can produce a valid plan;
        the caller decides how to surface that (it is a provider outage, not a
        plan).
        """
        prompt = self._prompt(goal, project_name, context or {}, memory or [])
        result = self.cascade.plan(prompt, PLANNER_SYSTEM)
        tasks = validate_tasks(result.response.data)
        meta = {"provider": result.provider, "failovers": result.failovers,
                "task_count": len(tasks)}
        return tasks, meta

    @staticmethod
    def _prompt(goal: str, project_name: str, context: dict, memory: list[dict]) -> str:
        lines = [f"Project: {project_name}", f"Goal: {goal}", ""]
        if context:
            langs = ", ".join(f"{k} ({v})" for k, v in (context.get("languages") or {}).items())
            lines.append(f"Detected languages: {langs or 'unknown'}")
            if context.get("build_commands"):
                lines.append("Build/test commands: " + "; ".join(context["build_commands"]))
            if context.get("test_file_count"):
                lines.append(f"Existing test files: {context['test_file_count']}")
            if context.get("readme_excerpt"):
                lines.append("README excerpt:\n" + str(context["readme_excerpt"])[:1500])
            lines.append("")
        verified = [m for m in memory if m.get("provenance") in
                    ("test_verified", "independently_verified")]
        if verified:
            lines.append("Verified project memory:")
            lines.extend(f"- {m['content']}" for m in verified[:20])
            lines.append("")
        lines.append("Decompose the goal into the smallest set of tasks that "
                     "reaches it, each with acceptance criteria and dependencies.")
        return "\n".join(lines)


def store_plan(lab_store: Any, principal: str, project_id: str, run_id: str, *,
               tasks: list[dict[str, Any]], repository: str = "", branch: str = "",
               executor: str = "openhands", timeout_seconds: int = 1800,
               max_attempts: int = 3) -> list[dict]:
    """Persist a validated plan as run tasks. Returns the stored rows."""
    stored = []
    for task in tasks:
        stored.append(lab_store.add_task(
            principal, project_id, run_id,
            task_key=task["id"], title=task["title"], description=task["description"],
            acceptance=task["acceptance"], dependencies=task["dependencies"],
            repository=repository, branch=branch, executor=executor,
            timeout_seconds=timeout_seconds, max_attempts=max_attempts))
    return stored
