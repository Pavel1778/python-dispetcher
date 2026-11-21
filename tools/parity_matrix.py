"""Count the *real* Python↔Rust parity comparisons and write a matrix.

The mission forbids claiming a case count that is not backed by an actual
comparison, and forbids counting duplicated parameter combinations as
independent cases. This tool is the honest counter:

* it runs the built ``nexus-rs`` binary and the Python authority on the **same**
  inputs, the way ``tests/test_rust_parity.py`` does, and records one row per
  input it actually compared;
* a row is *meaningful* only when the two sides are compared and the case is not
  a byte-identical duplicate of another row in the same area (a repeated
  ``(from, to)`` pair, a repeated retry input, and so on);
* an area with no Rust counterpart is recorded ``NOT IMPLEMENTED`` / ``NOT
  COMPARABLE`` rather than counted.

Usage::

    python -m tools.parity_matrix              # print the summary
    python -m tools.parity_matrix --json out.json

Exit code 0 when every compared case agrees, 1 when any disagrees (so it can be
a gate), 2 when the Rust toolchain is absent (nothing was compared).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from orchestration import cloud_store as cp  # noqa: E402
from orchestration import nexus_planner, nexus_retry, nexus_workflow  # noqa: E402

_RUST = os.path.join(_ROOT, "rust")


def _rust_bin() -> str | None:
    override = os.environ.get("NEXUS_RUST_BIN")
    if override:
        return override
    if shutil.which("cargo") is None:
        return None
    if not hasattr(_rust_bin, "_path"):
        subprocess.run(["cargo", "build", "--quiet"], cwd=_RUST, check=True,
                       env={**os.environ, "CARGO_TERM_COLOR": "never"})
        _rust_bin._path = os.path.join(_RUST, "target", "debug", "nexus-rs")
    return _rust_bin._path


def _parity(binary: str, payload: dict) -> dict:
    proc = subprocess.run([binary, "parity"], input=json.dumps(payload).encode(),
                          capture_output=True, check=True)
    return json.loads(proc.stdout.decode())


class Matrix:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, area: str, case: str, *, compared: bool, agree: bool | None,
            note: str = "") -> None:
        self.rows.append({"area": area, "case": case, "compared": compared,
                          "agree": agree, "note": note})

    def summary(self) -> dict:
        compared = [r for r in self.rows if r["compared"]]
        return {
            "total_rows": len(self.rows),
            "compared": len(compared),
            "agree": sum(1 for r in compared if r["agree"] is True),
            "disagree": sum(1 for r in compared if r["agree"] is False),
            "not_implemented": sum(1 for r in self.rows if r["note"] == "NOT IMPLEMENTED"),
            "not_comparable": sum(1 for r in self.rows if r["note"] == "NOT COMPARABLE"),
        }


RUN_STATES = ("running", "paused", "waiting_for_approval", "blocked",
              "completed", "failed", "cancelled")
WORKFLOW_STATES = tuple(nexus_workflow.STATES)
TASK_STATES = tuple(s.value for s in cp.TaskState)
_RUN_ENTERED_FROM = getattr(__import__(
    "orchestration.nexus_lab_store", fromlist=["_RUN_ENTERED_FROM"]), "_RUN_ENTERED_FROM")


def _py_run_transition(frm: str, to: str) -> bool:
    return frm == to or frm in _RUN_ENTERED_FROM.get(to, frozenset())


def _py_task_transition(frm: str, to: str) -> bool:
    return cp.TaskState(to) in cp._ALLOWED.get(cp.TaskState(frm), frozenset())


def collect(binary: str) -> Matrix:
    m = Matrix()

    # -- run transitions (exhaustive, distinct answers only) ----------------
    cases = [(f, t) for f in RUN_STATES for t in RUN_STATES]
    out = _parity(binary, {"run_transitions": cases})["run_transitions"]
    seen = set()
    for (f, t), got in zip(cases, out):
        exp = _py_run_transition(f, t)
        key = (f, t, exp)
        if key in seen:
            continue
        seen.add(key)
        m.add("run transitions", f"{f}->{t}", compared=True, agree=(got == exp))

    # -- workflow transitions ----------------------------------------------
    cases = [[f, t] for f in (None, *WORKFLOW_STATES) for t in WORKFLOW_STATES]
    out = _parity(binary, {"workflow_transitions": cases})["workflow_transitions"]
    seen = set()
    for (f, t), got in zip(cases, out):
        exp = nexus_workflow.can_transition(f if f is not None else "DISCOVERY", t)
        key = (f, t, exp)
        if key in seen:
            continue
        seen.add(key)
        m.add("workflow transitions", f"{f}->{t}", compared=True, agree=(got == exp))

    # -- control-plane transitions -----------------------------------------
    cases = [(f, t) for f in TASK_STATES for t in TASK_STATES]
    out = _parity(binary, {"task_transitions": cases})["task_transitions"]
    seen = set()
    for (f, t), got in zip(cases, out):
        exp = _py_task_transition(f, t)
        key = (f, t, exp)
        if key in seen:
            continue
        seen.add(key)
        m.add("control-plane transitions", f"{f}->{t}", compared=True, agree=(got == exp))

    # -- plan validation ----------------------------------------------------
    valid = [
        {"tasks": [{"id": "t1", "title": "one"}]},
        {"tasks": [{"id": "t2", "title": " two ", "dependencies": ["t1"]},
                   {"id": "t1", "title": "one", "acceptance": "criterion"}]},
        {"tasks": [{"id": "a", "title": "a", "acceptance": ["x", "y"],
                    "description": "d" * 5000, "kind": "bugfix"}]},
        [{"id": "solo", "title": "solo"}],
    ]
    invalid = [
        {"tasks": []}, {},
        {"tasks": [{"id": "has space", "title": "x"}]},
        {"tasks": [{"id": "a", "title": ""}]},
        {"tasks": [{"id": "a", "title": "a", "dependencies": ["a"]}]},
        {"tasks": [{"id": "a", "title": "a", "dependencies": ["b"]},
                   {"id": "b", "title": "b", "dependencies": ["a"]}]},
        {"tasks": [{"id": "a", "title": "a", "dependencies": ["ghost"]}]},
        {"tasks": [{"id": "a", "title": "a", "acceptance": [1]}]},
        {"tasks": [{"id": "a", "title": "a", "dependencies": "t1"}]},
        {"tasks": [{"id": "a", "title": "a"}, {"id": "a", "title": "b"}]},
        {"tasks": [1]},
    ]
    out = _parity(binary, {"plans": valid + invalid})["plans"]
    for i, plan in enumerate(valid):
        py = nexus_planner.validate_tasks(plan)
        got = out[i]
        ok = got["ok"] is True and all(
            rs["id"] == p["id"] and rs["title"] == p["title"]
            and rs["description"] == p["description"]
            and rs["acceptance"] == p["acceptance"]
            and rs["dependencies"] == p["dependencies"]
            for p, rs in zip(py, got["tasks"]))
        m.add("plan validation", f"valid[{i}]", compared=True, agree=ok)
    for j, plan in enumerate(invalid):
        got = out[len(valid) + j]
        try:
            nexus_planner.validate_tasks(plan)
            py_ok = True
        except ValueError:
            py_ok = False
        m.add("plan validation", f"invalid[{j}]", compared=True,
              agree=(got["ok"] is False and not py_ok))

    # -- plan hash ----------------------------------------------------------
    out = _parity(binary, {"plan_hashes": valid})["plan_hashes"]
    for plan, got in zip(valid, out):
        exp = nexus_workflow.plan_hash_of(nexus_planner.validate_tasks(plan))
        m.add("plan hash", f"hash[{valid.index(plan)}]", compared=True, agree=(got == exp))

    # -- retry arithmetic ---------------------------------------------------
    cycles = [{"cycle": c, "seed": s, "base": 60, "ceiling": 3600}
              for c in (1, 2, 3, 5, 10, 40) for s in ("", "task-1", "task-abc:7")]
    refused = [
        {"retry_after": None, "daily": False, "refusals": 0},
        {"retry_after": None, "daily": True, "refusals": 0},
        {"retry_after": None, "daily": False, "refusals": 3},
        {"retry_after": 120.0, "daily": False, "refusals": 0},
        {"retry_after": 1.0, "daily": False, "refusals": 0},
        {"retry_after": 99999.0, "daily": True, "refusals": 5},
    ]
    out = _parity(binary, {"cycle_waits": cycles, "refused_waits": refused})
    for c, got in zip(cycles, out["cycle_waits"]):
        exp = nexus_retry.cycle_wait(c["cycle"], seed=c["seed"], base=c["base"],
                                     ceiling=c["ceiling"])
        m.add("retry arithmetic", f"cycle {c['cycle']} seed={c['seed'] or '-'}",
              compared=True, agree=(abs(got - exp) < 1e-9))
    for c, got in zip(refused, out["refused_waits"]):
        exp = nexus_retry.refused_wait(retry_after=c["retry_after"], daily=c["daily"],
                                       refusals=c["refusals"])
        m.add("retry arithmetic", f"refused {c['retry_after']} daily={c['daily']} "
              f"refusals={c['refusals']}", compared=True, agree=(abs(got - exp) < 1e-9))

    # -- areas with no Rust counterpart (honest, not counted) --------------
    for area in ("SQL claim (FOR UPDATE SKIP LOCKED)",
                 "lease reclaim (reclaim_expired)",
                 "durable dispatch journal",
                 "evidence signing",
                 "HTTP API surface",
                 "OpenHands transport",
                 "intent router (language-sensitive)",
                 "task-kind derivation (language-sensitive)"):
        m.add(area, area, compared=False, agree=None,
              note="NOT IMPLEMENTED" if "claim" not in area else "NOT COMPARABLE")
    return m


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", help="write the full matrix here")
    args = ap.parse_args(argv)

    binary = _rust_bin()
    if binary is None:
        print("no Rust toolchain (cargo/nexus-rs absent): nothing compared", file=sys.stderr)
        return 2
    matrix = collect(binary)
    summary = matrix.summary()
    print(json.dumps(summary, indent=2))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"summary": summary, "rows": matrix.rows}, fh, indent=2)
        print(f"matrix written to {args.json}")
    return 0 if summary["disagree"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
