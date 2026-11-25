"""LLM-driven replanner: a corrective instruction for a task that did not verify.

A durable run reworks a task that was not accepted (``LabScheduler._retry``).
The rework note it can write by itself is deterministic: "your report names no
commit, push and name the head". That covers the mechanical failures. It cannot
read *why* an agent went the wrong way and say what to do differently.

This module is that missing step, and only that step:

* it is given the failure (the verifier's reason and evidence), the agent's own
  report and the task's identity;
* it asks the **provider cascade** (the same one the chat and the planner use)
  for a short, concrete corrective instruction;
* it returns it, or ``None``. It never changes a task's state, never decides
  that a task passed, and never fabricates a correction when no provider
  answers.

Rules that keep it honest:

* **Bounded.** At most ``max_attempts`` corrections per task (default 3). The
  caller passes the attempt number it is about to make, read from what it
  already stored, so the ceiling survives a restart.
* **Never fatal.** A provider outage, an empty answer or a malformed one is
  logged and answered with ``None``; the deterministic rework note is used
  instead. A replanner failure must not stop a run.
* **No secret in the prompt or the record.** Every free-text field is passed
  through ``nexus_policy.redact_text`` and truncated.
* **The model proposes; the scheduler disposes.** The correction is delivered
  as text to the next attempt. It cannot create a task, change a budget or
  mark anything verified.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Callable

from .nexus_policy import redact_text

log = logging.getLogger("nexus_replanner")

#: The system prompt. It asks for one thing: an instruction to the next attempt.
SYSTEM = (
    "You are the NEXUS replanner. An autonomous coding agent finished a task "
    "and its result was NOT accepted. You are given the reason, the evidence "
    "the verifier saw, and the agent's own report. Write a short, concrete "
    "corrective instruction for the NEXT attempt: what to check, what to change "
    "and how to prove it. Rules: do not invent facts that are not in what you "
    "are given; do not ask for credentials; never tell it to force-push; keep "
    "the project's existing decisions and constraints. Answer with the "
    "instruction only - no preamble, no markdown fence, at most 1200 characters."
)

#: A correction longer than this is cut; it is delivered as one message.
MAX_NOTE_CHARS = 1500
#: How much of the agent's report and the evidence is shown to the model.
MAX_CONTEXT_CHARS = 6000

_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")


@dataclass(frozen=True)
class Correction:
    """One corrective instruction, and what produced it."""

    note: str
    attempt: int
    reason: str = ""
    provider: str = ""
    model: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"note": self.note, "attempt": self.attempt, "reason": self.reason,
                "provider": self.provider, "model": self.model}


def _clip(text: Any, limit: int) -> str:
    text = redact_text(str(text or ""))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clean(answer: str) -> str:
    """The model's answer as one instruction: no fence, no surrounding space."""
    text = (answer or "").strip()
    text = _FENCE.sub("", text).strip()
    if len(text) > MAX_NOTE_CHARS:
        text = text[: MAX_NOTE_CHARS - 1] + "…"
    return text


class Replanner:
    """Ask a text model for a correction. Pure with respect to the store."""

    def __init__(self, text: Callable[[str, str], str], *,
                 max_attempts: int = 3,
                 max_context_chars: int = MAX_CONTEXT_CHARS) -> None:
        if not callable(text):
            raise TypeError("Replanner needs a callable text(prompt, system) -> str")
        self.text = text
        self.max_attempts = max(1, int(max_attempts))
        self.max_context_chars = max(500, int(max_context_chars))

    # ------------------------------------------------------------- builders
    @classmethod
    def from_cascade(cls, cascade: Any, **options: Any) -> "Replanner":
        """A replanner over a ``ProviderCascade`` (the chat/planner one)."""

        def call(prompt: str, system: str) -> str:
            response = cascade.text(prompt, system)
            data = getattr(response, "data", None) or {}
            return str(data.get("text") or "")

        return cls(call, **options)

    @classmethod
    def from_env(cls, **options: Any) -> "Replanner | None":
        """A replanner over the configured providers, or ``None``.

        ``None`` when no provider has a key: a deployment without one keeps the
        deterministic rework note and never calls a model.
        """
        try:
            from .providers import ProviderCascade

            cascade = ProviderCascade()
            if not cascade.configured():
                return None
            return cls.from_cascade(cascade, **options)
        except Exception:  # noqa: BLE001 - no replanner is a valid answer
            log.warning("could not build a replanner from the environment", exc_info=True)
            return None

    # -------------------------------------------------------------- the call
    def correct(self, task: dict, verdict: Any, report: Any, *,
                attempt: int | None = None) -> Correction | None:
        """Ask for a correction for this failure, or answer ``None``.

        *attempt* is the number of this correction (1-based). The caller reads
        it from what it stored, so the ceiling is durable. When it is omitted,
        it is taken from the task's stored ``auto_corrections``.
        """
        reason = str(getattr(verdict, "reason", "") or "")
        if attempt is None:
            attempt = _stored_attempts(task) + 1
        if attempt < 1 or attempt > self.max_attempts:
            return None
        prompt = self.build_prompt(task, verdict, report, attempt=attempt)
        try:
            answer = self.text(prompt, SYSTEM)
        except Exception as e:  # noqa: BLE001 - a provider outage is not a task failure
            log.warning("replanner provider failed for task %s: %s",
                        task.get("task_key"), type(e).__name__)
            return None
        note = _clean(answer)
        if not note:
            log.info("replanner returned no usable correction for task %s",
                     task.get("task_key"))
            return None
        return Correction(note=note, attempt=attempt, reason=_clip(reason, 400),
                          provider="cascade", model="")

    def build_prompt(self, task: dict, verdict: Any, report: Any, *,
                     attempt: int) -> str:
        """The exact text the model is shown. Kept public so a test can pin it."""
        evidence = getattr(verdict, "evidence", {}) or {}
        lines = [
            f"Task: {_clip(task.get('task_key') or task.get('id') or 'unknown', 200)}",
            f"Repository: {_clip(task.get('repository') or 'none', 200)}",
            f"Branch: {_clip(task.get('branch') or 'default', 200)}",
            f"Correction {attempt} of {self.max_attempts}.",
            "",
            f"The result was not accepted: {_clip(getattr(verdict, 'verdict', ''), 80)}",
            f"Reason: {_clip(getattr(verdict, 'reason', ''), 1000)}",
        ]
        if evidence:
            shown = {k: _clip(v, 600) for k, v in list(evidence.items())[:12]
                     if k in ("remote_sha", "named_sha", "branch", "state",
                              "self_qa", "conversation_id", "reason")}
            if shown:
                lines += ["", "Evidence the verifier saw:"]
                lines += [f"- {k}: {v}" for k, v in shown.items()]
        report_text = _clip(getattr(report, "final_text", ""), self.max_context_chars // 2)
        if report_text:
            lines += ["", "The agent's own report:", report_text]
        last_error = _clip(task.get("last_error") or "", 1500)
        if last_error:
            lines += ["", "The last recorded error:", last_error]
        previous = _clip((task.get("verification") or {}).get("auto_correction", {}).get("note", "")
                         if isinstance(task.get("verification"), dict) else "", 800)
        if previous:
            lines += ["", "A previous correction was already given:", previous]
        lines += ["", "Write the corrective instruction for the next attempt now."]
        return "\n".join(lines)[: self.max_context_chars + 2000]


def _stored_attempts(task: dict) -> int:
    verification = task.get("verification") or {}
    if isinstance(verification, str):
        try:
            import json
            verification = json.loads(verification)
        except Exception:  # noqa: BLE001 - a malformed column is "no attempt yet"
            return 0
    if not isinstance(verification, dict):
        return 0
    try:
        return max(0, int(verification.get("auto_corrections") or 0))
    except (TypeError, ValueError):
        return 0


def enabled() -> bool:
    """Is the replanner switched on for this process?"""
    return os.environ.get("NEXUS_REPLANNER", "").strip().lower() in (
        "1", "true", "yes", "on")


def from_env_if_enabled(**options: Any) -> "Replanner | None":
    """The replanner for a worker: only when ``NEXUS_REPLANNER=1`` and a key exists."""
    if not enabled():
        return None
    return Replanner.from_env(**options)
