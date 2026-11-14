#!/usr/bin/env python3
"""DB access concurrency benchmark: A (current) vs B (optimized Python).

Same PostgreSQL 17, same SQL, same dataset, same concurrency levels. This is
an isolated research program; it is NOT part of the NEXUS runtime and does not
import or modify it.

Variant A reproduces the current NEXUS store: ONE connection guarded by a
threading.RLock (orchestration/cloud_store.py:276-281, _tx at :536).

Variant B is the optimized Python: a psycopg_pool ConnectionPool, one
connection per concurrent worker, no global lock.

The workload is a real NEXUS query against seeded data:
    SELECT count(*) FROM nexus_conversation_messages WHERE conversation_id = %s
run once per operation, so the measured cost is the round trip + lock/pool
acquisition, not a synthetic spin.

Usage:
  TEST_SUPABASE_DB_URL=postgresql://... python benchmarks/db_concurrency_bench.py
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

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SQL = "SELECT count(*) AS n FROM nexus_conversation_messages WHERE conversation_id = %s"


def _percentiles(samples: list[float]) -> dict:
    s = sorted(samples)
    def at(p):
        if not s:
            return 0.0
        return round(s[min(len(s) - 1, int(round(p * (len(s) - 1))))], 3)
    return {"n": len(s), "p50": at(0.50), "p90": at(0.90), "p95": at(0.95),
            "p99": at(0.99), "mean": round(statistics.fmean(s), 3) if s else 0.0,
            "max": round(max(s), 3) if s else 0.0}


class VariantA:
    """Current NEXUS: one connection + RLock (serialized)."""

    name = "A_current_single_conn_rlock"

    def __init__(self, dsn: str):
        self._conn = psycopg.connect(
            dsn, row_factory=dict_row, autocommit=False,
            connect_timeout=10.0, prepare_threshold=None)
        self._lock = threading.RLock()

    def query(self, cid: str) -> None:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(SQL, (cid,))
                cur.fetchone()
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()


class VariantB:
    """Optimized Python: a connection pool, one connection per worker."""

    name = "B_optimized_connection_pool"

    def __init__(self, dsn: str, max_size: int):
        self._pool = ConnectionPool(
            dsn, min_size=1, max_size=max_size, open=True,
            kwargs={"row_factory": dict_row, "autocommit": True,
                    "prepare_threshold": None})

    def query(self, cid: str) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(SQL, (cid,))
                cur.fetchone()

    def close(self) -> None:
        self._pool.close()


def _run_level(variant, cid: str, concurrency: int, per_worker: int) -> dict:
    """Every worker runs per_worker queries; wall time and per-query latency."""
    lat: list[float] = []
    lat_lock = threading.Lock()
    errors = [0]

    def worker():
        local = []
        for _ in range(per_worker):
            t0 = time.perf_counter()
            try:
                variant.query(cid)
            except Exception:
                with lat_lock:
                    errors[0] += 1
                continue
            local.append((time.perf_counter() - t0) * 1000.0)
        with lat_lock:
            lat.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0
    total_ops = concurrency * per_worker
    return {
        "concurrency": concurrency,
        "total_ops": total_ops,
        "wall_s": round(wall, 4),
        "throughput_ops_s": round(total_ops / wall, 1) if wall else 0.0,
        "errors": errors[0],
        "latency_ms": _percentiles(lat),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--per-worker", type=int, default=40,
                    help="queries each concurrent worker runs")
    ap.add_argument("--pool-max", type=int, default=20)
    args = ap.parse_args()

    dsn = os.environ.get("TEST_SUPABASE_DB_URL")
    if not dsn:
        print("set TEST_SUPABASE_DB_URL", file=sys.stderr)
        return 2

    with psycopg.connect(dsn) as c:
        with c.cursor() as cur:
            cur.execute("SELECT id FROM nexus_conversations LIMIT 1")
            row = cur.fetchone()
            if row is None:
                print("no conversations seeded; run tools/benchmark.py first",
                      file=sys.stderr)
                return 3
            cid = str(row[0])

    levels = [1, 5, 10, 25, 50, 100]
    out: dict = {"dsn_host": dsn.split("@")[-1], "workload": SQL,
                 "per_worker": args.per_worker, "levels": levels, "results": {}}

    for _ in range(20):
        with psycopg.connect(dsn) as c:
            with c.cursor() as cur:
                cur.execute(SQL, (cid,))
                cur.fetchone()

    a = VariantA(dsn)
    b = VariantB(dsn, max_size=max(args.pool_max, 100))
    try:
        out["results"][a.name] = {}
        out["results"][b.name] = {}
        for lvl in levels:
            out["results"][a.name][str(lvl)] = _run_level(a, cid, lvl, args.per_worker)
            out["results"][b.name][str(lvl)] = _run_level(b, cid, lvl, args.per_worker)
    finally:
        a.close()
        b.close()

    payload = json.dumps(out, indent=2)
    print(payload)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
