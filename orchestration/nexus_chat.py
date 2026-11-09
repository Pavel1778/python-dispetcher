"""The chat brain: turns a conversation turn into a real assistant reply.

The four chat modes map to real backend behaviour, never to a decorative label:

* ``ask``        — a natural-language answer from the LLM provider cascade,
                   grounded in the project's stored context and memory.
* ``plan``       — a validated, dependency-aware plan (the same validator the
                   Autonomous Lab uses). The plan is returned to the caller,
                   which stores it as a draft; nothing is dispatched.
* ``code``       — a real OpenHands workflow is started for the selected
                   repository/branch; the reply names the workflow id.
* ``autonomous`` — a real Autonomous Lab run is started; the reply names the run.

If no provider is configured, ``ask``/``plan`` fail loudly (the caller records an
``error`` message); they never fabricate an answer. ``code``/``autonomous`` do not
need a provider of their own — the dispatch hook does the work — but a planning
provider is required before a run can be started, and its absence is reported.
"""
from __future__ import annotations

from typing import Any, Callable

from .providers import (
    AllProvidersFailed,
    ProviderCascade,
    ProviderError,
    validate_plan_json,
)

_MODES = ("ask", "code", "plan", "autonomous")

_ASK_SYSTEM = (
    "You are NEXUS, an AI development assistant embedded in an IDE. Answer the "
    "developer's question about their project concisely and concretely. Use the "
    "provided project context and memory when relevant. Do not invent files or "
    "facts you were not given; say what you would need to check instead."
)

_PLAN_SYSTEM = (
    "You are NEXUS, a planning assistant. Turn the request into a dependency-aware "
    "plan. Reply with JSON only, of the form "
    '{"tasks": [{"id": "task-1", "title": "...", "description": "...", '
    '"dependencies": []}]}. Ids are short slugs; dependencies name other task ids. '
    "Keep the plan small and ordered. No prose outside the JSON."
)


class ChatError(RuntimeError):
    """A chat turn could not be answered. Never a fabricated answer."""


class ChatResponder:
    """Produce assistant replies for a conversation turn.

    *dispatch_code* and *start_autonomous* are injected so this class stays free
    of the orchestration wiring and can be tested with a real provider HTTP
    server and a stub dispatch that records what it was asked to do.
    """

    def __init__(self, providers: ProviderCascade, *,
                 dispatch_code: Callable[[dict], dict] | None = None,
                 start_autonomous: Callable[[dict], dict] | None = None):
        self.providers = providers
        self._dispatch_code = dispatch_code
        self._start_autonomous = start_autonomous

    def respond(self, *, mode: str, goal: str, context: dict | None = None,
                memory: list[dict] | None = None, repository: str = "",
                branch: str = "", history: list[dict] | None = None) -> list[dict]:
        """Return the messages to append for this turn.

        Each message is ``{"role", "content", "meta"}``. A failure is raised as
        :class:`ChatError` so the caller records it as an ``error`` message
        rather than presenting a made-up answer.
        """
        if mode not in _MODES:
            raise ValueError("mode must be ask, code, plan or autonomous")
        if mode == "ask":
            return [self._ask(goal, context, memory, history)]
        if mode == "plan":
            return [self._plan(goal, context, memory)]
        if mode == "code":
            return self._code(goal, repository, branch)
        return self._autonomous(goal, repository, branch)

    # ------------------------------------------------------------------- modes
    def _ask(self, goal: str, context: dict | None, memory: list[dict] | None,
             history: list[dict] | None) -> dict:
        prompt = self._prompt(goal, context, memory, history)
        try:
            response = self.providers.text(prompt, _ASK_SYSTEM)
        except AllProvidersFailed as e:
            raise ChatError(f"no orchestrator provider succeeded: {e}") from e
        except ProviderError as e:
            raise ChatError(str(e)) from e
        return {"role": "assistant", "content": response.data["text"],
                "meta": {"provider": response.provider, "model": response.model,
                         "mode": "ask"}}

    def _plan(self, goal: str, context: dict | None, memory: list[dict] | None) -> dict:
        prompt = self._prompt(goal, context, memory, None)
        try:
            result = self.providers.plan(prompt, _PLAN_SYSTEM)
        except AllProvidersFailed as e:
            raise ChatError(f"no orchestrator provider succeeded: {e}") from e
        except ProviderError as e:
            raise ChatError(str(e)) from e
        except ValueError as e:
            raise ChatError(f"provider returned an invalid plan: {e}") from e
        lines = ["**План работы**", ""]
        for i, task in enumerate(result.plan, 1):
            deps = task.get("dependencies") or []
            dep = f" (после: {', '.join(deps)})" if deps else ""
            lines.append(f"{i}. **{task['title']}**{dep}")
            if task.get("description"):
                lines.append(f"   {task['description']}")
        return {"role": "assistant", "content": "\n".join(lines),
                "meta": {"provider": result.provider, "mode": "plan",
                         "plan": result.plan}}

    def _code(self, goal: str, repository: str, branch: str) -> list[dict]:
        if self._dispatch_code is None:
            raise ChatError("code mode is not wired to a dispatcher")
        if not repository:
            raise ChatError("bind a repository to the project before using Code mode")
        try:
            out = self._dispatch_code({"goal": goal, "repository": repository,
                                       "branch": branch})
        except Exception as e:  # noqa: BLE001 - surfaced, never swallowed
            raise ChatError(f"could not start the OpenHands workflow: {e}") from e
        workflow_id = out.get("workflow_id") or out.get("id")
        content = ("Запущена задача OpenHands для репозитория "
                   f"`{repository}` (ветка `{branch}`).\n\n"
                   f"Идентификатор workflow: `{workflow_id}`.\n"
                   "Ход выполнения и изменения доступны на панели агента справа.")
        tool = {"role": "tool", "content": f"start_workflow → {workflow_id}",
                "meta": {"tool": "start_workflow", "workflow_id": workflow_id,
                         "repository": repository, "branch": branch}}
        return [tool, {"role": "assistant", "content": content,
                       "meta": {"mode": "code", "workflow_id": workflow_id}}]

    def _autonomous(self, goal: str, repository: str, branch: str) -> list[dict]:
        if self._start_autonomous is None:
            raise ChatError("autonomous mode is not wired to the lab")
        if not repository:
            raise ChatError("bind a repository to the project before using Autonomous mode")
        try:
            out = self._start_autonomous({"goal": goal, "repository": repository,
                                          "branch": branch})
        except Exception as e:  # noqa: BLE001 - surfaced, never swallowed
            raise ChatError(f"could not start the autonomous run: {e}") from e
        run_id = out.get("run_id") or out.get("id")
        tasks = out.get("tasks") or []
        content = (f"Запущена автономная работа над целью в `{repository}` "
                   f"(ветка `{branch}`).\n\n"
                   f"Идентификатор запуска: `{run_id}`.\n"
                   f"Задач в плане: {len(tasks)}.\n"
                   "Прогресс и проверки доступны на вкладке «Автономная лаборатория».")
        tool = {"role": "tool", "content": f"start_run → {run_id}",
                "meta": {"tool": "start_run", "run_id": run_id,
                         "tasks": len(tasks), "repository": repository,
                         "branch": branch}}
        return [tool, {"role": "assistant", "content": content,
                       "meta": {"mode": "autonomous", "run_id": run_id}}]

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _prompt(goal: str, context: dict | None, memory: list[dict] | None,
                history: list[dict] | None) -> str:
        parts: list[str] = []
        if context:
            summary = context.get("summary") if isinstance(context, dict) else None
            if summary:
                import json

                parts.append("Project context:\n" + json.dumps(summary, default=str)[:4000])
        if memory:
            facts = [m.get("content") for m in memory if m.get("content")][:30]
            if facts:
                parts.append("Known facts:\n- " + "\n- ".join(facts))
        if history:
            recent = [f"{m['role']}: {m['content']}" for m in history[-10:]
                      if m.get("content")]
            if recent:
                parts.append("Recent conversation:\n" + "\n".join(recent))
        parts.append("Request:\n" + goal)
        return "\n\n".join(parts)


def plan_tasks(data: Any) -> list[dict[str, Any]]:
    """Validate a plan body the same way the Autonomous Lab does."""
    if not isinstance(data, dict):
        raise ValueError("plan must be an object")
    return validate_plan_json(data)
