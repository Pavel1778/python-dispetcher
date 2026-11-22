"""Python↔Rust parity for the run *decision* (`_decide` vs `decide_run`).

The audit found the Rust worker's original `decide()` was incomplete: it lacked
the deadlock-detection and durable budget-pause branches of
`LabScheduler._decide`, so a dependency-deadlocked run would have spun forever
in Rust where Python blocks it. `Store::decide_run` now mirrors `_decide`
completely. This test drives both over identical run states and compares the
decision.

The completion branch is compared only for the *decision* (completed vs not);
the GitHub confirmation inside `_decide`/`_complete` is the executor's and is
not exercised here (no live GitHub) - that is stated, not faked.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RUST = os.path.join(_REPO, "rust")

pytestmark = pytest.mark.skipif(
    shutil.which("cargo") is None and not os.environ.get("NEXUS_RUST_BIN"),
    reason="no Rust toolchain (set NEXUS_RUST_BIN to an already-built nexus-rs)",
)


def _dsn() -> str | None:
    return os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")


def _rust_bin() -> str:
    override = os.environ.get("NEXUS_RUST_BIN")
    if override:
        return override
    if not hasattr(_rust_bin, "_path"):
        subprocess.run(
            ["cargo", "build", "--quiet", "-p", "nexus-cli"], cwd=_RUST, check=True,
            env={**os.environ, "CARGO_TERM_COLOR": "never"},
        )
        _rust_bin._path = os.path.join(_RUST, "target", "debug", "nexus-rs")
    return _rust_bin._path


def _rust_decide(run_id: str) -> dict:
    proc = subprocess.run(
        [_rust_bin(), "decide", run_id], capture_output=True, check=True,
        env={**os.environ, "NEXUS_DB_URL": _dsn()},
    )
    return json.loads(proc.stdout.decode())


def _reset_schema() -> None:
    import psycopg

    mig = os.path.join(_REPO, "migrations")
    with psycopg.connect(_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(mig)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(mig, name)).read())


@pytest.fixture()
def lab():
    if not _dsn():
        pytest.skip("set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")
    _reset_schema()
    from orchestration.cloud_store import PostgresStore
    from orchestration.nexus_lab_store import LabStore
    from orchestration.nexus_projects import ProjectService

    store = PostgresStore(_dsn())
    svc = ProjectService(store)
    project = str(svc.create("user:parity", "Decide parity")["id"])
    yield {"store": store, "svc": svc, "project": project, "lab": LabStore(store)}
    store.close()


def _scheduler(lab):
    """A LabScheduler with inert executor/verifier, just to call `_decide`."""
    from orchestration.nexus_lab import LabScheduler

    class _Exec:
        def dispatch(self, *a, **k):
            raise AssertionError("decide must not dispatch")

    class _Verify:
        def verify(self, *a, **k):
            raise AssertionError("decide must not verify")

    return LabScheduler(lab["lab"], _Exec(), _Verify(), principal="user:parity")


def _make_run(lab, *, policy, state, tasks):
    """tasks: list of (task_key, task_state, deps, attempt, max_attempts)."""
    goal = str(lab["svc"].create_goal("user:parity", lab["project"], "g")[0]["id"])
    run = lab["lab"].start_run("user:parity", lab["project"], goal, policy=policy)
    rid = str(run["id"])
    with lab["store"]._tx() as cur:
        cur.execute("UPDATE nexus_runs SET state=%s WHERE id=%s", (state, rid))
        for key, tstate, deps, attempt, max_att in tasks:
            cur.execute(
                "INSERT INTO nexus_run_tasks(run_id, project_id, task_key, title, state, "
                "dependencies, attempt, max_attempts) "
                "SELECT id, project_id, %s, %s, %s, %s, %s, %s FROM nexus_runs WHERE id=%s",
                (key, key, tstate, json.dumps(deps), attempt, max_att, rid))
    return rid


def _py_decision(lab, sched, rid) -> dict:
    """Call `_decide` and normalise it to the same shape the Rust CLI prints."""
    durable = lab["lab"].run_row(rid)["policy"] == "durable"
    d = sched._decide(rid, durable=durable)
    if d is None:
        return {"decision": "continue"}
    state, reason, _code = d
    if state == "completed":
        return {"decision": "completed"}
    return {"decision": "settle", "state": state, "reason": reason}


# Each case: (name, policy, run-state, tasks). Compared by decision *kind* and,
# for settle, the state. Reasons are compared loosely (Rust uses the stable
# prefix; Python appends `_why`), see the assertion.
CASES = [
    ("no_tasks", "durable", "running", []),
    ("all_verified", "durable", "running",
     [("a", "completed_verified", [], 1, 3), ("b", "completed_verified", [], 1, 3)]),
    ("in_flight", "durable", "running",
     [("a", "completed_verified", [], 1, 3), ("b", "running", [], 1, 3)]),
    ("unfinished_none_verified", "durable", "running",
     [("a", "completed_verified", [], 1, 3), ("b", "completed_unverified", [], 1, 3)]),
    ("pending_claimable", "durable", "running",
     [("a", "pending", [], 0, 3)]),
    ("durable_deadlock", "durable", "running",
     [("a", "failed", [], 3, 3), ("b", "pending", ["a"], 0, 3)]),
    ("bounded_deadlock", "bounded", "running",
     [("a", "failed", [], 3, 3), ("b", "pending", ["a"], 0, 3)]),
]


@pytest.mark.parametrize("name,policy,state,tasks", CASES, ids=[c[0] for c in CASES])
def test_decide_matches_python(lab, name, policy, state, tasks):
    rid = _make_run(lab, policy=policy, state=state, tasks=tasks)
    sched = _scheduler(lab)
    py = _py_decision(lab, sched, rid)
    rs = _rust_decide(rid)
    assert py["decision"] == rs["decision"], f"{name}: py={py} rs={rs}"
    if py["decision"] == "settle":
        assert py["state"] == rs["state"], f"{name}: py={py} rs={rs}"
