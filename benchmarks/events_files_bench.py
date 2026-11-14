#!/usr/bin/env python3
"""Event processing and file/archive processing benchmarks.

Real code paths on a real PostgreSQL 17:
- event write (the task_events INSERT the store does per state change);
- project event append + read;
- file upload (sha256 + INSERT) at several sizes;
- archive inspection (safe open) and safe extraction of a generated zip.

Isolated research program: it calls the real functions but changes nothing.

Usage:
  TEST_SUPABASE_DB_URL=postgresql://... python benchmarks/events_files_bench.py --out ef.json
"""
from __future__ import annotations

import argparse
import io
import json
import os
import statistics
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_studio_api import _reset_schema  # noqa: E402
from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402
from orchestration.file_lab import FileLabService
from orchestration import archive_safety  # noqa: E402

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


def _make_zip(members: int, size_each: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(members):
            zf.writestr(f"dir{i%10}/file{i}.txt", "x" * size_each)
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--repeat", type=int, default=30)
    args = ap.parse_args()

    dsn = os.environ.get("TEST_SUPABASE_DB_URL")
    if not dsn:
        print("set TEST_SUPABASE_DB_URL", file=sys.stderr)
        return 2
    _reset_schema()
    store = PostgresStore(dsn)
    projects = ProjectService(store)
    lab_files = FileLabService(store)
    project = projects.create(PRINCIPAL, "Bench", "")["id"]

    out: dict = {"environment": {"python": sys.version.split()[0]}, "measurements": {}}
    m = out["measurements"]

    # 1. event write: the store's task_events INSERT, one per call
    def event():
        with store._tx() as cur:
            store._event(cur, "bench.event", detail={"i": 1})
    m["event_write_ms"] = _pct([(lambda t0: (event(), (time.perf_counter()-t0)*1000)[1])(time.perf_counter())
                                for _ in range(args.repeat)])

    # 2. project event append (the audit trail write)
    def pevent():
        with store._tx() as cur:
            cur.execute("INSERT INTO nexus_project_events(project_id, actor, kind, resource_id) "
                        "VALUES (%s,%s,%s,%s)", (project, PRINCIPAL, "bench", project))
    m["project_event_append_ms"] = _pct([(lambda t0: (pevent(), (time.perf_counter()-t0)*1000)[1])(time.perf_counter())
                                         for _ in range(args.repeat)])

    # 3. file upload at several sizes (sha256 + INSERT with the bytes)
    for kb in (4, 64, 512, 2048):
        data = os.urandom(kb * 1024)
        def up(d=data):
            lab_files.upload(PRINCIPAL, project, filename=f"bench-{kb}.bin", content_type="application/octet-stream", data=d)
        m[f"file_upload_{kb}kb_ms"] = _pct([(lambda t0: (up(), (time.perf_counter()-t0)*1000)[1])(time.perf_counter())
                                            for _ in range(max(5, args.repeat // 3))])

    # 4. archive inspection (safe open) at several member counts
    for members in (100, 1000, 2000):
        blob = _make_zip(members, 200)
        m[f"archive_inspect_{members}_members_ms"] = _pct(
            [(lambda t0: (archive_safety.open_zip(blob), (time.perf_counter()-t0)*1000)[1])(time.perf_counter())
             for _ in range(max(3, args.repeat // 5))])

    # 5. safe extraction into a workspace
    blob = _make_zip(1000, 200)
    up_id = lab_files.upload(PRINCIPAL, project, filename="bench.zip", content_type="application/zip", data=blob)["id"]
    def extract():
        lab_files.extract_into_workspace(PRINCIPAL, project, name="bench-ws", file_id=up_id)
    m["archive_extract_1000_members_ms"] = _pct(
        [(lambda t0: (extract(), (time.perf_counter()-t0)*1000)[1])(time.perf_counter())
         for _ in range(3)])

    store.close()
    payload = json.dumps(out, indent=2, ensure_ascii=False)
    print(payload)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
