#!/usr/bin/env python3
"""Scheduler / Execution Engine benchmark against the REAL LabStore.

Uses the real orchestration stores on a real PostgreSQL 17. It measures the
LOCAL control-plane cost only (no OpenHands Cloud, no LLM): run creation,
atomic task claim, state transitions, reclaim, cleanup queueing, and the
throughput of concurrent claims.

Isolated research program: it drives the real store classes but changes nothing
in them.

Usage:
  TEST_SUPABASE_DB_URL=postgresql://... python benchmarks/scheduler_bench.py --out sched.json
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_studio_api import _reset_schema  # noqa: E402
from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402
from orchestration.nexus_lab_store import LabStore  # noqa: E402

PRINCIPAL = "bench-principal"


def _pct(samples: list[float]) -> dict:
    s = sorted(samples)
    def at(p):
        if not s:
            return 0.0
        return round(s[min(len(s) - 1, int(round(p * (len(s) - 1))))], 3)
    return {"n": len(s), "p50": at(.5), "p90": at(.9), "p95": at(.95), "p99": at(.99),
            "mean": round(statistics.fmean(s), 3) if s else 0.0,
            "max": round(max(s), 3) if s else 0.0}


def _plan(n: int, deps: bool = False) -> list[dict]:
    tasks = []
    for i in range(n):
        t = {"id": f"t{i}", "title": f"Задача {i}", "description": "d",
             "acceptance": ["ok"], "dependencies": ([f"t{i-1}"] if deps and i else [])}
        tasks.append(t)
    return tasks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--repeat", type=int, default=50)
    args = ap.parse_args()
    if not 1 <= args.repeat <= 500:
        ap.error("--repeat must be between 1 and 500 (run iteration limit)")

    dsn = os.environ.get("TEST_SUPABASE_DB_URL")
    if not dsn:
        print("set TEST_SUPABASE_DB_URL", file=sys.stderr)
        return 2
    os.environ.setdefault("NEXUS_LAB_MAX_ACTIVE_TASKS", "0")
    _reset_schema()
    store = PostgresStore(dsn)
    projects = ProjectService(store)
    lab = LabStore(store)

    project = projects.create(PRINCIPAL, "Bench", "")["id"]

    def start(n):
        with store._tx() as cur:
            cur.execute("INSERT INTO nexus_goals(project_id, text, created_by, state) "
                        "VALUES (%s,%s,%s,'draft') RETURNING id", (project, "g", PRINCIPAL))
            goal = cur.fetchone()["id"]
        return lab.start_run(PRINCIPAL, project, goal, plan=_plan(n), max_iterations=n,
                             repository="", branch="")

    def times(operation):
        samples = []
        for _ in range(args.repeat):
            t0 = time.perf_counter()
            operation()
            samples.append((time.perf_counter() - t0) * 1000)
        return _pct(samples)

    out = {"environment": {"python": sys.version.split()[0], "repeat": args.repeat}, "measurements": {}}
    m = out["measurements"]
    m["start_run_40_tasks_ms"] = times(lambda: start(40))
    run = start(max(200, args.repeat))

    def claim():
        if lab.claim_task(run["id"], "w1") is None:
            raise RuntimeError("expected a successful claim, not an empty poll")

    m["claim_task_ms"] = times(claim)

    # A fresh 500-task run per level. Count successful distinct IDs, not poll
    # attempts, and propagate thread errors instead of timing a partial queue.
    def drain(run_id, owner):
        ids = []
        while (task := lab.claim_task(run_id, owner)) is not None:
            ids.append(task["id"])
        return ids

    conc = {}
    for workers in (1, 5, 10, 25):
        run_id = start(500)["id"]
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            jobs = [pool.submit(drain, run_id, f"w-{i}") for i in range(workers)]
            ids = [task_id for job in jobs for task_id in job.result()]
        wall = time.perf_counter() - t0
        if len(ids) != 500 or len(set(ids)) != 500:
            raise RuntimeError(f"{workers} workers: expected 500 distinct claims, "
                               f"got {len(ids)} claims of {len(set(ids))} tasks")
        conc[str(workers)] = {"workers": workers, "claimed": len(ids),
                              "wall_s": round(wall, 4), "claims_per_s": round(len(ids) / wall, 1)}
    m["concurrent_claim"] = conc
    empty = start(500)
    m["reclaim_expired_scan_ms"] = times(lambda: lab.reclaim_expired(empty["id"]))
    m["queue_cleanups_ms"] = times(lambda: lab.queue_cleanups(retention_seconds=0))
    m["dashboard_counts_ms"] = times(store.dashboard_counts)

    store.close()
    payload = json.dumps(out, indent=2, ensure_ascii=False)
    print(payload)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
