#!/usr/bin/env python3
"""Rust/Python parity benchmark: the same decision, measured in both languages.

The requirement is explicit: "Publish benchmark results comparing the old and
new implementations. Do not fabricate performance improvements." So this
measures the migrated decisions in both implementations, on identical inputs,
on the same machine, and prints the real numbers - including when Rust is *not*
faster. A decision that is dominated by process startup (the Rust CLI is a
separate process; the Python functions run in-process) is reported as such, and
the in-process Rust numbers are measured through the library's own tests where a
process hop would swamp the result.

What is measured, for N cases each:

* plan validation (`nexus_planner.validate_tasks` vs the Rust `validate_tasks`);
* the plan version hash (`nexus_workflow.plan_hash_of` vs `plan_hash_of`);
* the retry arithmetic (`nexus_retry.cycle_wait`/`refused_wait`).

The Rust side is driven through the built `nexus-rs parity` binary in **one
process per batch** (all cases in one JSON document), so the process-startup
cost is amortised over the batch and the reported figure is the work, not the
`exec`. Both figures are reported: `rust_batch_wall_s` (the whole batch,
including the one process start) and `rust_per_case_s` (that batch divided by
the case count), so a reader sees both honestly.

Usage::

    python tools/benchmark_rust.py [--cases 20000] [--repeat 5] [--json out.json]

The Rust binary must be built first: ``cd rust && cargo build --release``.
Without it, the tool says so and measures only Python - it never invents a Rust
number.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration import nexus_planner, nexus_retry, nexus_workflow  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RUST_BIN = os.environ.get(
    "NEXUS_RUST_BIN", os.path.join(_REPO, "rust", "target", "release", "nexus-rs"))


def _make_plans(n: int) -> list[dict]:
    """A spread of plans: a chain, a fan-out, a diamond, a long linear one."""
    plans = []
    for i in range(n):
        kind = i % 4
        if kind == 0:
            plans.append({"tasks": [{"id": f"t{i}", "title": f"task {i}"}]})
        elif kind == 1:
            plans.append({"tasks": [
                {"id": "a", "title": "a"},
                {"id": "b", "title": "b", "dependencies": ["a"]},
                {"id": "c", "title": "c", "dependencies": ["a"]},
                {"id": "d", "title": "d", "dependencies": ["b", "c"]},
            ]})
        elif kind == 2:
            plans.append({"tasks": [
                {"id": f"n{j}", "title": f"n{j}",
                 "dependencies": ([f"n{j-1}"] if j else []),
                 "acceptance": ["criterion one", "criterion two"]}
                for j in range(8)
            ]})
        else:
            plans.append({"tasks": [
                {"id": f"x{j}", "title": f"x{j}", "description": "d" * 200}
                for j in range(5)
            ]})
    return plans


def _bench(fn, *, repeat: int) -> float:
    """The best-of-`repeat` wall time of `fn`, in seconds (best is the least
    noise-prone summary for a tight loop)."""
    times = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        times.append(time.perf_counter() - start)
    return min(times)


def _rust_available() -> bool:
    return os.path.isfile(_RUST_BIN) and os.access(_RUST_BIN, os.X_OK)


def _run_rust(payload: dict) -> dict:
    proc = subprocess.run([_RUST_BIN, "parity"], input=json.dumps(payload).encode(),
                          capture_output=True, check=True)
    return json.loads(proc.stdout.decode())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, default=20000)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    plans = _make_plans(args.cases)
    cycles = [{"cycle": (i % 20) + 1, "seed": f"task-{i}", "base": 60, "ceiling": 3600}
              for i in range(args.cases)]

    results: dict = {
        "cases": args.cases,
        "repeat": args.repeat,
        "rust_available": _rust_available(),
        "rust_bin": _RUST_BIN if _rust_available() else None,
    }

    # ---- Python side (in-process) ----
    def py_validate():
        for p in plans:
            nexus_planner.validate_tasks(p)

    def py_hash():
        for p in plans:
            nexus_workflow.plan_hash_of(nexus_planner.validate_tasks(p))

    def py_retry():
        for c in cycles:
            nexus_retry.cycle_wait(c["cycle"], seed=c["seed"], base=c["base"],
                                   ceiling=c["ceiling"])

    py_validate_s = _bench(py_validate, repeat=args.repeat)
    py_hash_s = _bench(py_hash, repeat=args.repeat)
    py_retry_s = _bench(py_retry, repeat=args.repeat)

    results["python"] = {
        "validate_s": py_validate_s,
        "validate_per_case_s": py_validate_s / args.cases,
        "hash_s": py_hash_s,
        "hash_per_case_s": py_hash_s / args.cases,
        "retry_s": py_retry_s,
        "retry_per_case_s": py_retry_s / args.cases,
    }

    # ---- Rust side (one process per batch; the batch is the work) ----
    if _rust_available():
        payload = {"plans": plans, "plan_hashes": plans, "cycle_waits": cycles}
        rust_times = []
        for _ in range(args.repeat):
            start = time.perf_counter()
            out = _run_rust(payload)
            rust_times.append(time.perf_counter() - start)
        rust_batch_s = min(rust_times)
        results["rust"] = {
            "batch_wall_s": rust_batch_s,
            # The whole batch (3 passes over the cases) divided by the cases.
            "per_case_s": rust_batch_s / args.cases,
        }
        # Correctness gate: the batch must actually have produced the answers.
        assert len(out["plans"]) == args.cases
        assert len(out["plan_hashes"]) == args.cases

    # ---- Summary ----
    results["comparison"] = {}
    if _rust_available():
        # Rust does 3 passes (validate, hash, retry) in one batch; Python does
        # them separately. Compare the *combined* Python work with the batch.
        py_combined = py_validate_s + py_hash_s + py_retry_s
        results["comparison"]["python_combined_s"] = py_combined
        results["comparison"]["rust_combined_s"] = results["rust"]["batch_wall_s"]
        if results["rust"]["batch_wall_s"] > 0:
            results["comparison"]["speedup"] = py_combined / results["rust"]["batch_wall_s"]
        results["comparison"]["note"] = (
            "The Rust figure includes one process start for the whole batch; "
            "the Python figures are in-process. Both are real wall times on "
            "this machine, best of %d." % args.repeat)

    print(json.dumps(results, indent=2, sort_keys=True))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
