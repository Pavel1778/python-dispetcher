#!/usr/bin/env python3
"""End-to-end user-scenario benchmark (Phase 9).

Drives the real server through the user journeys the program names, with the
stand-in OpenHands/GitHub/LLM servers (no real external load), and prints the
per-scenario latency. It separates NEXUS-side time from the stand-in external
time where it can.

Usage:
  TEST_SUPABASE_DB_URL=postgresql://... python benchmarks/e2e_scenarios.py --out e2e.json
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
from orchestration.github_client import GitHubClient  # noqa: E402
from orchestration.nexus_chat import ChatResponder  # noqa: E402
from orchestration.nexus_planner import Planner  # noqa: E402
from orchestration.providers import GroqProvider, ProviderCascade  # noqa: E402
from orchestration.web_auth import hash_password  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402

TOKEN = "studio-machine-token"


def pct(samples):
    s = sorted(samples)
    return {"n": len(s), "p50": round(s[len(s) // 2], 2) if s else 0.0,
            "p95": round(s[min(len(s) - 1, int(0.95 * len(s)))], 2) if s else 0.0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
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

    github = FakeGitHub()
    llm = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    llm.script, llm.calls, llm.index = [{"payload": {"choices": [
        {"message": {"content": "ok"}}]}}], [], 0
    threading.Thread(target=llm.serve_forever, daemon=True).start()

    out: dict = {"environment": {"python": sys.version.split()[0]}, "scenarios": {}}
    with (FakeCloudServer() as cloud, FakeGitHubServer(github) as gh, FakeServer()):
        app = App(dsn, poll_interval=0.2, token=TOKEN, lab_inline=False)
        app.client = CloudClient(cloud.base_url, api_key="k", timeout=10.0)
        app.dispatcher.client = app.client
        app.runtime.client = app.client
        app.verifier.client = app.client
        app.github = GitHubClient(base_url=gh.base_url, token="gh")
        app.github_accounts._client_factory = lambda t: GitHubClient(base_url=gh.base_url, token=t)
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
            t0 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode()
            return (time.perf_counter() - t0) * 1000.0, (json.loads(raw) if raw else None)

        def timed(name, fn, repeat=10):
            fn()  # warm
            out["scenarios"][name] = pct([fn() for _ in range(repeat)])

        # 1. open dashboard / list projects
        _, proj = api("POST", "/api/v1/projects", {"name": "E2E"})
        pid = proj["project"]["id"]
        api("POST", f"/api/v1/projects/{pid}/repository",
            {"full_name": "Pavel1778/python-dispetcher"})
        timed("list_projects", lambda: api("GET", "/api/v1/projects")[0])
        timed("open_dashboard", lambda: api("GET", "/api/studio")[0])

        # 2. open a conversation / fetch history
        _, conv = api("POST", f"/api/v1/projects/{pid}/conversations",
                      {"title": "E2E", "mode": "ask"})
        cid = conv["conversation"]["id"]
        for i in range(30):
            with app.store._tx() as cur:
                cur.execute("INSERT INTO nexus_conversation_messages(conversation_id,role,content)"
                            " VALUES (%s,%s,%s)", (cid, "user", f"m{i} " + "t " * 20))
        timed("open_conversation", lambda: api("GET",
              f"/api/v1/projects/{pid}/conversations/{cid}")[0])
        timed("fetch_history", lambda: api("GET",
              f"/api/v1/projects/{pid}/conversations/{cid}/messages")[0])

        # 3. send a chat turn (stand-in LLM)
        timed("send_message", lambda: api("POST",
              f"/api/v1/projects/{pid}/conversations/{cid}/messages",
              {"text": "Привет", "mode": "ask"})[0], repeat=5)

        # 4. start an autonomous run (stand-in planner)
        plan = json.dumps({"tasks": [
            {"id": "a", "title": "A", "description": "a", "acceptance": ["ok"], "dependencies": []},
            {"id": "b", "title": "B", "description": "b", "acceptance": ["ok"], "dependencies": ["a"]}]})

        def start_run():
            llm.script = [{"payload": {"choices": [{"message": {"content": plan}}]}}]
            llm.index = 0
            _, c = api("POST", f"/api/v1/projects/{pid}/conversations",
                       {"title": "", "mode": "autonomous"})
            return api("POST", f"/api/v1/projects/{pid}/conversations/"
                       f"{c['conversation']['id']}/messages", {"text": "Исправь ошибки"})[0]
        timed("start_run", start_run, repeat=3)

        # 5. task status / notifications / files
        _, runs = api("GET", f"/api/v1/projects/{pid}/runs")
        if runs.get("runs"):
            rid = runs["runs"][0]["id"]
            timed("run_status", lambda: api("GET", f"/api/v1/projects/{pid}/runs/{rid}")[0])
        timed("notifications", lambda: api("GET", f"/api/v1/projects/{pid}/notifications")[0])
        timed("upload_file", lambda: _upload(base, pid))

        server.stop()
        llm.shutdown()

    payload = json.dumps(out, indent=2, ensure_ascii=False)
    print(payload)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    return 0


def _upload(base: str, pid: str) -> float:
    import uuid
    req = urllib.request.Request(base + f"/api/v1/projects/{pid}/files",
                                 data=b"x" * 4096, method="POST")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Content-Type", "application/octet-stream")
    req.add_header("X-File-Name", "e2e-" + uuid.uuid4().hex + ".txt")
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()
    return (time.perf_counter() - t0) * 1000.0


if __name__ == "__main__":
    raise SystemExit(main())
