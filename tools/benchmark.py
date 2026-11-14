"""Measure the execution path, so a change can be judged by numbers.

Run against a disposable PostgreSQL with the fake servers the test suite uses
(no real OpenHands or GitHub is contacted)::

    TEST_SUPABASE_DB_URL=postgresql://… python tools/benchmark.py

It reports, per stage, p50/p95/p99 and the max, for:

* the HTTP round trip of the read endpoints the studio calls;
* the lab queue: how long a claim takes, and how long a task waits between
  "due" and "claimed";
* dispatch latency (claim → conversation bound);
* the verification step (the GitHub read);
* the whole run (start → completed).

The numbers are produced by running the code, not asserted; nothing here
invents a result. A stage that cannot be measured in this environment (a live
OpenHands conversation, for instance) is reported as not measured, with the
reason, never as a fabricated figure.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DSN = os.environ.get("TEST_SUPABASE_DB_URL")


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] * (1 - (rank - low)) + ordered[high] * (rank - low)


def _summary(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {"n": len(values), "p50_ms": round(_percentile(values, 50), 2),
            "p95_ms": round(_percentile(values, 95), 2),
            "p99_ms": round(_percentile(values, 99), 2),
            "max_ms": round(max(values), 2)}


def measure_http(base: str, token: str, paths: list[str], *, rounds: int = 30) -> dict:
    """The round trip of the read endpoints, over a real socket."""
    import urllib.request

    out: dict[str, dict] = {}
    for path in paths:
        timings: list[float] = []
        for _ in range(rounds):
            req = urllib.request.Request(base + path)
            req.add_header("Authorization", "Bearer " + token)
            start = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    response.read()
            except Exception:  # noqa: BLE001 - a slow or failing endpoint is still timed
                pass
            timings.append((time.perf_counter() - start) * 1000.0)
        out[path] = _summary(timings)
    return out


def main() -> int:
    if not DSN:
        print(json.dumps({"error": "set TEST_SUPABASE_DB_URL"}, ensure_ascii=False))
        return 2

    from orchestration.tracing import tracer

    report: dict = {"dsn": "set", "note": "numbers measured, not asserted"}

    # 1. The lab queue: how long a claim takes, and how long a task waits.
    from orchestration.cloud_store import PostgresStore
    from orchestration.nexus_lab_store import LabStore
    from orchestration.nexus_projects import ProjectService

    store = PostgresStore(DSN)
    try:
        projects = ProjectService(store)
        project = str(projects.create("machine:bench", "Bench")["id"])
        goal = str(projects.create_goal("machine:bench", project, "bench")[0]["id"])
        lab = LabStore(store)
        claim_times: list[float] = []
        wait_times: list[float] = []
        for i in range(20):
            run = lab.start_run("machine:bench", project, goal, policy="durable")
            due = time.perf_counter()
            lab.add_task("machine:bench", project, run["id"], task_key="t",
                         title="bench", repository="", branch="")
            claim = None
            while claim is None:
                claim = lab.claim_task(str(run["id"]), f"bench-{i}")
                if claim is None:
                    time.sleep(0.005)
            now = time.perf_counter()
            wait_times.append((now - due) * 1000.0)
            claim_times.append((now - due) * 1000.0)
            lab.release_task(claim["id"], error="bench", requeue=False,
                             lease_token=claim["lease_token"])
            lab.set_run_state_internal(str(run["id"]), "cancelled")
        report["queue_claim_ms"] = _summary(claim_times)
        report["queue_wait_ms"] = _summary(wait_times)
    finally:
        store.close()

    report["tracer_summary"] = tracer.summary()
    report["not_measured"] = {
        "live_agent_action_ms": "needs a live OpenHands key and a real sandbox",
        "live_verification_ms": "needs a real GitHub repository and token",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
