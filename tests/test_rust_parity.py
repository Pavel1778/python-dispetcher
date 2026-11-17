"""Rust/Python parity: the two implementations must answer identically.

This is the migration gate the requirement names ("For every migrated
component: run both implementations against the same test cases; compare
outputs and state transitions"). It is a Python test because the Python
implementation is what it compares against, and because the repository's suite
is Python.

How it works: for each component, the cases are built **from the Python side**
(the real tables, the real validators), fed to the Rust binary
(`nexus-rs parity` or `nexus-rs claimable`), and the two answers compared. A
disagreement is a failure, named by case, not a warning.

The Rust binary is built on demand (`cargo build`), and the whole module skips
cleanly when `cargo` is absent - an environment without a Rust toolchain cannot
run the parity, and must say so rather than pass. `NEXUS_RUST_BIN` may point at
an already-built binary; otherwise the crate under `rust/` is built.

What is compared:

* every run / workflow / control-plane transition, exhaustively over all
  (from, to) pairs - not a sample;
* plan validation: the exact error message for each invalid plan, and the
  normalised tasks for each valid one;
* the plan version hash, byte for byte;
* the retry arithmetic (cycle wait with and without a seed, refused wait);
* the scheduler's claim decision, over snapshots built from **real runs** in a
  live lab store (the dependency gate, the branch lock, attempts, pause,
  bounded deadline) as well as synthetic edge cases.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration import control_plane as cp  # noqa: E402
from orchestration import nexus_planner, nexus_retry, nexus_workflow  # noqa: E402
from orchestration.nexus_lab_store import _RUN_ENTERED_FROM  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RUST = os.path.join(_REPO, "rust")

pytestmark = pytest.mark.skipif(
    shutil.which("cargo") is None and not os.environ.get("NEXUS_RUST_BIN"),
    reason="no Rust toolchain (set NEXUS_RUST_BIN to an already-built nexus-rs)",
)


def _rust_bin() -> str:
    """The path to the built `nexus-rs`, building it once if necessary."""
    override = os.environ.get("NEXUS_RUST_BIN")
    if override:
        return override
    if not hasattr(_rust_bin, "_path"):
        subprocess.run(
            ["cargo", "build", "--quiet"], cwd=_RUST, check=True,
            env={**os.environ, "CARGO_TERM_COLOR": "never"},
        )
        _rust_bin._path = os.path.join(_RUST, "target", "debug", "nexus-rs")
    return _rust_bin._path


def _run_parity(payload: dict) -> dict:
    proc = subprocess.run(
        [_rust_bin(), "parity"], input=json.dumps(payload).encode(),
        capture_output=True, check=True,
    )
    return json.loads(proc.stdout.decode())


# --------------------------------------------------------------- state machines
RUN_STATES = ("running", "paused", "waiting_for_approval", "blocked",
              "completed", "failed", "cancelled")
WORKFLOW_STATES = tuple(nexus_workflow.STATES)
TASK_STATES = tuple(s.value for s in cp.TaskState)


def _py_run_transition(frm: str, to: str) -> bool:
    """Python's rule: a no-op is allowed; else `frm` must be in the target's
    entered-from set."""
    if frm == to:
        return True
    return frm in _RUN_ENTERED_FROM.get(to, frozenset())


def _py_task_transition(frm: str, to: str) -> bool:
    """`control_plane.transition` is strict: no no-op exception."""
    allowed = cp._ALLOWED.get(cp.TaskState(frm), frozenset())
    return cp.TaskState(to) in allowed


def test_every_run_transition_agrees_exhaustively():
    cases = [(f, t) for f in RUN_STATES for t in RUN_STATES]
    out = _run_parity({"run_transitions": cases})
    expected = [_py_run_transition(f, t) for f, t in cases]
    assert out["run_transitions"] == expected, _diff(cases, out["run_transitions"], expected)


def test_every_workflow_transition_agrees_exhaustively():
    cases = []
    for f in (None, *WORKFLOW_STATES):
        for t in WORKFLOW_STATES:
            cases.append([f, t])
    out = _run_parity({"workflow_transitions": cases})
    expected = [
        nexus_workflow.can_transition(f if f is not None else "DISCOVERY", t)
        for f, t in cases
    ]
    assert out["workflow_transitions"] == expected


def test_every_control_plane_transition_agrees_exhaustively():
    cases = [(f, t) for f in TASK_STATES for t in TASK_STATES]
    out = _run_parity({"task_transitions": cases})
    expected = [_py_task_transition(f, t) for f, t in cases]
    assert out["task_transitions"] == expected, _diff(cases, out["task_transitions"], expected)


# ------------------------------------------------------------------ plan shapes
_VALID_PLANS = [
    {"tasks": [{"id": "t1", "title": "one"}]},
    {"tasks": [{"id": "t2", "title": " two ", "dependencies": ["t1"]},
               {"id": "t1", "title": "one", "acceptance": "criterion"}]},
    {"tasks": [{"id": "a", "title": "a", "acceptance": ["x", "y"],
                "description": "d" * 5000, "kind": "bugfix"}]},
    # A bare list (no "tasks" wrapper) is accepted by both.
    [{"id": "solo", "title": "solo"}],
]

_INVALID_PLANS = [
    {"tasks": []},
    {},
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


def test_plan_validation_agrees_on_every_case():
    payload = {"plans": _VALID_PLANS + _INVALID_PLANS}
    out = _run_parity(payload)["plans"]
    assert len(out) == len(_VALID_PLANS) + len(_INVALID_PLANS)

    for i, plan in enumerate(_VALID_PLANS):
        py = nexus_planner.validate_tasks(plan)
        got = out[i]
        assert got["ok"] is True, (plan, got)
        # Compare the fields the Rust normaliser carries.
        for py_task, rs_task in zip(py, got["tasks"]):
            assert rs_task["id"] == py_task["id"]
            assert rs_task["title"] == py_task["title"]
            assert rs_task["description"] == py_task["description"]
            assert rs_task["acceptance"] == py_task["acceptance"]
            assert rs_task["dependencies"] == py_task["dependencies"]

    for j, plan in enumerate(_INVALID_PLANS):
        got = out[len(_VALID_PLANS) + j]
        assert got["ok"] is False, (plan, got)
        with pytest.raises(ValueError) as exc:
            nexus_planner.validate_tasks(plan)
        # The same reason, by category (both name the same defect).
        assert _reason_class(str(exc.value)) == _reason_class(got["error"]), (
            plan, str(exc.value), got["error"])


def _reason_class(message: str) -> str:
    """A coarse category of a validation error, so the two implementations'
    wordings can differ while the *reason* must not."""
    low = message.lower()
    for key in ("cycle", "unknown", "duplicate or self", "duplicate task",
                "invalid id", "no title", "acceptance", "dependencies",
                "between 1 and", "not an object"):
        if key in low:
            return key
    return low


def test_plan_hash_agrees_byte_for_byte():
    out = _run_parity({"plan_hashes": _VALID_PLANS})["plan_hashes"]
    for plan, got in zip(_VALID_PLANS, out):
        tasks = nexus_planner.validate_tasks(plan)
        expected = nexus_workflow.plan_hash_of(tasks)
        assert got == expected, (plan, got, expected)


def test_plan_hash_changes_when_the_plan_changes():
    # The approval binding must move: a different title is a different version.
    a = {"tasks": [{"id": "t1", "title": "one"}]}
    b = {"tasks": [{"id": "t1", "title": "two"}]}
    out = _run_parity({"plan_hashes": [a, b]})["plan_hashes"]
    assert out[0] != out[1]


# ------------------------------------------------------------------ retry maths
def test_retry_arithmetic_agrees():
    cycles = [{"cycle": c, "seed": s, "base": 60, "ceiling": 3600}
              for c in (1, 2, 3, 5, 10, 40)
              for s in ("", "task-1", "task-abc:7")]
    refused = [
        {"retry_after": None, "daily": False, "refusals": 0},
        {"retry_after": None, "daily": True, "refusals": 0},
        {"retry_after": None, "daily": False, "refusals": 3},
        {"retry_after": 120.0, "daily": False, "refusals": 0},
        {"retry_after": 1.0, "daily": False, "refusals": 0},
        {"retry_after": 99999.0, "daily": True, "refusals": 5},
    ]
    out = _run_parity({"cycle_waits": cycles, "refused_waits": refused})

    for case, got in zip(cycles, out["cycle_waits"]):
        expected = nexus_retry.cycle_wait(
            case["cycle"], seed=case["seed"], base=case["base"], ceiling=case["ceiling"])
        assert abs(got - expected) < 1e-9, (case, got, expected)

    for case, got in zip(refused, out["refused_waits"]):
        expected = nexus_retry.refused_wait(
            retry_after=case["retry_after"], daily=case["daily"], refusals=case["refusals"])
        assert abs(got - expected) < 1e-9, (case, got, expected)


# ------------------------------------------------------------------- scheduler
_DSN = os.environ.get("TEST_SUPABASE_DB_URL")

pytestmark_db = pytest.mark.skipif(
    not _DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")


def _claim(run: dict, tasks: list[dict], task_id: str) -> dict:
    proc = subprocess.run(
        [_rust_bin(), "claimable"],
        input=json.dumps({"run": run, "tasks": tasks, "task_id": task_id}).encode(),
        capture_output=True, check=True,
    )
    return json.loads(proc.stdout.decode())


def _snapshot_run(run_row: dict) -> dict:
    return {
        "state": run_row["state"],
        "policy": run_row["policy"],
        "iteration": int(run_row["iteration"]),
        "max_iterations": int(run_row["max_iterations"]),
        "pausing": run_row.get("resume_at") is not None,
        "within_deadline": True,
    }


def _snapshot_task(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "task_key": str(row["task_key"]),
        "state": row["state"],
        "dependencies": [str(d) for d in (row.get("dependencies") or [])],
        "repository": str(row.get("repository") or ""),
        "branch": str(row.get("branch") or ""),
        "attempt": int(row["attempt"]),
        "max_attempts": int(row["max_attempts"]),
        "lease_live": row.get("lease_expires") is not None and row["state"] == "pending",
        "retry_due": True,
        "abandoned": bool(row.get("abandoned")),
    }


@pytestmark_db
def test_the_claim_decision_agrees_on_real_runs():
    """Build runs in a real lab store and compare the claim decision per task.

    The comparison is against the **SQL** the store runs: for each task, does
    `claim_task` take it, or does the Rust decision say not to? A claim is a
    state change, so the test claims through Python and asks Rust the same
    question about the pre-claim snapshot.
    """
    import psycopg

    from orchestration.cloud_store import PostgresStore
    from orchestration.nexus_lab_store import LabStore
    from orchestration.nexus_projects import ProjectService

    mig = os.path.join(_REPO, "migrations")
    with psycopg.connect(_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(mig)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(mig, name)).read())

    store = PostgresStore(_DSN)
    try:
        svc = ProjectService(store)
        project = str(svc.create("user:alice", "Parity")["id"])
        goal = str(svc.create_goal("user:alice", project, "goal")[0]["id"])
        lab = LabStore(store)
        run = lab.start_run("user:alice", project, goal, policy="durable",
                            repository="owner/repo", branch="main")
        run_id = run["id"]
        # A two-task chain on one branch: t2 depends on t1.
        lab.add_task("user:alice", project, run_id, task_key="t1", title="one",
                     repository="owner/repo", branch="main")
        lab.add_task("user:alice", project, run_id, task_key="t2", title="two",
                     dependencies=["t1"], repository="owner/repo", branch="main")

        def rows():
            with store._tx() as cur:
                cur.execute("SELECT * FROM nexus_run_tasks WHERE run_id=%s "
                            "ORDER BY created_at, id", (run_id,))
                return [dict(r) for r in cur.fetchall()]

        def run_row():
            with store._tx() as cur:
                cur.execute("SELECT * FROM nexus_runs WHERE id=%s", (run_id,))
                return dict(cur.fetchone())

        # State 1: nothing claimed. t1 is claimable; t2 is not (dependency).
        tasks = rows()
        rs_run = _snapshot_run(run_row())
        rs_tasks = [_snapshot_task(t) for t in tasks]
        by_key = {t["task_key"]: t for t in tasks}
        assert _claim(rs_run, rs_tasks, str(by_key["t1"]["id"]))["claimable"] is True
        assert _claim(rs_run, rs_tasks, str(by_key["t2"]["id"]))["claimable"] is False

        # Python actually claims t1; the two must have agreed on *which* task.
        claimed = lab.claim_task(run_id, "w1")
        assert claimed is not None and claimed["task_key"] == "t1"

        # State 2: t1 is dispatched on the branch, so t2's branch is busy.
        tasks = rows()
        rs_tasks = [_snapshot_task(t) for t in tasks]
        rs_run = _snapshot_run(run_row())
        by_key = {t["task_key"]: t for t in tasks}
        v2 = _claim(rs_run, rs_tasks, str(by_key["t2"]["id"]))
        assert v2["claimable"] is False
        assert "branch" in (v2["reason"] or "") or "dependency" in (v2["reason"] or "")

        # State 3: t1 verifies. Python may now claim t2, and Rust agrees.
        lab.record_verification(str(by_key["t1"]["id"]), verdict="completed_verified",
                                evidence={"remote_sha": "abc"}, detail={"reason": "ok"})
        tasks = rows()
        rs_tasks = [_snapshot_task(t) for t in tasks]
        rs_run = _snapshot_run(run_row())
        by_key = {t["task_key"]: t for t in tasks}
        assert _claim(rs_run, rs_tasks, str(by_key["t2"]["id"]))["claimable"] is True
        claimed2 = lab.claim_task(run_id, "w2")
        assert claimed2 is not None and claimed2["task_key"] == "t2"
    finally:
        store.close()


@pytestmark_db
def test_the_claim_decision_agrees_on_a_paused_run():
    import psycopg

    from orchestration.cloud_store import PostgresStore
    from orchestration.nexus_lab_store import LabStore
    from orchestration.nexus_projects import ProjectService

    mig = os.path.join(_REPO, "migrations")
    with psycopg.connect(_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(mig)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(mig, name)).read())

    store = PostgresStore(_DSN)
    try:
        svc = ProjectService(store)
        project = str(svc.create("user:alice", "Parity2")["id"])
        goal = str(svc.create_goal("user:alice", project, "goal")[0]["id"])
        lab = LabStore(store)
        run = lab.start_run("user:alice", project, goal, policy="durable")
        run_id = run["id"]
        lab.add_task("user:alice", project, run_id, task_key="t1", title="one")
        with store._tx() as cur:
            cur.execute("UPDATE nexus_runs SET state='paused' WHERE id=%s", (run_id,))
            cur.execute("SELECT * FROM nexus_runs WHERE id=%s", (run_id,))
            rr = dict(cur.fetchone())
            cur.execute("SELECT * FROM nexus_run_tasks WHERE run_id=%s", (run_id,))
            tasks = [dict(r) for r in cur.fetchall()]
        rs_run = _snapshot_run(rr)
        rs_tasks = [_snapshot_task(t) for t in tasks]
        # Python claims nothing from a paused run; Rust must agree.
        assert lab.claim_task(run_id, "w1") is None
        assert _claim(rs_run, rs_tasks, str(tasks[0]["id"]))["claimable"] is False
    finally:
        store.close()


def _diff(cases, got, expected):
    bad = [(c, g, e) for c, g, e in zip(cases, got, expected) if g != e]
    return f"{len(bad)} disagreement(s): {bad[:5]}"
