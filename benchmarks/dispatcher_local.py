#!/usr/bin/env python3
"""Local, dependency-free benchmark for the dispatcher control-plane scan.

The dispatcher ticks on a SQLite-backed ``ControlPlane``. Every tick,
``_advance_active`` must find the handful of *live* tasks among potentially
thousands of finished ones. This benchmark proves the cost of that scan scales
with the number of *active* tasks, not the total backlog, by comparing the two
control-plane query strategies:

* legacy  -- ``list_tasks()``            (full table scan, then filter in Python)
* indexed -- ``list_tasks(states=[...])`` (``idx_tasks_state`` seeks only live rows)

It also runs real dispatcher ticks over a large completed backlog so the number
is end-to-end, not a micro-benchmark. No PostgreSQL, no network, no OpenHands
Cloud: the runtime client is an in-process stand-in.

Usage:
  python benchmarks/dispatcher_local.py --backlog 20000 --active 50 --ticks 200
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orchestration.control_plane import ControlPlane, TaskState  # noqa: E402
from orchestration.dispatcher import Dispatcher  # noqa: E402
from orchestration.openhands_client import (  # noqa: E402
    ConversationView,
    Outcome,
    SendOutcome,
)


ACTIVE_STATES = (
    TaskState.ASSIGNED,
    TaskState.RUNNING,
    TaskState.VERIFYING,
    TaskState.RECOVERING,
)


class _InstantClient:
    """In-process stand-in for the OpenHands agent-server.

    Conversations report RUNNING forever (the steady state a live dispatcher
    holds between LLM turns), so ticks keep renewing leases instead of
    completing work — exactly the regime where the per-tick scan dominates.
    """

    def agent_settings(self) -> dict:
        return {}

    def create_conversation(self, *a, **k):
        return ConversationView(conversation_id="x", outcome=Outcome.RUNNING, raw_status="running"), True

    def send_task(self, *a, **k) -> SendOutcome:
        return SendOutcome(started=True)

    def start(self, *a, **k) -> SendOutcome:
        return SendOutcome(started=True)

    def get_conversation(self, *a, **k) -> ConversationView:
        return ConversationView(conversation_id="x", outcome=Outcome.RUNNING, raw_status="running")

    def final_response(self, *a, **k):
        return None

    def failure_classification(self, *a, **k):
        return None


def _mark_completed(cp: ControlPlane, task_id: str) -> None:
    cp.transition(task_id, TaskState.ASSIGNED)
    cp.transition(task_id, TaskState.RUNNING)
    cp.transition(task_id, TaskState.VERIFYING)
    cp.set_result(task_id, "done")
    cp.transition(task_id, TaskState.COMPLETED, result="done")


def _seed(cp: ControlPlane, *, backlog: int, active: int, queued: int) -> None:
    for i in range(backlog):
        t = cp.submit_task(f"done-{i}", "historical task", max_attempts=1)
        _mark_completed(cp, t.id)
    for i in range(active):
        t = cp.submit_task(f"run-{i}", "live task", max_attempts=3)
        cp.claim_task(t.id, "w1", lease_seconds=3600)
        cp.bind_conversation(t.id, f"conversation-{i}")
        cp.transition(t.id, TaskState.RUNNING, owner="w1")
    for i in range(queued):
        cp.submit_task(f"q-{i}", "pending task", max_attempts=3)


def _pct(samples: list[float]) -> dict[str, float]:
    s = sorted(samples)
    n = len(s)

    def at(p: float) -> float:
        if n == 0:
            return 0.0
        idx = min(n - 1, int(round(p * (n - 1))))
        return s[idx]

    return {
        "p50": at(0.50),
        "p95": at(0.95),
        "mean": sum(s) / n if n else 0.0,
    }


def _bench_scan(cp: ControlPlane, *, iters: int) -> dict[str, dict[str, float]]:
    # Warm up the page cache / prepared statements.
    for _ in range(min(20, iters)):
        list(cp.list_tasks())
        list(cp.list_tasks(states=list(ACTIVE_STATES)))

    legacy: list[float] = []
    indexed: list[float] = []
    for _ in range(iters):
        t0 = time.perf_counter()
        list(cp.list_tasks())
        legacy.append(time.perf_counter() - t0)

        t0 = time.perf_counter()
        list(cp.list_tasks(states=list(ACTIVE_STATES)))
        indexed.append(time.perf_counter() - t0)

    return {"legacy_scan": _pct(legacy), "indexed_filter": _pct(indexed)}


def _bench_ticks(cp: ControlPlane, *, ticks: int) -> dict[str, float]:
    d = Dispatcher(cp, _InstantClient(), worker_id="w1", workspace_root="/tmp/ws",
                   desired_state_path=None)
    samples: list[float] = []
    for _ in range(ticks):
        t0 = time.perf_counter()
        d.tick()
        samples.append(time.perf_counter() - t0)
    return _pct(samples)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backlog", type=int, default=20000)
    ap.add_argument("--active", type=int, default=50)
    ap.add_argument("--queued", type=int, default=10)
    ap.add_argument("--iters", type=int, default=200, help="scan iterations")
    ap.add_argument("--ticks", type=int, default=100, help="dispatcher ticks")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as td:
        cp = ControlPlane(Path(td) / "cp.db")
        t0 = time.perf_counter()
        _seed(cp, backlog=args.backlog, active=args.active, queued=args.queued)
        seed_s = time.perf_counter() - t0

        scan = _bench_scan(cp, iters=args.iters)
        tick = _bench_ticks(cp, ticks=args.ticks)

    legacy_ms = scan["legacy_scan"]["p50"] * 1000
    indexed_ms = scan["indexed_filter"]["p50"] * 1000
    speedup = (scan["legacy_scan"]["p50"] / scan["indexed_filter"]["p50"]) if scan["indexed_filter"]["p50"] else 0

    print(f"backlog={args.backlog} active={args.active} queued={args.queued} "
          f"(seed {seed_s*1000:.1f} ms)")
    print("control-plane scan (p50):")
    print(f"  legacy  list_tasks()             : {legacy_ms:8.3f} ms")
    print(f"  indexed list_tasks(states=[...]) : {indexed_ms:8.3f} ms")
    if speedup:
        print(f"  speedup                          : {speedup:8.2f}x")
    print("dispatcher tick (full, p50/p95/mean):")
    print(f"  {tick['p50']*1000:8.3f} ms / {tick['p95']*1000:8.3f} ms / {tick['mean']*1000:8.3f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
