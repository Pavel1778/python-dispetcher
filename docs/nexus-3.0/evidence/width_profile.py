"""From either checkout, using the private postgres wrapper. No model, no provider call."""

import concurrent.futures as cf
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import secrets
import sys
import time
import urllib.request

sys.path.insert(0, str(Path.cwd()))
from benchmarks.forensic_workload import private_database  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402


def measure(fn, width):
    def timed(i):
        t = time.monotonic()
        result = fn(i)
        return (time.monotonic() - t) * 1000, result

    t = time.monotonic()
    with cf.ThreadPoolExecutor(width) as pool:
        rows = list(pool.map(timed, range(64)))
    rate = len(rows) / (time.monotonic() - t)
    ms = sorted(r[0] for r in rows)
    return dict(
        requests=len(ms),
        per_s=round(rate, 1),
        **{
            f"p{p}_ms": round(ms[math.ceil(len(ms) * p / 100) - 1], 3)
            for p in (50, 95, 99)
        },
    ), [r[1] for r in rows]


def main():
    dsn = private_database()
    os.environ["NEXUS_LAB_MAX_ACTIVE_TASKS"] = (
        "0"  # test queue capacity; not the deployed active-task limit
    )
    token = secrets.token_hex(24)
    app = App(dsn, token=token, lab_inline=False, poll_interval=3600)
    principal = "machine:default"
    pid = app.projects.create(principal, "load")["id"]
    samples = []
    with WebServer(app, host="127.0.0.1", port=0) as server:
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/api/v1/projects/{pid}",
            headers={"Authorization": "Bearer " + token},
        )

        def fetch(i):
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)["project"]["id"]

        for width in (1, 4, 16, 64):
            goal = app.projects.create_goal(principal, pid, "load " + str(width))[0][
                "id"
            ]
            run = app.lab.start_run(
                principal,
                pid,
                goal,
                max_iterations=64,
                plan=[{"id": str(i), "title": "read"} for i in range(64)],
            )["id"]
            before = app.store.pool_stats()
            db, grants = measure(
                lambda i: app.lab.claim_task(run, "w" + str(i), dispatch_intent=True),
                width,
            )
            assert len({g["id"] for g in grants}) == 64 and all(
                g["lease_token"] > 0 and g["_intent_id"] for g in grants
            )
            assert app.lab.claim_task(run, "extra") is None
            api, ids = measure(fetch, width)
            assert set(ids) == {str(pid)}
            after = app.store.pool_stats()
            samples.append(
                dict(
                    width=width,
                    claim=db,
                    http_member_read=api,
                    pool=after,
                    wait_delta_ms=(after["wait_ms"] or 0) - (before["wait_ms"] or 0),
                    peak_process_rss_kib=resource.getrusage(
                        resource.RUSAGE_SELF
                    ).ru_maxrss,
                )
            )
    import subprocess

    revision = (
        subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        if Path(".git").exists()
        else Path(".git-archive-commit").read_text().strip()
    )
    Path(sys.argv[1]).write_text(
        json.dumps(
            dict(
                revision=revision,
                harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                samples=samples,
            ),
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
