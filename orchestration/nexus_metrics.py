"""Prometheus metrics for the NEXUS control plane.

A small, dependency-free registry that renders the Prometheus text exposition
format (``text/plain; version=0.0.4``). It exists because the operators need to
see queue depth, claim rate, evidence verdicts, OpenHands latency and database
pool pressure without adding a client library to a deliberately small runtime
dependency set.

Rules that keep it safe to call from a hot path:

* **Instrumentation never raises.** Every helper swallows its own errors: a
  metrics bug must not fail the request or the dispatch it was observing.
* **Cardinality is bounded.** The HTTP helper normalises a path (UUIDs and
  numbers become ``{id}`` / ``{n}``) so a user-supplied id cannot create an
  unbounded number of series.
* **No secret is ever a label.** Labels are worker ids, verdicts, transports
  and normalised paths - never a key, a token or a DSN.

The registry is process-local. A deployment with several processes scrapes
each one; Prometheus sums them.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Iterable

#: Latency buckets in seconds. They cover a fast database read (5 ms) through a
#: slow provider call (10 s), which is the range NEXUS actually sees.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)

_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_DIGITS = re.compile(r"\b\d+\b")


def _escape(value: Any) -> str:
    """Escape a label value for the exposition format."""
    text = str(value)
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt(value: float) -> str:
    """Format a sample value without exponent surprises for integers."""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


class _Metric:
    __slots__ = ("name", "help", "labelnames", "_lock", "_series")

    kind = "untyped"

    def __init__(self, name: str, help_text: str, labelnames: Iterable[str] = ()):
        if not _METRIC_NAME.match(name):
            raise ValueError(f"invalid metric name: {name!r}")
        labelnames = tuple(labelnames)
        for label in labelnames:
            if not _LABEL_NAME.match(label):
                raise ValueError(f"invalid label name: {label!r}")
        self.name = name
        self.help = help_text
        self.labelnames = labelnames
        # Re-entrant: an ``inc``/``observe`` takes the lock and then looks the
        # series up through ``_state``, which takes it again.
        self._lock = threading.RLock()
        self._series: dict[tuple[str, ...], Any] = {}

    def _key(self, labels: dict[str, Any]) -> tuple[str, ...]:
        if set(labels) != set(self.labelnames):
            raise ValueError(
                f"{self.name}: expected labels {self.labelnames}, got {tuple(labels)}")
        return tuple(str(labels[name]) for name in self.labelnames)

    def _state(self, labels: dict[str, Any]) -> Any:
        key = self._key(labels)
        with self._lock:
            if key not in self._series:
                self._series[key] = self._new_state()
            return self._series[key]

    def _new_state(self) -> Any:  # pragma: no cover - overridden
        return None

    def _render_series(self) -> list[str]:  # pragma: no cover - overridden
        return []

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}",
                 f"# TYPE {self.name} {self.kind}"]
        with self._lock:
            keys = sorted(self._series)
        for key in keys:
            with self._lock:
                state = self._series[key]
            lines.extend(self._render_series_key(key, state))
        return lines

    def _render_series_key(self, key: tuple[str, ...], state: Any) -> list[str]:
        return []

    def _label_text(self, key: tuple[str, ...], extra: str = "") -> str:
        parts = [f'{name}="{_escape(value)}"'
                 for name, value in zip(self.labelnames, key)]
        if extra:
            parts.append(extra)
        return "{" + ",".join(parts) + "}" if parts else ""


class Counter(_Metric):
    """A monotonically increasing value."""

    kind = "counter"

    def _new_state(self) -> list[float]:
        return [0.0]

    def inc(self, amount: float = 1.0, **labels: Any) -> None:
        if amount < 0:
            raise ValueError("a counter cannot decrease")
        with self._lock:
            state = self._state(labels)
            state[0] += amount

    def value(self, **labels: Any) -> float:
        return self._state(labels)[0]

    def _render_series_key(self, key, state) -> list[str]:
        return [f"{self.name}{self._label_text(key)} {_fmt(state[0])}"]


class Gauge(_Metric):
    """A value that can go up and down."""

    kind = "gauge"

    def _new_state(self) -> list[float]:
        return [0.0]

    def set(self, value: float, **labels: Any) -> None:
        with self._lock:
            self._state(labels)[0] = float(value)

    def inc(self, amount: float = 1.0, **labels: Any) -> None:
        with self._lock:
            self._state(labels)[0] += float(amount)

    def dec(self, amount: float = 1.0, **labels: Any) -> None:
        self.inc(-amount, **labels)

    def value(self, **labels: Any) -> float:
        return self._state(labels)[0]

    def _render_series_key(self, key, state) -> list[str]:
        return [f"{self.name}{self._label_text(key)} {_fmt(state[0])}"]


class Histogram(_Metric):
    """Cumulative bucket counts plus a sum and a count."""

    kind = "histogram"

    def __init__(self, name: str, help_text: str, labelnames: Iterable[str] = (),
                 buckets: Iterable[float] = DEFAULT_BUCKETS):
        super().__init__(name, help_text, labelnames)
        self.buckets = tuple(sorted(float(b) for b in buckets))

    def _new_state(self) -> dict[str, Any]:
        return {"counts": [0] * len(self.buckets), "sum": 0.0, "count": 0}

    def observe(self, value: float, **labels: Any) -> None:
        value = float(value)
        with self._lock:
            state = self._state(labels)
            for index, bound in enumerate(self.buckets):
                if value <= bound:
                    state["counts"][index] += 1
            state["sum"] += value
            state["count"] += 1

    def _render_series_key(self, key, state) -> list[str]:
        lines = []
        # ``observe`` increments every bound at or above the value, so each
        # bucket already holds its cumulative count - do not accumulate again.
        for index, bound in enumerate(self.buckets):
            label = self._label_text(key, 'le="' + _fmt(bound) + '"')
            lines.append(f"{self.name}_bucket{label} {state['counts'][index]}")
        infinite = self._label_text(key, 'le="+Inf"')
        lines.append(f"{self.name}_bucket{infinite} {state['count']}")
        lines.append(f"{self.name}_sum{self._label_text(key)} {_fmt(state['sum'])}")
        lines.append(f"{self.name}_count{self._label_text(key)} {state['count']}")
        return lines


class Registry:
    """A named collection of metrics, rendered together."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: dict[str, _Metric] = {}

    def _add(self, metric: _Metric) -> _Metric:
        with self._lock:
            existing = self._metrics.get(metric.name)
            if existing is not None:
                return existing
            self._metrics[metric.name] = metric
            return metric

    def counter(self, name: str, help_text: str,
                labelnames: Iterable[str] = ()) -> Counter:
        return self._add(Counter(name, help_text, labelnames))  # type: ignore[return-value]

    def gauge(self, name: str, help_text: str,
              labelnames: Iterable[str] = ()) -> Gauge:
        return self._add(Gauge(name, help_text, labelnames))  # type: ignore[return-value]

    def histogram(self, name: str, help_text: str,
                  labelnames: Iterable[str] = (),
                  buckets: Iterable[float] = DEFAULT_BUCKETS) -> Histogram:
        return self._add(Histogram(name, help_text, labelnames, buckets))  # type: ignore[return-value]

    def render(self) -> str:
        with self._lock:
            metrics = [self._metrics[name] for name in sorted(self._metrics)]
        lines: list[str] = []
        for metric in metrics:
            lines.extend(metric.render())
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """Drop every series. For tests only."""
        with self._lock:
            for metric in self._metrics.values():
                with metric._lock:
                    metric._series.clear()


#: The process-wide registry. A test resets it; production never does.
REGISTRY = Registry()

# --- the metrics the requirement names -------------------------------------
TASKS_CLAIMED = REGISTRY.counter(
    "nexus_tasks_claimed_total",
    "Tasks claimed by a lab worker.", ("worker",))
EVIDENCE_VERIFICATIONS = REGISTRY.counter(
    "nexus_evidence_verifications_total",
    "Evidence records by verification verdict.", ("verdict",))
OPENHANDS_LATENCY = REGISTRY.histogram(
    "nexus_openhands_api_latency_seconds",
    "Latency of an OpenHands HTTP request.", ("account", "transport"))
DB_POOL_ACTIVE = REGISTRY.gauge(
    "nexus_db_pool_active_connections",
    "Database pool connections currently in use.")
DB_POOL_IDLE = REGISTRY.gauge(
    "nexus_db_pool_idle_connections",
    "Database pool connections currently idle.")
DB_POOL_WAITING = REGISTRY.gauge(
    "nexus_db_pool_waiting_requests",
    "Callers queued for a database connection.")
DB_POOL_MAX = REGISTRY.gauge(
    "nexus_db_pool_max_connections",
    "Configured maximum database pool size.")
HTTP_REQUESTS = REGISTRY.counter(
    "nexus_http_requests_total",
    "HTTP responses by method, normalised path and status.", ("method", "path", "status"))
HTTP_LATENCY = REGISTRY.histogram(
    "nexus_http_request_duration_seconds",
    "HTTP request duration by method and normalised path.", ("method", "path"))
NOTIFICATIONS_SENT = REGISTRY.counter(
    "nexus_notifications_sent_total",
    "PostgreSQL NOTIFY messages published.", ("reason",))
NOTIFICATIONS_RECEIVED = REGISTRY.counter(
    "nexus_notifications_received_total",
    "PostgreSQL NOTIFY messages received by a listener.", ("kind",))
QUEUE_DEPTH = REGISTRY.gauge(
    "nexus_queue_depth", "Tasks that are due and not claimed right now.")
QUEUE_OLDEST = REGISTRY.gauge(
    "nexus_queue_oldest_seconds", "How long the oldest claimable task has waited.")
STUCK_TASKS = REGISTRY.gauge(
    "nexus_stuck_tasks", "Claimable tasks that have waited longer than the stuck threshold.")
LIVE_WORKERS = REGISTRY.gauge(
    "nexus_live_workers", "Workers with a fresh heartbeat.")
ACTIVE_RUNS = REGISTRY.gauge(
    "nexus_active_runs", "Runs in an active state.")


def _safe(action: Callable[[], None]) -> None:
    try:
        action()
    except Exception:  # noqa: BLE001 - a metric must never break its caller
        pass


def normalize_path(path: str) -> str:
    """Collapse ids and numbers so a path is a bounded label."""
    path = _UUID.sub("{id}", path or "/")
    path = _DIGITS.sub("{n}", path)
    return path or "/"


def task_claimed(worker: str) -> None:
    _safe(lambda: TASKS_CLAIMED.inc(worker=worker))


def evidence_verification(verdict: str) -> None:
    _safe(lambda: EVIDENCE_VERIFICATIONS.inc(verdict=verdict))


def observe_openhands(account: str | None, transport: str, seconds: float) -> None:
    label = (account or "default")
    _safe(lambda: OPENHANDS_LATENCY.observe(seconds, account=label, transport=transport))


def observe_http(method: str, path: str, status: int, seconds: float) -> None:
    normalised = normalize_path(path)
    _safe(lambda: HTTP_REQUESTS.inc(method=method, path=normalised, status=str(status)))
    _safe(lambda: HTTP_LATENCY.observe(seconds, method=method, path=normalised))


def set_db_pool(stats: dict[str, Any]) -> None:
    """Publish a ``PostgresStore.pool_stats()`` snapshot. Counts only."""
    if not stats or not stats.get("available"):
        return
    size = stats.get("size") or 0
    idle = stats.get("idle") or 0
    _safe(lambda: DB_POOL_ACTIVE.set(max(0, size - idle)))
    _safe(lambda: DB_POOL_IDLE.set(idle))
    _safe(lambda: DB_POOL_WAITING.set(stats.get("waiting") or 0))
    _safe(lambda: DB_POOL_MAX.set(stats.get("max") or 0))


def notification_sent(reason: str) -> None:
    _safe(lambda: NOTIFICATIONS_SENT.inc(reason=reason))


def notification_received(kind: str) -> None:
    _safe(lambda: NOTIFICATIONS_RECEIVED.inc(kind=kind))


def set_queue(queue: dict[str, Any] | None, stuck: int, workers: int) -> None:
    """Publish the queue/worker snapshot the observability endpoint already has."""
    queue = queue or {}
    _safe(lambda: QUEUE_DEPTH.set(queue.get("claimable_now") or 0))
    _safe(lambda: QUEUE_OLDEST.set(queue.get("oldest_claimable_seconds") or 0))
    _safe(lambda: ACTIVE_RUNS.set(queue.get("active_runs") or 0))
    _safe(lambda: STUCK_TASKS.set(max(0, int(stuck))))
    _safe(lambda: LIVE_WORKERS.set(max(0, int(workers))))


def render() -> str:
    """The full registry in the Prometheus exposition format."""
    try:
        return REGISTRY.render()
    except Exception:  # noqa: BLE001 - a scrape must not 500
        return "# nexus metrics unavailable\n"


class Timer:
    """``with Timer() as t:`` - a monotonic elapsed measurement."""

    __slots__ = ("started", "elapsed")

    def __enter__(self) -> "Timer":
        self.started = time.perf_counter()
        self.elapsed = 0.0
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.elapsed = time.perf_counter() - self.started
        return False
