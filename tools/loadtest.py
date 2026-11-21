#!/usr/bin/env python3
"""A dependency-free load generator for a running NEXUS server.

``tools/benchmark.py`` measures each path once at a time, in-process, against
the stand-in services; it answers "how long does one call take". This answers a
different question: "what does a *running* server do under concurrent clients,
over real sockets" - latency percentiles and throughput, with the server started
the way a deployment starts it.

There is no ``wrk``/``ab`` in this environment, so the generator is stdlib only:
a pool of threads, each looping over a mix of requests, recording every latency
with ``perf_counter`` and counting non-2xx answers. It never sleeps between
requests, so the reported throughput is the server's, not the client's.

Usage::

    python tools/loadtest.py --base http://127.0.0.1:12000 --token TOKEN \\
        --clients 16 --duration 15 --mix reads

The mix presets: ``reads`` (what the studio polls), ``writes`` (a chat turn's
API path), ``mixed`` (both, as a person clicking around produces).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

#: A request the generator can make: method, path, body, and a weight.
@dataclass(frozen=True)
class Call:
    method: str
    path: str
    body: dict | None = None
    weight: float = 1.0


@dataclass
class Samples:
    """Everything one run recorded, per call, so a report can split by path."""
    lock: threading.Lock = field(default_factory=threading.Lock)
    by_call: dict[str, list[float]] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)
    total: int = 0

    def record(self, key: str, seconds: float, ok: bool) -> None:
        with self.lock:
            self.by_call.setdefault(key, []).append(seconds * 1000.0)
            self.total += 1
            if not ok:
                self.errors[key] = self.errors.get(key, 0) + 1


def _percentiles(samples: list[float]) -> dict:
    samples = sorted(samples)
    def at(p: float) -> float:
        if not samples:
            return 0.0
        return round(samples[min(len(samples) - 1, int(round(p * (len(samples) - 1))))], 2)
    return {"n": len(samples),
            "min": round(samples[0], 2) if samples else 0.0,
            "p50": at(0.50), "p90": at(0.90), "p95": at(0.95), "p99": at(0.99),
            "max": round(samples[-1], 2) if samples else 0.0,
            "mean": round(statistics.fmean(samples), 2) if samples else 0.0}


class Worker(threading.Thread):
    def __init__(self, base: str, token: str, plan: list[Call],
                 samples: Samples, stop: threading.Event, timeout: float):
        super().__init__(daemon=True)
        self.base, self.token, self.plan = base, token, plan
        self.samples, self.stop, self.timeout = samples, stop, timeout

    def _one(self, call: Call) -> None:
        data = json.dumps(call.body).encode() if call.body is not None else None
        request = urllib.request.Request(self.base + call.path, data=data,
                                         method=call.method)
        request.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        started = time.perf_counter()
        ok = False
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
                ok = 200 <= response.status < 300
        except (urllib.error.URLError, TimeoutError, OSError):
            ok = False
        finally:
            self.samples.record(f"{call.method} {call.path}", time.perf_counter() - started, ok)

    def run(self) -> None:
        i = 0
        while not self.stop.is_set():
            self._one(self.plan[i % len(self.plan)])
            i += 1


def _expand(plan: list[Call]) -> list[Call]:
    """Turn weights into a literal list so each worker can index it in a loop."""
    out: list[Call] = []
    for call in plan:
        out.extend([call] * max(1, int(round(call.weight * 10))))
    return out


def _mix(name: str, project_id: str, conversation_id: str) -> list[Call]:
    if name == "reads":
        return [
            Call("GET", "/api/studio", weight=3),
            Call("GET", "/api/v1/projects", weight=2),
            Call("GET", f"/api/v1/projects/{project_id}", weight=2),
            Call("GET", f"/api/v1/projects/{project_id}/conversations", weight=3),
            Call("GET", f"/api/v1/projects/{project_id}/conversations/{conversation_id}",
                 weight=2),
            Call("GET", f"/api/v1/projects/{project_id}/conversations/{conversation_id}/messages",
                 weight=2),
            Call("GET", "/api/commands", weight=1),
        ]
    if name == "writes":
        return [
            Call("POST", f"/api/v1/projects/{project_id}/conversations",
                 {"title": "load", "mode": "ask"}, weight=2),
            Call("POST", f"/api/v1/projects/{project_id}/conversations/{conversation_id}/messages",
                 {"text": "Привет, что нового?"}, weight=3),
        ]
    return (_mix("reads", project_id, conversation_id)
            + [Call("POST", f"/api/v1/projects/{project_id}/conversations/{conversation_id}/messages",
                    {"text": "Привет"}, weight=1)])


def _seed(base: str, token: str) -> tuple[str, str]:
    """A project and a conversation to read, so the reads are not 404s."""
    def call(method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(base + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")

    project = call("POST", "/api/v1/projects", {"name": "Load"})["project"]["id"]
    conversation = call("POST", f"/api/v1/projects/{project}/conversations",
                        {"title": "load", "mode": "ask"})["conversation"]["id"]
    return project, conversation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load-test a running NEXUS server")
    parser.add_argument("--base", default="http://127.0.0.1:12000")
    parser.add_argument("--token", required=True)
    parser.add_argument("--clients", type=int, default=16)
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--mix", choices=["reads", "writes", "mixed"], default="reads")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    project, conversation = _seed(args.base, args.token)
    plan = _expand(_mix(args.mix, project, conversation))

    samples = Samples()
    stop = threading.Event()
    workers = [Worker(args.base, args.token, plan, samples, stop, args.timeout)
               for _ in range(args.clients)]
    started = time.perf_counter()
    for worker in workers:
        worker.start()
    time.sleep(args.duration)
    stop.set()
    for worker in workers:
        worker.join(timeout=args.timeout)
    elapsed = time.perf_counter() - started

    report = {
        "mix": args.mix,
        "clients": args.clients,
        "elapsed_s": round(elapsed, 2),
        "requests": samples.total,
        "throughput_rps": round(samples.total / elapsed, 1) if elapsed else 0.0,
        "errors": sum(samples.errors.values()),
        "by_call": {key: {**_percentiles(values),
                          "errors": samples.errors.get(key, 0)}
                    for key, values in sorted(samples.by_call.items())},
        "overall": _percentiles([v for values in samples.by_call.values() for v in values]),
    }
    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    return 0 if report["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
