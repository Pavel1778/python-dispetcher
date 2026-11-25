#!/usr/bin/env python3
"""Compare real PostgresStore task reads on an explicitly disposable database.

This creates and deletes task rows, applies migrations and runs ANALYZE. Never
point it at a shared or production database. The database name must start with
``nexus_bench_`` and tasks must initially be empty. Example::

    NEXUS_BENCHMARK_DATABASE_URL=postgresql://.../nexus_bench_example \\
      python benchmarks/dispatcher_postgres.py --allow-disposable-writes
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestration.cloud_store import PostgresStore, TaskState  # noqa: E402


STATES = [TaskState.ASSIGNED, TaskState.RUNNING, TaskState.VERIFYING, TaskState.RECOVERING]


def measure(fn, iterations: int) -> tuple[int, float, float]:
    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        rows = fn()
        samples.append((time.perf_counter() - start) * 1000)
    samples.sort()
    return len(rows), statistics.median(samples), samples[round(0.95 * (iterations - 1))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-disposable-writes", action="store_true")
    parser.add_argument("--backlog", type=int, default=20000)
    parser.add_argument("--active", type=int, default=50)
    parser.add_argument("--iterations", type=int, default=40)
    args = parser.parse_args()
    dsn = os.environ.get("NEXUS_BENCHMARK_DATABASE_URL", "")
    if (not args.allow_disposable_writes or not dsn
            or not conninfo_to_dict(dsn).get("dbname", "").startswith("nexus_bench_")
            or not 1 <= args.iterations <= 10000
            or not 0 <= args.backlog <= 1000000 or not 0 <= args.active <= 1000000):
        parser.error("use --allow-disposable-writes, a nexus_bench_* database and valid counts")
    marker = "benchmark-" + uuid.uuid4().hex
    store = PostgresStore(dsn)
    try:
        with store._tx() as cur:
            cur.execute("SELECT count(*) AS count FROM tasks")
            if cur.fetchone()["count"]:
                raise RuntimeError("refusing: benchmark database already contains tasks")
            cur.execute("INSERT INTO tasks(goal,state) SELECT %s,'completed' FROM generate_series(1,%s)",
                        (marker, args.backlog))
            cur.execute("INSERT INTO tasks(goal,state) SELECT %s,'running' FROM generate_series(1,%s)",
                        (marker, args.active))
            cur.execute("INSERT INTO conversations(id, task_id, external_conversation_id, status) "
                        "SELECT gen_random_uuid(), id, %s || id::text, 'running' FROM tasks "
                        "WHERE goal=%s AND state='running'", (marker, marker))
            cur.execute("ANALYZE tasks")
            cur.execute("ANALYZE conversations")
        ids = [task.id for task in store.list_tasks(states=STATES)]
        for label, fn in (("full", store.list_tasks),
                          ("filtered", lambda: store.list_tasks(states=STATES)),
                          ("individual conversations", lambda: [store.conversation_for(i) for i in ids]),
                          ("batch conversations", lambda: store.conversation_ids_for_tasks(ids))):
            count, p50, p95 = measure(fn, args.iterations)
            print(f"{label}: rows={count}, p50={p50:.3f} ms, p95={p95:.3f} ms")
    finally:
        try:
            with store._tx() as cur:
                cur.execute("DELETE FROM tasks WHERE goal=%s", (marker,))
        finally:
            store.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except psycopg.Error as exc:
        # Do not print connection strings or credentials.
        print(f"PostgreSQL error: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
