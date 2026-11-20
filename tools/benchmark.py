#!/usr/bin/env python3
"""NEXUS benchmarks: reproducible measurements of the real request paths.

This is not a synthetic micro-benchmark. It starts the real web server against
a real PostgreSQL and the same stand-in OpenHands/GitHub/LLM servers the test
suite uses, then measures the paths the requirement names: opening the studio,
listing projects and conversations, fetching a message history, a chat turn's
API latency, a full autonomous run's planning and dispatch, file operations and
the store's own query costs.

Every number is a real round trip; nothing is estimated. The output is JSON on
stdout (and to ``--out``), so a run before and after a change can be compared
with ``tools/benchmark_compare.py``.

Usage::

    TEST_SUPABASE_DB_URL=postgresql://user:pw@host/db \\
      python tools/benchmark.py --out before.json

A database that is reset per run is used (the migrations are applied to a fresh
schema), so the numbers are not polluted by earlier data.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_studio_api import _reset_schema  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402
from tests.fake_github import FakeGitHub, FakeGitHubServer  # noqa: E402
from tests.fake_runtime import FakeServer  # noqa: E402
from tests.test_providers import FakeProvider  # noqa: E402

TOKEN = "studio-machine-token"

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.github_client import GitHubClient  # noqa: E402
from orchestration.nexus_chat import ChatResponder  # noqa: E402
from orchestration.nexus_planner import Planner  # noqa: E402
from orchestration.providers import GroqProvider, ProviderCascade  # noqa: E402
from orchestration.web_auth import hash_password  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402

import threading  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402


def _percentiles(samples: list[float]) -> dict:
    samples = sorted(samples)
    def at(p):
        if not samples:
            return 0.0
        idx = min(len(samples) - 1, int(round(p * (len(samples) - 1))))
        return round(samples[idx], 2)
    return {"n": len(samples), "min": round(min(samples), 2) if samples else 0.0,
            "p50": at(0.50), "p90": at(0.90), "p99": at(0.99),
            "mean": round(statistics.fmean(samples), 2) if samples else 0.0,
            "max": round(max(samples), 2) if samples else 0.0}


class Client:
    """A tiny JSON-over-HTTP client with the machine token."""

    def __init__(self, base: str):
        self.base = base

    def __call__(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {TOKEN}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw else None

    def get(self, path):
        return self("GET", path)

    def get_raw(self, path, *, headers=None):
        """A raw GET: the bytes and the Content-Encoding, for a static asset."""
        request = urllib.request.Request(self.base + path, method="GET")
        request.add_header("Authorization", f"Bearer {TOKEN}")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read(), response.headers.get("Content-Encoding")

    def post(self, path, body=None):
        return self("POST", path, {} if body is None else body)


def _time(fn, *, repeat: int = 1) -> list[float]:
    out = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        out.append((time.perf_counter() - start) * 1000.0)   # milliseconds
    return out


def _queue_claim_ms(dsn: str, *, repeat: int = 15) -> list[float]:
    """How long a due lab task waits before a worker claims it (one run's worth).

    Real PostgreSQL and the real ``LabStore``: a durable run is started, a task
    is added, and the time from "the task is due" to "the claim took it" is
    measured. This is the queue latency; a task that stays due is the
    "running + pending" symptom the diagnostics surface.
    """
    from orchestration.cloud_store import PostgresStore
    from orchestration.nexus_lab_store import LabStore
    from orchestration.nexus_projects import ProjectService

    store = PostgresStore(dsn)
    try:
        projects = ProjectService(store)
        project = str(projects.create("machine:bench", "Bench")["id"])
        lab = LabStore(store)
        timings: list[float] = []
        for i in range(repeat):
            goal = str(projects.create_goal("machine:bench", project, f"g{i}")[0]["id"])
            run = lab.start_run("machine:bench", project, goal, policy="durable")
            lab.add_task("machine:bench", project, run["id"], task_key="t", title="bench",
                         repository="", branch="")
            start = time.perf_counter()
            claimed = lab.claim_task(str(run["id"]), f"bench-{i}")
            timings.append((time.perf_counter() - start) * 1000.0)
            if claimed is not None:
                lab.release_task(claimed["id"], error="bench", requeue=False,
                                 lease_token=claimed["lease_token"])
            lab.set_run_state_internal(str(run["id"]), "cancelled")
        return timings
    finally:
        store.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--repeat", type=int, default=15,
                    help="how many times each read path is measured")
    ap.add_argument("--projects", type=int, default=5)
    ap.add_argument("--conversations", type=int, default=20,
                    help="conversations seeded per project")
    ap.add_argument("--messages", type=int, default=50,
                    help="messages seeded per conversation")
    args = ap.parse_args()

    dsn = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
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

    results: dict = {"environment": {}, "measurements": {}}

    github = FakeGitHub()
    llm = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    llm.script, llm.calls, llm.index = [{"payload": {"choices": [
        {"message": {"content": "Ответ."}}]}}], [], 0
    threading.Thread(target=llm.serve_forever, daemon=True).start()

    with (FakeCloudServer() as cloud, FakeGitHubServer(github) as github_server,
          FakeServer()):
        app = App(dsn, poll_interval=0.2, token=TOKEN, lab_inline=False)
        app.client = CloudClient(cloud.base_url, api_key="bench-key", timeout=10.0)
        app.dispatcher.client = app.client
        app.runtime.client = app.client
        app.verifier.client = app.client
        app.github = GitHubClient(base_url=github_server.base_url, token="gh-bench")
        app.github_accounts._client_factory = lambda token: GitHubClient(
            base_url=github_server.base_url, token=token)
        app.providers = ProviderCascade([GroqProvider(
            base_url=f"http://127.0.0.1:{llm.server_port}", api_key="k",
            model="bench-model", sleep=lambda _s: None)])
        app.chat = ChatResponder(app.providers, dispatch_code=app._chat_dispatch_code,
                                 start_autonomous=app._chat_start_autonomous)
        app.planner = Planner(app.providers)
        server = WebServer(app, "127.0.0.1", 0)
        server.start()
        api = Client(f"http://127.0.0.1:{server.port}")

        # Seed data: several projects, each with conversations and messages.
        project_ids, conversation_ids = [], []
        seed_start = time.perf_counter()
        for _p in range(args.projects):
            project = api.post("/api/v1/projects", {"name": "Bench"})["project"]["id"]
            project_ids.append(project)
            api.post(f"/api/v1/projects/{project}/repository", {"full_name": "Pavel1778/python-dispetcher"})
            for _c in range(args.conversations):
                conv = api.post(f"/api/v1/projects/{project}/conversations",
                                {"title": "Chat", "mode": "ask"})["conversation"]["id"]
                conversation_ids.append((project, conv))
                with app.store._tx() as cur:
                    for i in range(args.messages):
                        cur.execute(
                            "INSERT INTO nexus_conversation_messages"
                            "(conversation_id, role, content) VALUES (%s,%s,%s)",
                            (conv, "user" if i % 2 == 0 else "assistant",
                             f"Сообщение номер {i} " + "текст " * 20))
        results["environment"]["seed_seconds"] = round(time.perf_counter() - seed_start, 2)
        results["environment"]["projects"] = len(project_ids)
        results["environment"]["conversations"] = len(conversation_ids)
        results["environment"]["messages_per_conversation"] = args.messages
        results["environment"]["python"] = sys.version.split()[0]

        pid = project_ids[0]
        cid = conversation_ids[0][1]

        # --- the read paths the requirement names ---
        m = results["measurements"]
        m["studio_status_ms"] = _percentiles(_time(lambda: api.get("/api/studio"), repeat=args.repeat))
        m["list_projects_ms"] = _percentiles(_time(lambda: api.get("/api/v1/projects"), repeat=args.repeat))
        m["open_project_ms"] = _percentiles(_time(lambda: api.get(f"/api/v1/projects/{pid}"), repeat=args.repeat))
        m["list_conversations_ms"] = _percentiles(_time(
            lambda: api.get(f"/api/v1/projects/{pid}/conversations?archived=1"), repeat=args.repeat))
        m["open_conversation_ms"] = _percentiles(_time(
            lambda: api.get(f"/api/v1/projects/{pid}/conversations/{cid}"), repeat=args.repeat))
        m["fetch_history_ms"] = _percentiles(_time(
            lambda: api.get(f"/api/v1/projects/{pid}/conversations/{cid}/messages"),
            repeat=args.repeat))
        m["commands_registry_ms"] = _percentiles(_time(lambda: api.get("/api/commands"), repeat=args.repeat))
        # A studio asset fetched the way a browser fetches it (gzip accepted).
        # It is a static file, so its cost is the server's own: this is what the
        # precomputed gzip body is measured by.
        m["static_asset_ms"] = _percentiles(_time(
            lambda: api.get_raw("/static/app.js", headers={"Accept-Encoding": "gzip"}),
            repeat=args.repeat))
        m["search_by_content_ms"] = _percentiles(_time(
            lambda: api.get(f"/api/v1/projects/{pid}/conversations?q="
                            + urllib.parse.quote("номер") + "&archived=1"),
            repeat=args.repeat))
        m["export_conversation_ms"] = _percentiles(_time(
            lambda: api.get(f"/api/v1/projects/{pid}/conversations/{cid}/export"),
            repeat=args.repeat))
        # The evidence viewer's two calls. A list over an empty project is the
        # cost the Agent tab pays when it opens the section.
        m["evidence_list_ms"] = _percentiles(_time(
            lambda: api.get(f"/api/v1/projects/{pid}/evidence"),
            repeat=args.repeat))

        # --- a chat turn (the LLM stand-in answers immediately) ---
        def chat_turn():
            api.post(f"/api/v1/projects/{pid}/conversations/{cid}/messages",
                     {"text": "Как устроен модуль?", "mode": "ask"})
        m["chat_turn_ms"] = _percentiles(_time(chat_turn, repeat=max(3, args.repeat // 3)))

        # --- planning + autonomous run start (through the chat) ---
        plan_json = json.dumps({"tasks": [
            {"id": "a", "title": "Шаг A", "description": "сделать A",
             "acceptance": ["ок"], "dependencies": []},
            {"id": "b", "title": "Шаг B", "description": "сделать B",
             "acceptance": ["ок"], "dependencies": ["a"]}]})

        def start_run():
            llm.script = [{"payload": {"choices": [{"message": {"content": plan_json}}]}}]
            llm.index = 0
            conv = api.post(f"/api/v1/projects/{pid}/conversations",
                            {"title": "", "mode": "autonomous"})["conversation"]["id"]
            api.post(f"/api/v1/projects/{pid}/conversations/{conv}/messages",
                     {"text": "Исправь найденные ошибки"})
        m["start_run_ms"] = _percentiles(_time(start_run, repeat=5))

        # --- the store's own query costs, directly ---
        store = PostgresStore(dsn)
        m["store_dashboard_counts_ms"] = _percentiles(_time(
            lambda: store.dashboard_counts(), repeat=args.repeat))

        # --- background operations: a file upload + read ---
        def upload_and_read():
            import uuid as _uuid
            # The opaque upload route: a raw body with the name in a header.
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.port}/api/v1/projects/{pid}/files",
                data=b"x" * 4096, method="POST")
            req.add_header("Authorization", f"Bearer {TOKEN}")
            req.add_header("Content-Type", "application/octet-stream")
            req.add_header("X-File-Name", "bench-" + _uuid.uuid4().hex + ".txt")
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
        m["file_upload_ms"] = _percentiles(_time(upload_and_read, repeat=max(3, args.repeat // 3)))

        # --- the lab queue: how long a due task waits to be claimed, and how
        #     long the claim itself takes. This is the "queue latency" the
        #     reliability work is about: a task that is due and unclaimed is the
        #     "running + pending" symptom.
        m["queue_claim_ms"] = _percentiles(_queue_claim_ms(dsn, repeat=max(5, args.repeat)))

        server.stop()
        llm.shutdown()

    payload = json.dumps(results, indent=2, ensure_ascii=False)
    print(payload)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        print(f"\nwritten to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
