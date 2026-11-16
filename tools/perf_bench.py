#!/usr/bin/env python3
"""NEXUS performance benchmark: DB, scheduler claim, API concurrency, archive.

The instrument for the performance-evolution program. It measures the real code
paths against a real PostgreSQL (never a mock), at a fixed data set, so a run
before and after a change can be compared with ``tools/benchmark_compare.py``.

Sections (``--only`` selects them):

* ``db``       — mixed read/write against the store, swept over concurrency.
* ``claim``    — the scheduler's ``claim_task``/``release_task`` throughput.
* ``api``      — the real web server under concurrent HTTP clients.
* ``archive``  — ``archive_safety`` inspection of a real zip.
* ``memory``   — RSS / threads / open descriptors / DB connections over time.

Nothing here talks to OpenHands Cloud, a real GitHub or a paid LLM: the API
section uses the same local stand-in servers the test suite uses.

Usage::

    TEST_SUPABASE_DB_URL=postgresql://user:pw@127.0.0.1:5432/bench \\
      python tools/perf_bench.py --out before.json

The schema is reset per run so the numbers are not polluted by earlier data.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import gc
import io
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orchestration.cloud_store import PostgresStore  # noqa: E402

TOKEN = "perf-machine-token"


def percentiles(samples: list[float]) -> dict:
    if not samples:
        return {"n": 0}
    s = sorted(samples)

    def at(p: float) -> float:
        i = min(len(s) - 1, int(round(p * (len(s) - 1))))
        return round(s[i], 3)

    return {"n": len(s), "min": round(s[0], 3), "p50": at(0.50), "p90": at(0.90),
            "p95": at(0.95), "p99": at(0.99), "max": round(s[-1], 3),
            "mean": round(statistics.fmean(s), 3)}


def _rss_mb() -> float:
    with open("/proc/self/statm") as fh:
        pages = int(fh.read().split()[1])
    return round(pages * (os.sysconf("SC_PAGE_SIZE") / 1024 / 1024), 1)


def _threads() -> int:
    return threading.active_count()


def _open_fds() -> int:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return -1


# --------------------------------------------------------------- db section
def _seed(store: PostgresStore, projects: int, per_project: int) -> dict:
    from orchestration.nexus_projects import ProjectService
    from orchestration.nexus_conversations import ConversationService
    from orchestration.nexus_lab_store import LabStore

    projects_svc = ProjectService(store)
    convs = ConversationService(store)
    lab = LabStore(store)
    ids = {"projects": [], "conversations": [], "runs": []}
    for i in range(projects):
        p = projects_svc.create("user:bench", f"bench-{i}", "seed")
        ids["projects"].append(p["id"])
        for j in range(per_project):
            c = convs.create("user:bench", p["id"], title=f"chat-{i}-{j}")
            ids["conversations"].append((p["id"], c["id"]))
            with store._tx() as cur:
                for k in range(10):
                    cur.execute(
                        "INSERT INTO nexus_conversation_messages(conversation_id, role, content) "
                        "VALUES (%s,%s,%s)",
                        (c["id"], "user" if k % 2 == 0 else "assistant",
                         f"message {k} " + "text " * 20))
        goal_id = _goal_id(store, p["id"], f"goal-{i}")
        run = lab.start_run("user:bench", p["id"], goal_id, policy="bounded",
                            repository="", branch="",
                            plan=[{"id": f"t{i}-{k}", "title": f"task {k}",
                                   "description": "do", "acceptance": ["ok"],
                                   "dependencies": []} for k in range(per_project)])
        ids["runs"].append(run["id"])
    return ids


def _goal_id(store: PostgresStore, project_id: str, text: str) -> str:
    """Create a goal and return its id (a ``start_run`` argument)."""
    from orchestration.nexus_specs import SpecService
    goal, _spec, _created = SpecService(store).create_with_goal(
        "user:bench", project_id, text)
    return str(goal["id"])


def _db_worker(store: PostgresStore, ids: dict, iters: int, counters: dict) -> None:
    pid = ids["projects"][0]
    cid = ids["conversations"][0][1]
    run = ids["runs"][0]
    lat: list[float] = []
    for n in range(iters):
        t0 = time.perf_counter()
        try:
            with store._tx() as cur:
                cur.execute("SELECT id, name FROM nexus_projects WHERE id=%s", (pid,))
                cur.fetchone()
                cur.execute(
                    "INSERT INTO nexus_conversation_messages(conversation_id, role, content) "
                    "VALUES (%s,'user',%s)", (cid, f"w{n}"))
                cur.execute("SELECT count(*) AS n FROM nexus_run_tasks WHERE run_id=%s", (run,))
                cur.fetchone()
        except Exception as e:  # noqa: BLE001
            counters["errors"] = counters.get("errors", 0) + 1
            counters["last_error"] = str(e)[:200]
        lat.append((time.perf_counter() - t0) * 1000.0)
    with counters["lock"]:
        counters.setdefault("lat", []).extend(lat)


def bench_db(dsn: str, concurrency_levels: list[int], iters: int) -> dict:
    from tests.test_studio_api import _reset_schema
    out = {}
    for c in concurrency_levels:
        _reset_schema()  # a clean, identical data set for every level
        store = PostgresStore(dsn, auto_migrate=False)
        try:
            ids = _seed(store, projects=2, per_project=3)
            counters: dict = {"lock": threading.Lock()}
            t0 = time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=c) as ex:
                futs = [ex.submit(_db_worker, store, ids, iters, counters) for _ in range(c)]
                for f in futs:
                    f.result()
            wall = time.perf_counter() - t0
            out[str(c)] = {
                "throughput_ops_s": round((c * iters) / wall, 1),
                "errors": counters.get("errors", 0),
                "last_error": counters.get("last_error"),
                "latency_ms": percentiles(counters.get("lat", [])),
            }
        finally:
            store.close()
    return out


# ------------------------------------------------------------ claim section
def bench_claim(dsn: str, workers: list[int], tasks_per_worker: int) -> dict:
    from tests.test_studio_api import _reset_schema
    from orchestration.nexus_lab_store import LabStore
    out = {}
    for w in workers:
        _reset_schema()
        store = PostgresStore(dsn, auto_migrate=False)
        try:
            project = _project(store)
            goal_id = _goal_id(store, project, f"claim-{w}")
            lab = LabStore(store)
            run = lab.start_run("user:bench", project, goal_id, policy="bounded",
                                repository="", branch="",
                                plan=[{"id": f"c{k}", "title": f"t{k}", "description": "d",
                                       "acceptance": ["ok"], "dependencies": []}
                                      for k in range(w * tasks_per_worker)])
            run_id = run["id"]
            lat: list[float] = []
            errors = {"n": 0}

            def claim_loop():
                for _ in range(tasks_per_worker):
                    t0 = time.perf_counter()
                    try:
                        task = lab.claim_task(run_id, "bench", lease_seconds=900)
                        if task is not None:
                            lab.release_task(task["id"], error="", requeue=True,
                                             lease_token=task.get("lease_token"))
                    except Exception:  # noqa: BLE001
                        errors["n"] += 1
                    lat.append((time.perf_counter() - t0) * 1000.0)

            t0 = time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=w) as ex:
                list(ex.map(lambda _: claim_loop(), range(w)))
            wall = time.perf_counter() - t0
            out[str(w)] = {
                "claims_s": round((w * tasks_per_worker) / wall, 1),
                "errors": errors["n"],
                "latency_ms": percentiles(lat),
            }
        finally:
            store.close()
    return out


def _project(store: PostgresStore) -> str:
    """A fresh project (the schema was just reset, so none exists yet)."""
    from orchestration.nexus_projects import ProjectService
    return ProjectService(store).create("user:bench", "claim-bench", "")["id"]


# -------------------------------------------------------------- api section
def _start_stack(dsn: str):
    from http.server import ThreadingHTTPServer
    from tests.fake_cloud import FakeCloudServer
    from tests.fake_github import FakeGitHub, FakeGitHubServer
    from tests.fake_runtime import FakeServer
    from tests.test_providers import FakeProvider
    from orchestration.cloud_client import CloudClient
    from orchestration.github_client import GitHubClient
    from orchestration.nexus_chat import ChatResponder
    from orchestration.nexus_planner import Planner
    from orchestration.providers import GroqProvider, ProviderCascade
    from orchestration.web_server import App, WebServer

    cloud = FakeCloudServer()
    cloud.start()
    github = FakeGitHub()
    gh_server = FakeGitHubServer(github)
    gh_server.start()
    sandbox = FakeServer()
    sandbox.start()
    llm = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    llm.script = [{"payload": {"choices": [{"message": {"content": "ok"}}]}}]
    llm.calls, llm.index = [], 0
    threading.Thread(target=llm.serve_forever, daemon=True).start()
    app = App(dsn, poll_interval=0.2, token=TOKEN, lab_inline=False)
    app.client = CloudClient(cloud.base_url, api_key="k", timeout=10.0)
    app.dispatcher.client = app.client
    app.runtime.client = app.client
    app.verifier.client = app.client
    app.github = GitHubClient(base_url=gh_server.base_url, token="gh")
    app.providers = ProviderCascade([GroqProvider(
        base_url=f"http://127.0.0.1:{llm.server_port}", api_key="k", model="m",
        sleep=lambda _s: None)])
    app.chat = ChatResponder(app.providers, dispatch_code=app._chat_dispatch_code,
                             start_autonomous=app._chat_start_autonomous)
    app.planner = Planner(app.providers)
    server = WebServer(app, "127.0.0.1", 0)
    server.start()
    return {"app": app, "server": server, "cloud": cloud, "github": gh_server,
            "sandbox": sandbox, "llm": llm}


def _stop_stack(stack: dict) -> None:
    stack["server"].stop()
    try:
        stack["app"].store.close()
    except Exception:  # noqa: BLE001
        pass
    stack["cloud"].stop()
    stack["github"].stop()
    stack["sandbox"].stop()
    stack["llm"].shutdown()


def _api_client(base: str, path: str) -> tuple[int, float]:
    req = urllib.request.Request(base + path)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return r.status, (time.perf_counter() - t0) * 1000.0
    except urllib.error.HTTPError as e:
        return e.code, (time.perf_counter() - t0) * 1000.0


def bench_api(dsn: str, concurrency_levels: list[int], requests_per_client: int) -> dict:
    from tests.test_studio_api import _reset_schema
    _reset_schema()
    stack = _start_stack(dsn)
    out = {}
    try:
        base = f"http://127.0.0.1:{stack['server'].port}"
        app = stack["app"]
        project = app.projects.create("user:bench", "api-bench", "")["id"]
        conv = app.conversations.create("user:bench", project, title="c")["id"]
        paths = [f"/api/v1/projects/{project}",
                 f"/api/v1/projects/{project}/conversations",
                 f"/api/v1/projects/{project}/conversations/{conv}/messages",
                 "/api/studio"]
        for c in concurrency_levels:
            lat: list[float] = []
            errs = {"n": 0}

            def client(_i):
                for n in range(requests_per_client):
                    code, ms = _api_client(base, paths[n % len(paths)])
                    lat.append(ms)
                    if code >= 500:
                        errs["n"] += 1

            t0 = time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=c) as ex:
                list(ex.map(client, range(c)))
            wall = time.perf_counter() - t0
            out[str(c)] = {
                "throughput_req_s": round((c * requests_per_client) / wall, 1),
                "server_errors": errs["n"],
                "latency_ms": percentiles(lat),
            }
    finally:
        _stop_stack(stack)
    return out


# ---------------------------------------------------------- archive section
def _make_zip(files: int, size: int) -> bytes:
    """A *legitimate* archive: incompressible data, so the zip-bomb ratio guard
    does not (correctly) refuse it. ``os.urandom`` gives a realistic ratio."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i in range(files):
            z.writestr(f"dir{i % 20}/file{i}.txt", os.urandom(size))
    return buf.getvalue()


def bench_archive(datasets: dict) -> dict:
    from orchestration.archive_safety import open_zip
    out = {}
    for name, (files, size) in datasets.items():
        data = _make_zip(files, size)
        lat: list[float] = []
        total = 0
        for _ in range(5):
            t0 = time.perf_counter()
            report = open_zip(data)
            lat.append((time.perf_counter() - t0) * 1000.0)
            total = sum(len(m.data) for m in report.members)
        out[name] = {"archive_bytes": len(data), "uncompressed_bytes": total,
                     "members": files, "latency_ms": percentiles(lat)}
    return out


# ----------------------------------------------------------- memory section
def bench_memory(dsn: str, seconds: float, concurrency: int) -> dict:
    """Steady HTTP load; sample RSS/threads/fds/DB connections over time."""
    from tests.test_studio_api import _reset_schema
    _reset_schema()
    stack = _start_stack(dsn)
    base = f"http://127.0.0.1:{stack['server'].port}"
    app = stack["app"]
    project = app.projects.create("user:bench", "mem-bench", "")["id"]
    samples = []
    stop = threading.Event()
    counts = {"n": 0}

    def load():
        while not stop.is_set():
            _api_client(base, f"/api/v1/projects/{project}")
            counts["n"] += 1

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=concurrency)
    for _ in range(concurrency):
        ex.submit(load)
    try:
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < seconds:
            time.sleep(2.0)
            with app.store._tx() as cur:
                cur.execute("SELECT count(*) AS n FROM pg_stat_activity "
                            "WHERE datname=current_database()")
                db_conns = cur.fetchone()["n"]
            samples.append({"t": round(time.perf_counter() - t0, 1),
                            "rss_mb": _rss_mb(), "threads": _threads(),
                            "fds": _open_fds(), "db_conns": db_conns,
                            "requests": counts["n"]})
    finally:
        stop.set()
        ex.shutdown(wait=False)
        _stop_stack(stack)
    gc.collect()
    return {"samples": samples, "requests": counts["n"]}


def _max_connections(dsn: str) -> int | None:
    try:
        store = PostgresStore(dsn, auto_migrate=False)
        with store._tx() as cur:
            cur.execute("SHOW max_connections")
            n = int(cur.fetchone()["max_connections"])
        store.close()
        return n
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--only", default="db,claim,api,archive,memory")
    ap.add_argument("--concurrency", default="1,5,10,25,50,100")
    ap.add_argument("--iters", type=int, default=60)
    ap.add_argument("--api-requests", type=int, default=25)
    ap.add_argument("--workers", default="1,5,10,25,50")
    ap.add_argument("--tasks-per-worker", type=int, default=20)
    ap.add_argument("--mem-seconds", type=float, default=20.0)
    ap.add_argument("--mem-concurrency", type=int, default=8)
    args = ap.parse_args()

    dsn = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        print("set TEST_SUPABASE_DB_URL", file=sys.stderr)
        return 2
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    levels = [int(x) for x in args.concurrency.split(",")]
    workers = [int(x) for x in args.workers.split(",")]

    result = {"environment": {
        "python": sys.version.split()[0], "rss_mb_start": _rss_mb(),
        "max_connections": _max_connections(dsn)}, "measurements": {}}
    m = result["measurements"]

    if "db" in only:
        m["db"] = bench_db(dsn, levels, args.iters)
    if "claim" in only:
        m["claim"] = bench_claim(dsn, workers, args.tasks_per_worker)
    if "api" in only:
        m["api"] = bench_api(dsn, levels, args.api_requests)
    if "archive" in only:
        m["archive"] = bench_archive({
            "small": (20, 1024), "medium": (200, 4096), "many_small": (1000, 256),
            "few_large": (5, 256 * 1024)})
    if "memory" in only:
        m["memory"] = bench_memory(dsn, args.mem_seconds, args.mem_concurrency)

    payload = json.dumps(result, indent=2, ensure_ascii=False)
    print(payload)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        print(f"\nwritten to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
