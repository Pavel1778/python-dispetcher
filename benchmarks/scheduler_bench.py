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
import json
import os
import statistics
import sys
import threading
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

    dsn = os.environ.get("TEST_SUPABASE_DB_URL")
    if not dsn:
        print("set TEST_SUPABASE_DB_URL", file=sys.stderr)
        return 2
    os.environ.setdefault("NEXUS_LAB_MAX_ACTIVE_TASKS", "0")
    _reset_schema()
    store = PostgresStore(dsn)
    projects = ProjectService(store)
    lab = LabStore(store)

    # Seed a project, a goal, and a run with 40 independent tasks.
    project = projects.create(PRINCIPAL, "Bench", "")["id"]
    with store._tx() as cur:
        cur.execute("INSERT INTO nexus_goals(project_id, text, created_by, state) "
                    "VALUES (%s,%s,%s,'draft') RETURNING id",
                    (project, "goal", PRINCIPAL))
        cur.fetchone()

    out: dict = {"environment": {"python": sys.version.split()[0]}, "measurements": {}}
    m = out["measurements"]

    # 1. run creation (with a 40-task plan)
    def start():
        with store._tx() as cur:
            cur.execute("INSERT INTO nexus_goals(project_id, text, created_by, state) "
                        "VALUES (%s,%s,%s,'draft') RETURNING id", (project, "g", PRINCIPAL))
            g = cur.fetchone()["id"]
        lab.start_run(PRINCIPAL, project, g, plan=_plan(40), repository="", branch="")
    m["start_run_40_tasks_ms"] = _pct([(lambda t0: (start(), (time.perf_counter()-t0)*1000)[1])(time.perf_counter()) for _ in range(10)])

    # 2. atomic claim of one task (fresh run, many tasks)
    with store._tx() as cur:
        cur.execute("INSERT INTO nexus_goals(project_id, text, created_by, state) "
                    "VALUES (%s,%s,%s,'draft') RETURNING id", (project, "g", PRINCIPAL))
        g = cur.fetchone()["id"]
    run = lab.start_run(PRINCIPAL, project, g, plan=_plan(200), repository="", branch="")
    claim_times = []
    for _ in range(100):
        t0 = time.perf_counter()
        task = lab.claim_task(run["id"], "w1")
        claim_times.append((time.perf_counter() - t0) * 1000.0)
        if task is None:
            break
    m["claim_task_ms"] = _pct(claim_times)

    # 3. concurrent claim throughput: N workers, one run with many tasks
    with store._tx() as cur:
        cur.execute("INSERT INTO nexus_goals(project_id, text, created_by, state) "
                    "VALUES (%s,%s,%s,'draft') RETURNING id", (project, "g", PRINCIPAL))
        g = cur.fetchone()["id"]
    run2 = lab.start_run(PRINCIPAL, project, g, plan=_plan(500), repository="", branch="")
    conc = {}
    for workers in (1, 5, 10, 25):
        # A fresh run per level: claiming consumes tasks, so each level needs
        # its own run or the later levels would find nothing left to claim.
        with store._tx() as cur:
            cur.execute("INSERT INTO nexus_goals(project_id, text, created_by, state) "
                        "VALUES (%s,%s,%s,'draft') RETURNING id", (project, "g", PRINCIPAL))
            g = cur.fetchone()["id"]
        r = lab.start_run(PRINCIPAL, project, g, plan=_plan(500), repository="", branch="", max_iterations=500)
        claimed = [0]
        lock = threading.Lock()
        def w():
            n = 0
            while True:
                t = lab.claim_task(r["id"], f"w-{threading.get_ident()}")
                if t is None:
                    break
                n += 1
            with lock:
                claimed[0] += n
        th = [threading.Thread(target=w) for _ in range(workers)]
        t0 = time.perf_counter()
        for t in th:
            t.start()
        for t in th:
            t.join()
        wall = time.perf_counter() - t0
        conc[str(workers)] = {"workers": workers, "claimed": claimed[0],
                              "wall_s": round(wall, 4),
                              "claims_per_s": round(claimed[0] / wall, 1) if wall else 0.0}
    m["concurrent_claim"] = conc

    # 4. reclaim_expired (no expired leases -> the empty scan cost)
    m["reclaim_expired_scan_ms"] = _pct([
        (lambda t0: (lab.reclaim_expired(run2["id"]), (time.perf_counter()-t0)*1000)[1])(time.perf_counter())
        for _ in range(20)])

    # 5. queue_cleanups (the worker's retention scan)
    m["queue_cleanups_ms"] = _pct([
        (lambda t0: (lab.queue_cleanups(retention_seconds=0), (time.perf_counter()-t0)*1000)[1])(time.perf_counter())
        for _ in range(20)])

    # 6. store dashboard counts (aggregate query)
    m["dashboard_counts_ms"] = _pct([
        (lambda t0: (store.dashboard_counts(), (time.perf_counter()-t0)*1000)[1])(time.perf_counter())
        for _ in range(50)])

    store.close()
    payload = json.dumps(out, indent=2, ensure_ascii=False)
    print(payload)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
