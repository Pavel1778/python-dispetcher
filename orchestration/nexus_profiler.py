"""Run-time profile: where a run's wall-clock time actually went.

A run's life is a handful of stages (queue, dispatch, OpenHands start, the
agent working, verification, gaps between tries). The store records one
timestamp per milestone on the run row (migration ``0040``); this module turns
those timestamps into durations and a bottleneck classification. It is **pure**:
it reads a row-like mapping and returns numbers - no database, no clock except
the ``now`` a caller passes, so the same code serves the API, the studio and the
tests.

Rules that keep the numbers honest:

* a stage whose start (or end) is missing is ``None``, never ``0`` - "not
  measured" is not "took no time";
* the milestones are written once at the real lifecycle point, so a retry or a
  re-claim does not move them; the profile of a run therefore never rewrites
  history;
* ``idle`` is wall-clock minus the sum of the known stages, floored at zero:
  time nobody accounted for is shown as idle, not hidden inside a stage.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

#: The milestone columns, in the order they happen. Each is a run column name.
MILESTONES = (
    "queued_at",
    "claimed_at",
    "dispatch_started_at",
    "executor_started_at",
    "conversation_ready_at",
    "prompt_sent_at",
    "first_agent_event_at",
    "verification_started_at",
    "verification_completed_at",
    "completed_at",
)

#: Named stages as ``(label, start_milestone, end_milestone)``. The label is
#: what the studio and the API show; the pair is the span measured.
STAGES = (
    ("queue_wait", "queued_at", "claimed_at"),
    ("dispatch", "claimed_at", "dispatch_started_at"),
    ("openhands_start", "dispatch_started_at", "conversation_ready_at"),
    ("prompt", "conversation_ready_at", "prompt_sent_at"),
    ("first_event", "prompt_sent_at", "first_agent_event_at"),
    ("execution", "first_agent_event_at", "verification_started_at"),
    ("verification", "verification_started_at", "verification_completed_at"),
    ("finish", "verification_completed_at", "completed_at"),
)

#: Which stage a dominant cost is attributed to, for a one-line reason.
_BOTTLENECK_BY_STAGE = {
    "queue_wait": "queue_wait",
    "dispatch": "dispatch",
    "openhands_start": "openhands_start",
    "prompt": "openhands_start",
    "first_event": "agent_first_event",
    "execution": "agent_execution",
    "verification": "verification",
    "finish": "finish",
    "idle": "idle",
}


def _as_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _seconds(a: Any, b: Any) -> float | None:
    """``(b - a)`` in seconds, or ``None`` when either end is missing."""
    start, end = _as_dt(a), _as_dt(b)
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def profile_run(run: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """The stages of one run, in seconds, and its dominant bottleneck.

    *run* is a mapping with the ``MILESTONES`` keys (a store row, or a test
    dict). *now* ends an in-flight run; a terminal run uses its own
    ``completed_at``/``updated_at``.
    """
    end = _as_dt(run.get("completed_at")) or _as_dt(run.get("updated_at")) or now
    stages: dict[str, float | None] = {}
    for label, start_key, end_key in STAGES:
        # The last stage of a live run ends at "now"; an earlier stage whose end
        # milestone is unset is genuinely not measured yet.
        if label == "finish" and end_key == "completed_at" and run.get(end_key) is None:
            stages[label] = None
            continue
        stages[label] = _seconds(run.get(start_key), run.get(end_key))

    total = _seconds(run.get("queued_at") or run.get("created_at"), end)
    measured = sum(v for v in stages.values() if v is not None)
    idle = None if total is None else max(0.0, total - measured)

    ranked = {k: v for k, v in stages.items() if v is not None}
    ranked["idle"] = idle
    dominant = None
    if ranked:
        top = max(ranked, key=lambda k: ranked[k] if ranked[k] is not None else -1)
        if ranked.get(top) is not None:
            dominant = _BOTTLENECK_BY_STAGE.get(top, top)

    return {
        "total_seconds": total,
        "stages": stages,
        "idle_seconds": idle,
        "bottleneck": dominant,
        "measured_seconds": measured,
        "started_at": run.get("queued_at") or run.get("created_at"),
        "ended_at": end,
    }


def idle_ratio(profile: dict[str, Any]) -> float | None:
    """The fraction of wall-clock time that is idle, or ``None`` if unknown."""
    total = profile.get("total_seconds")
    idle = profile.get("idle_seconds")
    if not total or idle is None:
        return None
    return round(idle / total, 3)
