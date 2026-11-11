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
                   A text too long for one planning request is planned in parts
                   in the background: the reply says so, and the run is
                   announced in the conversation when it starts. Sending the
                   same text again never starts a second run.

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
                branch: str = "", history: list[dict] | None = None,
                project_id: str | None = None,
                principal: str | None = None,
                conversation_id: str | None = None,
                spec_id: str | None = None) -> list[dict]:
        """Return the messages to append for this turn.

        Each message is ``{"role", "content", "meta"}``. A failure is raised as
        :class:`ChatError` so the caller records it as an ``error`` message
        rather than presenting a made-up answer.

        *project_id* and *principal* identify whose turn this is. They are handed
        to the dispatch hooks unchanged, so the work a turn starts is scoped to
        the conversation's project instead of being created outside any project.

        *spec_id* names the stored copy of this turn's text when there is one
        (``nexus_specs``). *goal* may be a shortened or trimmed form of what
        was sent; the autonomous hook works from the stored copy, which is
        exact. *conversation_id* lets it recognise the same text sent twice.
        """
        if mode not in _MODES:
            raise ValueError("mode must be ask, code, plan or autonomous")
        if mode == "ask":
            return [self._ask(goal, context, memory, history)]
        if mode == "plan":
            return [self._plan(goal, context, memory)]
        scope = {"project_id": project_id, "principal": principal}
        if mode == "code":
            return self._code(goal, repository, branch, scope)
        return self._autonomous(goal, repository, branch,
                                {**scope, "conversation_id": conversation_id,
                                 "spec_id": spec_id})

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

    def _code(self, goal: str, repository: str, branch: str,
              scope: dict | None = None) -> list[dict]:
        if self._dispatch_code is None:
            raise ChatError("code mode is not wired to a dispatcher")
        if not repository:
            raise ChatError("bind a repository to the project before using Code mode")
        try:
            out = self._dispatch_code({"goal": goal, "repository": repository,
                                       "branch": branch, **(scope or {})})
        except Exception as e:  # noqa: BLE001 - surfaced, never swallowed
            raise ChatError(f"could not start the OpenHands workflow: {e}") from e
        workflow_id = out.get("workflow_id") or out.get("id")
        # The id travels in ``meta`` and in the tool message; the text is what a
        # person reads, so it names the repository and where to watch the work.
        content = (f"Задача передана OpenHands: репозиторий `{repository}`, "
                   f"ветка `{branch}`.\n\n"
                   "Ход работы виден на вкладке «Агент».")
        tool = {"role": "tool", "content": f"start_workflow → {workflow_id}",
                "meta": {"tool": "start_workflow", "workflow_id": workflow_id,
                         "repository": repository, "branch": branch}}
        return [tool, {"role": "assistant", "content": content,
                       "meta": {"mode": "code", "workflow_id": workflow_id}}]

    def _autonomous(self, goal: str, repository: str, branch: str,
                    scope: dict | None = None) -> list[dict]:
        if self._start_autonomous is None:
            raise ChatError("autonomous mode is not wired to the lab")
        if not repository:
            raise ChatError("bind a repository to the project before using Autonomous mode")
        try:
            out = self._start_autonomous({"goal": goal, "repository": repository,
                                          "branch": branch, **(scope or {})})
        except Exception as e:  # noqa: BLE001 - surfaced, never swallowed
            raise ChatError(f"could not start the autonomous run: {e}") from e
        spec = out.get("spec") or {}
        if out.get("status") == "planning":
            # No run yet: the plan is being built part by part. The run is
            # announced in this conversation by whoever finishes the plan.
            stages = out.get("stages") or {}
            size = (f"{spec.get('chars')} символов, {spec.get('lines')} строк, "
                    f"SHA-256 {str(spec.get('sha256') or '')[:12]}…")
            content = (
                f"ТЗ принято целиком и сохранено без изменений: {size}\n\n"
                "Оно больше, чем модель планирования читает за один запрос, поэтому план "
                f"строится по частям: готово {stages.get('planned', 0)} из "
                f"{stages.get('total', 0)}. Прогон запустится сам, когда план будет готов, "
                "и сообщение об этом появится здесь. Повторная отправка этого же текста "
                "второй прогон не запускает.")
            return [{"role": "assistant", "content": content,
                     "meta": {"mode": "autonomous", "spec_id": spec.get("id"),
                              "planning": stages}}]
        if out.get("status") == "existing":
            run_id = out.get("run_id")
            content = ("Этот текст уже выполняется: прогон по нему идёт, второй не запущен.\n\n"
                       "Ход работы и проверки видны на вкладке «Агент».")
            return [{"role": "assistant", "content": content,
                     "meta": {"mode": "autonomous", "run_id": run_id, "existing": True,
                              "spec_id": spec.get("id")}}]
        run_id = out.get("run_id") or out.get("id")
        tasks = out.get("tasks") or []
        content = (f"Автономный прогон запущен: репозиторий `{repository}`, "
                   f"ветка `{branch}`, задач в плане: {len(tasks)}.\n\n"
                   "Ход работы и проверки видны на вкладке «Агент».")
        tool_meta = {"tool": "start_run", "run_id": run_id,
                     "tasks": len(tasks), "repository": repository,
                     "branch": branch}
        reply_meta = {"mode": "autonomous", "run_id": run_id}
        if spec.get("id"):
            tool_meta["spec_id"] = reply_meta["spec_id"] = spec["id"]
            if (out.get("plan") or {}).get("coverage"):
                tool_meta["coverage"] = out["plan"]["coverage"]
        tool = {"role": "tool", "content": f"start_run → {run_id}", "meta": tool_meta}
        return [tool, {"role": "assistant", "content": content, "meta": reply_meta}]

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
