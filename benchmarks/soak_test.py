#!/usr/bin/env python3
"""Long-run soak test for NEXUS (independent acceptance, Stage 4).

Runs the real server against a real PostgreSQL with a steady read load plus
periodic bursts, a File Laboratory operation, and controlled temporary database
faults (the pool's connections are dropped from the server side, then the pool
recovers). Samples RSS, threads, file descriptors, database connections and pool
wait time on a fixed interval and prints a JSON summary.

It never touches production: it uses TEST_SUPABASE_DB_URL (a throwaway
database) and the stand-in OpenHands/GitHub/LLM servers.

Usage:
  TEST_SUPABASE_DB_URL=postgresql://... python benchmarks/soak_test.py --minutes 60 --out soak.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_studio_api import _reset_schema  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402
from tests.fake_github import FakeGitHub, FakeGitHubServer  # noqa: E402
from tests.fake_runtime import FakeServer  # noqa: E402
from tests.test_providers import FakeProvider  # noqa: E402
from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.nexus_chat import ChatResponder  # noqa: E402
from orchestration.nexus_planner import Planner  # noqa: E402
from orchestration.providers import GroqProvider, ProviderCascade  # noqa: E402
from orchestration.web_auth import hash_password  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402

TOKEN = "soak-machine-token"


def _rss_mb() -> float:
    """Current resident set size in MB.

    NOT ``ru_maxrss``: that is the *peak* and only ever grows, so it would show
    a "leak" in any long run even when the process is steady. The current RSS is
    what tells a leak from a plateau.
    """
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return -1.0


def _fds() -> int:
    try:
        return len(os.listdir(f"/proc/{os.getpid()}/fd"))
    except OSError:
        return -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--out", default="")
    ap.add_argument("--readers", type=int, default=8)
    args = ap.parse_args()

    dsn = os.environ.get("TEST_SUPABASE_DB_URL")
    if not dsn:
        print("set TEST_SUPABASE_DB_URL", file=sys.stderr)
        return 2
    _reset_schema()
    for k, v in {"NEXUS_LAB_INLINE": "0", "NEXUS_ALLOW_PRIVATE_TARGETS": "1",
                 "OPENHANDS_API_KEY": "k", "NEXUS_SECRET_KEY": "b" * 32,
                 "AUTH_USERNAME": "owner", "AUTH_SESSION_SECRET": "b" * 32}.items():
        os.environ.setdefault(k, v)
    os.environ.setdefault("AUTH_PASSWORD_HASH", hash_password("p"))

    llm = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    llm.script, llm.calls, llm.index = [{"payload": {"choices": [
        {"message": {"content": "ok"}}]}}], [], 0
    threading.Thread(target=llm.serve_forever, daemon=True).start()

    out: dict = {"minutes": args.minutes, "readers": args.readers, "samples": [],
                 "errors": [], "faults": 0}
    with (FakeCloudServer() as cloud, FakeGitHubServer(FakeGitHub()), FakeServer()):
        app = App(dsn, poll_interval=0.5, token=TOKEN, lab_inline=False)
        app.client = CloudClient(cloud.base_url, api_key="k", timeout=10.0)
        app.dispatcher.client = app.client
        app.runtime.client = app.client
        app.verifier.client = app.client
        app.providers = ProviderCascade([GroqProvider(
            base_url=f"http://127.0.0.1:{llm.server_port}", api_key="k",
            model="m", sleep=lambda _s: None)])
        app.chat = ChatResponder(app.providers, dispatch_code=app._chat_dispatch_code,
                                 start_autonomous=app._chat_start_autonomous)
        app.planner = Planner(app.providers)
        server = WebServer(app, "127.0.0.1", 0)
        server.start()
        base = f"http://127.0.0.1:{server.port}"

        def api(method, path, body=None):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(base + path, data=data, method=method)
            req.add_header("Authorization", f"Bearer {TOKEN}")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else None

        pid = api("POST", "/api/v1/projects", {"name": "Soak"})["project"]["id"]
        for _ in range(20):
            api("POST", f"/api/v1/projects/{pid}/conversations",
                {"title": "S", "mode": "ask"})

        stop = threading.Event()
        counts = {"reads": 0, "errors": 0}
        lat: list[float] = []
        lock = threading.Lock()

        def reader():
            while not stop.is_set():
                t0 = time.perf_counter()
                try:
                    api("GET", f"/api/v1/projects/{pid}/conversations?archived=1")
                    with lock:
                        counts["reads"] += 1
                        lat.append((time.perf_counter() - t0) * 1000.0)
                except Exception:
                    with lock:
                        counts["errors"] += 1

        readers = [threading.Thread(target=reader, daemon=True) for _ in range(args.readers)]
        for t in readers:
            t.start()

        def drop_connections():
            import psycopg
            with psycopg.connect(dsn, autocommit=True) as admin:
                admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                              "WHERE datname = current_database() AND pid <> pg_backend_pid()")

        def db_conns():
            import psycopg
            with psycopg.connect(dsn, autocommit=True) as c:
                with c.cursor() as cur:
                    cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
                    return cur.fetchone()[0]

        started = time.time()
        deadline = started + args.minutes * 60
        n = 0
        while time.time() < deadline:
            time.sleep(20)
            n += 1
            sample = {
                "t": round(time.time() - started, 1),
                "rss_mb": round(_rss_mb(), 1),
                "threads": threading.active_count(),
                "fds": _fds(),
                "db_conns": db_conns(),
                "reads": counts["reads"],
                "errors": counts["errors"],
                "pool": app.store.pool_stats(),
            }
            out["samples"].append(sample)
            # Periodic burst: a File Lab extraction.
            if n % 3 == 0:
                try:
                    import io
                    import uuid
                    import zipfile
                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                        for i in range(200):
                            z.writestr(f"d{i%5}/f{i}.txt", "x" * 200)
                    req = urllib.request.Request(
                        base + f"/api/v1/projects/{pid}/files", data=buf.getvalue(),
                        method="POST")
                    req.add_header("Authorization", f"Bearer {TOKEN}")
                    req.add_header("Content-Type", "application/zip")
                    req.add_header("X-File-Name", "soak-" + uuid.uuid4().hex + ".zip")
                    with urllib.request.urlopen(req, timeout=30) as r:
                        r.read()
                except Exception as e:  # noqa: BLE001
                    out["errors"].append(f"upload: {type(e).__name__}")
            # Periodic controlled database fault + recovery.
            if n % 5 == 0:
                out["faults"] += 1
                try:
                    drop_connections()
                except Exception as e:  # noqa: BLE001
                    out["errors"].append(f"drop: {type(e).__name__}")
                # The readers keep going; the pool must recover by itself.
                time.sleep(2)

        stop.set()
        for t in readers:
            t.join(timeout=5)
        server.stop()
        llm.shutdown()

    if lat:
        lat.sort()
        out["latency_ms"] = {
            "n": len(lat), "p50": round(lat[len(lat) // 2], 2),
            "p95": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 2),
            "p99": round(lat[min(len(lat) - 1, int(0.99 * len(lat)))], 2),
        }
    out["reads"] = counts["reads"]
    out["errors_total"] = counts["errors"]
    payload = json.dumps(out, indent=2)
    print(payload)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
