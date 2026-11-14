#!/usr/bin/env python3
"""HTTP API benchmark against the REAL NEXUS server.

Starts the real orchestration.web_server.App + WebServer on a real PostgreSQL
and the same stand-in OpenHands/GitHub/LLM servers the test suite uses, then
measures the read paths at several concurrency levels, splitting the time into
"client+HTTP" and "server" where possible.

Isolated research program: it starts the real server but does not modify it.

Usage:
  TEST_SUPABASE_DB_URL=postgresql://... python benchmarks/api_bench.py --out api.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
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
from orchestration.github_client import GitHubClient  # noqa: E402
from orchestration.nexus_chat import ChatResponder  # noqa: E402
from orchestration.nexus_planner import Planner  # noqa: E402
from orchestration.providers import GroqProvider, ProviderCascade  # noqa: E402
from orchestration.web_auth import hash_password  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402

TOKEN = "studio-machine-token"


def _pct(samples: list[float]) -> dict:
    s = sorted(samples)
    def at(p):
        if not s:
            return 0.0
        return round(s[min(len(s) - 1, int(round(p * (len(s) - 1))))], 3)
    return {"n": len(s), "p50": at(.5), "p90": at(.9), "p95": at(.95), "p99": at(.99),
            "mean": round(statistics.fmean(s), 3) if s else 0.0,
            "max": round(max(s), 3) if s else 0.0}


def _get(base: str, path: str) -> float:
    req = urllib.request.Request(base + path)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()
    return (time.perf_counter() - t0) * 1000.0


def _level(base: str, path: str, concurrency: int, per_worker: int) -> dict:
    lat, lock, err = [], threading.Lock(), [0]
    def worker():
        local = []
        for _ in range(per_worker):
            try:
                local.append(_get(base, path))
            except Exception:
                with lock:
                    err[0] += 1
        with lock:
            lat.extend(local)
    th = [threading.Thread(target=worker) for _ in range(concurrency)]
    t0 = time.perf_counter()
    for t in th: t.start()
    for t in th: t.join()
    wall = time.perf_counter() - t0
    ops = concurrency * per_worker
    return {"concurrency": concurrency, "total_ops": ops, "wall_s": round(wall, 4),
            "throughput_ops_s": round(ops / wall, 1) if wall else 0.0,
            "errors": err[0], "latency_ms": _pct(lat)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--repeat", type=int, default=50)
    args = ap.parse_args()

    dsn = os.environ.get("TEST_SUPABASE_DB_URL")
    if not dsn:
        print("set TEST_SUPABASE_DB_URL", file=sys.stderr)
        return 2
    _reset_schema()

    os.environ.setdefault("NEXUS_LAB_INLINE", "0")
    os.environ.setdefault("NEXUS_ALLOW_PRIVATE_TARGETS", "1")
    os.environ["OPENHANDS_API_KEY"] = "bench-key"
    os.environ.setdefault("NEXUS_SECRET_KEY", "bench-secret-key-0000000000000000")
    os.environ.setdefault("AUTH_USERNAME", "owner")
    os.environ.setdefault("AUTH_PASSWORD_HASH", hash_password("owner-password"))
    os.environ.setdefault("AUTH_SESSION_SECRET", "b" * 32)

    github = FakeGitHub()
    llm = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    llm.script, llm.calls, llm.index = [{"payload": {"choices": [
        {"message": {"content": "Ответ."}}]}}], [], 0
    threading.Thread(target=llm.serve_forever, daemon=True).start()

    out: dict = {"environment": {"python": sys.version.split()[0]}, "results": {}}
    with (FakeCloudServer() as cloud, FakeGitHubServer(github) as gh, FakeServer()):
        app = App(dsn, poll_interval=0.2, token=TOKEN, lab_inline=False)
        app.client = CloudClient(cloud.base_url, api_key="bench-key", timeout=10.0)
        app.dispatcher.client = app.client
        app.runtime.client = app.client
        app.verifier.client = app.client
        app.github = GitHubClient(base_url=gh.base_url, token="gh-bench")
        app.github_accounts._client_factory = lambda token: GitHubClient(
            base_url=gh.base_url, token=token)
        app.providers = ProviderCascade([GroqProvider(
            base_url=f"http://127.0.0.1:{llm.server_port}", api_key="k",
            model="bench-model", sleep=lambda _s: None)])
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
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else None

        # Seed: 3 projects x 10 conversations x 40 messages
        pids = []
        for _ in range(3):
            p = api("POST", "/api/v1/projects", {"name": "B"})["project"]["id"]
            pids.append(p)
            api("POST", f"/api/v1/projects/{p}/repository",
                {"full_name": "Pavel1778/python-dispetcher"})
            for _c in range(10):
                c = api("POST", f"/api/v1/projects/{p}/conversations",
                        {"title": "C", "mode": "ask"})["conversation"]["id"]
                with app.store._tx() as cur:
                    for i in range(40):
                        cur.execute("INSERT INTO nexus_conversation_messages"
                                    "(conversation_id, role, content) VALUES (%s,%s,%s)",
                                    (c, "user" if i % 2 == 0 else "assistant",
                                     f"Сообщение {i} " + "текст " * 20))
        pid = pids[0]

        # single-request latency (warm)
        paths = {
            "studio_status": "/api/studio",
            "list_projects": "/api/v1/projects",
            "open_project": f"/api/v1/projects/{pid}",
            "list_conversations": f"/api/v1/projects/{pid}/conversations?archived=1",
            "commands": "/api/commands",
        }
        for name, path in paths.items():
            for _ in range(5):
                _get(base, path)
            out["results"][name] = _pct([_get(base, path) for _ in range(args.repeat)])

        # concurrency sweep on one read path
        conc = {}
        for c in (1, 5, 10, 25, 50):
            conc[str(c)] = _level(base, f"/api/v1/projects/{pid}/conversations?archived=1",
                                  c, max(10, args.repeat // 2))
        out["results"]["concurrency_list_conversations"] = conc

        server.stop()
        llm.shutdown()

    payload = json.dumps(out, indent=2, ensure_ascii=False)
    print(payload)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
