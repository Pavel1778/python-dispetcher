"""Stage timing for the whole execution path, as structured records.

The path a request takes - project → conversation → planner → task
decomposition → queue → dispatcher → OpenHands runtime → workspace → agent →
verification → completion → notification - used to leave no per-stage timing
anywhere, so "NEXUS is slow" could not be answered with a number. This module
is the one place a stage is timed, and the one shape a record has.

A **span** is one stage of one attempt. It carries exactly the fields the
operation needs to be read back:

    started_at, finished_at, duration_ms, execution_id, project_id, task_id,
    conversation_id, attempt_id, status, error

Nothing here writes a secret: an error is a type name and a bounded message.
Only the type name and a bounded text are kept; no environment value, key or
token is read.

Two ways to record a span:

* :func:`span` - a context manager, for code that has the ids to hand;
* :meth:`Tracer.record` - a plain call, for code that computes the duration
  itself (a stage measured around a query, say).

A tracer that is not collecting (the default, and every test that does not ask
for one) does nothing: :func:`span` is a no-op context manager and
``record`` returns immediately. That keeps the instrumentation free on the hot
path and impossible to make a test flaky.

The records are kept in memory (a bounded ring) and can be read with
:meth:`Tracer.recent` / :meth:`Tracer.summary`; the worker and the web server
expose a summary at ``GET /api/observability``. A production deployment that
wants them durably can set ``NEXUS_TRACE_LOG=1`` to also emit one JSON line per
span to the ``nexus.trace`` logger, which the deployment's log pipeline picks
up.
"""
from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger("nexus.trace")

#: How many recent spans a tracer keeps for the summary endpoint.
DEFAULT_CAPACITY = 2000


def _enabled_from_env() -> bool:
    raw = os.environ.get("NEXUS_TRACE", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _log_lines_from_env() -> bool:
    raw = os.environ.get("NEXUS_TRACE_LOG", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _safe_error(error: BaseException | str | None) -> str:
    """A short, secret-free description of a failure: its type and message.

    Only the type name and a bounded text are kept here; no environment value,
    key or token is read. The message is expected to be scrubbed of
    credential-looking values by ``nexus_policy.install_log_redaction`` when a
    log line is emitted.
    """
    if error is None:
        return ""
    if isinstance(error, str):
        return error[:300]
    return f"{type(error).__name__}: {error}"[:300]


@dataclass
class Span:
    """One timed stage of one attempt."""

    stage: str
    started_at: float
    finished_at: float = 0.0
    execution_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    conversation_id: str | None = None
    attempt_id: str | None = None
    status: str = "ok"
    error: str = ""

    @property
    def duration_ms(self) -> float:
        end = self.finished_at or self.started_at
        return round((end - self.started_at) * 1000.0, 2)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["duration_ms"] = self.duration_ms
        return out


class Tracer:
    """Collects spans. A disabled tracer records nothing."""

    def __init__(self, *, enabled: bool | None = None, capacity: int = DEFAULT_CAPACITY,
                 log_lines: bool | None = None):
        self.enabled = _enabled_from_env() if enabled is None else bool(enabled)
        self.log_lines = _log_lines_from_env() if log_lines is None else bool(log_lines)
        self._spans: deque[Span] = deque(maxlen=max(1, capacity))
        self._lock = threading.Lock()

    def record(self, span: Span) -> Span:
        if not self.enabled:
            return span
        with self._lock:
            self._spans.append(span)
        if self.log_lines:
            log.info("span %s", span.as_dict())
        return span

    @contextlib.contextmanager
    def span(self, stage: str, **ids: Any):
        """Time one stage. ``ids`` are the project/task/conversation/attempt
        ids, and ``execution_id`` names the attempt."""
        if not self.enabled:
            yield None
            return
        span = Span(stage=stage, started_at=time.time(),
                    execution_id=_opt(ids.get("execution_id")),
                    project_id=_opt(ids.get("project_id")),
                    task_id=_opt(ids.get("task_id")),
                    conversation_id=_opt(ids.get("conversation_id")),
                    attempt_id=_opt(ids.get("attempt_id")))
        try:
            yield span
        except BaseException as e:  # noqa: BLE001 - recorded, then re-raised
            span.status = "error"
            span.error = _safe_error(e)
            span.finished_at = time.time()
            self.record(span)
            raise
        else:
            span.finished_at = time.time()
            self.record(span)

    def recent(self, *, limit: int = 200, stage: str | None = None) -> list[dict]:
        with self._lock:
            spans = list(self._spans)
        if stage:
            spans = [s for s in spans if s.stage == stage]
        return [s.as_dict() for s in spans[-max(1, limit):]]

    def summary(self) -> dict[str, Any]:
        """Per-stage count and latency (p50/p95/max) over the recent spans."""
        with self._lock:
            spans = list(self._spans)
        by_stage: dict[str, list[float]] = {}
        for span in spans:
            by_stage.setdefault(span.stage, []).append(span.duration_ms)
        out: dict[str, Any] = {"enabled": self.enabled, "spans": len(spans), "stages": {}}
        for stage, durations in by_stage.items():
            durations.sort()
            out["stages"][stage] = {
                "count": len(durations),
                "p50_ms": _percentile(durations, 50),
                "p95_ms": _percentile(durations, 95),
                "max_ms": round(durations[-1], 2),
                "errors": sum(1 for s in spans if s.stage == stage and s.status == "error"),
            }
        return out

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()


def _opt(value: Any) -> str | None:
    return None if value is None else str(value)


def _percentile(sorted_values: list[float], pct: int) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return round(sorted_values[0], 2)
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    frac = rank - low
    value = sorted_values[low] * (1 - frac) + sorted_values[high] * frac
    return round(value, 2)


#: The process-wide tracer. Off unless ``NEXUS_TRACE`` says otherwise; a test
#: or the web server may turn it on with ``tracing.tracer.enabled = True``.
tracer = Tracer()


def span(stage: str, **ids: Any):
    """Time *stage* on the process-wide tracer (a no-op when it is off)."""
    return tracer.span(stage, **ids)


@dataclass
class SpanIds:
    """The ids a stage is recorded with, in one object, for callers that carry
    them together (the lab scheduler's per-attempt context)."""

    project_id: str | None = None
    task_id: str | None = None
    conversation_id: str | None = None
    execution_id: str | None = None
    attempt_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_kwargs(self) -> dict[str, Any]:
        return {"project_id": self.project_id, "task_id": self.task_id,
                "conversation_id": self.conversation_id,
                "execution_id": self.execution_id, "attempt_id": self.attempt_id,
                **self.extra}
