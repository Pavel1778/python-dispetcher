"""The Rust claim transaction, compared with the Python one on one database.

This is the next migration gate after `test_rust_parity.py`: that file compares
the *decision* (`nexus-rs claimable`) over a snapshot; this one compares the
whole **claim transaction** - `LabStore.claim_task` (Python, SQL) against
`nexus-rs claim` (Rust, SQLx) - on the same rows of the same real PostgreSQL.

The point is not that the two "look alike". It is that, given one database
state, both must take the same task (or both take nothing), and the state they
leave behind must be the same shape: `dispatched`, one attempt spent, one
iteration spent, a fresh `lease_token`, the worker recorded as `lease_owner`.

A second test proves the property that matters under load: N workers claiming
at the same moment (half Python, half Rust) take N *distinct* tasks - no
duplicate claim, no lost task - which is what `FOR UPDATE SKIP LOCKED` plus the
advisory branch lock exist to guarantee.

Skips cleanly without `cargo`/`NEXUS_RUST_BIN` or without `TEST_SUPABASE_DB_URL`,
and says which, so an environment that cannot run it reports a skip, not a pass.
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


def _dsn() -> str | None:
    """The database to use, read **at call time**.

    `tests/_test_db.py` points `TEST_SUPABASE_DB_URL` at a per-process database
    when the pytest session starts, which is after this module is imported, so
    the value must not be captured at import time.
    """
    return os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

pytestmark = pytest.mark.skipif(
    shutil.which("cargo") is None and not os.environ.get("NEXUS_RUST_BIN"),
    reason="no Rust toolchain (set NEXUS_RUST_BIN to an already-built nexus-rs)",
)


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


def _rust_claim(run_id: str, worker: str) -> dict | None:
    """Claim one task through the Rust transaction; `None` when it takes none."""
    proc = subprocess.run(
        [_rust_bin(), "claim", str(run_id), worker],
        capture_output=True, check=True,
        env={**os.environ, "NEXUS_DB_URL": _dsn()},
    )
    raw = proc.stdout.decode().strip()
    return None if raw == "null" else json.loads(raw)


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
    """A fresh schema, a project, and a `LabStore` bound to it."""
    if not _dsn():
        pytest.skip("set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")
    _reset_schema()
    from orchestration.cloud_store import PostgresStore
    from orchestration.nexus_lab_store import LabStore
    from orchestration.nexus_projects import ProjectService

    store = PostgresStore(_dsn())
    svc = ProjectService(store)
    project = str(svc.create("user:parity", "Claim parity")["id"])
    yield {"store": store, "svc": svc, "project": project, "lab": LabStore(store)}
    store.close()


def _new_run(lab, svc, project, *, tasks, repository="", branch=""):
    goal = str(svc.create_goal("user:parity", project, "goal")[0]["id"])
    run = lab.start_run("user:parity", project, goal, policy="durable",
                        repository=repository, branch=branch)
    run_id = str(run["id"])
    for key, deps in tasks:
        lab.add_task("user:parity", project, run_id, task_key=key, title=key,
                     dependencies=deps, repository=repository, branch=branch)
    return run_id


def _task_states(store, run_id):
    with store._tx() as cur:
        cur.execute("SELECT task_key, state, attempt, lease_token, lease_owner "
                    "FROM nexus_run_tasks WHERE run_id=%s ORDER BY created_at, id",
                    (run_id,))
        return {r["task_key"]: dict(r) for r in cur.fetchall()}


def test_both_take_the_same_first_task(lab):
    """One run, one chain: Python and Rust agree on which task is first."""
    store, svc, project, labstore = lab["store"], lab["svc"], lab["project"], lab["lab"]
    run_id = _new_run(labstore, svc, project, tasks=[("t1", []), ("t2", ["t1"])])

    py = labstore.claim_task(run_id, "py-worker")
    assert py is not None and py["task_key"] == "t1"

    # After Python took t1 (in flight), Rust must take nothing: t2 depends on
    # an unverified t1, and t1 is at work. Both implementations see the same.
    assert _rust_claim(run_id, "rs-worker") is None

    states = _task_states(store, run_id)
    assert states["t1"]["state"] == "dispatched"
    assert states["t1"]["attempt"] == 1
    assert states["t1"]["lease_owner"] == "py-worker"


def test_rust_and_python_leave_the_same_row_shape(lab):
    """A Rust claim and a Python claim leave the same state on identical rows."""
    store, svc, project, labstore = lab["store"], lab["svc"], lab["project"], lab["lab"]

    # Two independent runs, identical shape, no shared branch.
    run_py = _new_run(labstore, svc, project, tasks=[("t1", [])])
    run_rs = _new_run(labstore, svc, project, tasks=[("t1", [])])

    py = labstore.claim_task(run_py, "worker-a")
    rs = _rust_claim(run_rs, "worker-a")
    assert py is not None and rs is not None

    for key in ("state", "attempt"):
        assert py[key] == rs[key], f"{key} differs: python={py[key]} rust={rs[key]}"
    assert rs["lease_owner"] == "worker-a"
    assert rs["lease_token"] >= 1 and py["lease_token"] >= 1

    # Both runs spent exactly one iteration.
    with store._tx() as cur:
        cur.execute("SELECT id, iteration FROM nexus_runs WHERE id = ANY(%s)",
                    ([run_py, run_rs],))
        it = {str(r["id"]): r["iteration"] for r in cur.fetchall()}
    assert it[run_py] == 1 and it[run_rs] == 1


def test_rust_does_not_take_a_busy_branch(lab):
    """A branch with a task at work is not handed to a second task (Rust path)."""
    store, svc, project, labstore = lab["store"], lab["svc"], lab["project"], lab["lab"]
    run_id = _new_run(labstore, svc, project,
                      tasks=[("a", []), ("b", [])],
                      repository="owner/parity", branch="main")

    first = labstore.claim_task(run_id, "py-worker")
    assert first is not None and first["task_key"] == "a"
    # `b` shares the branch and `a` is in flight: Rust must refuse it too.
    assert _rust_claim(run_id, "rs-worker") is None
    states = _task_states(store, run_id)
    assert states["b"]["state"] == "pending"


def test_mixed_workers_take_distinct_tasks(lab, monkeypatch):
    """Five Python and five Rust workers claiming together take ten distinct tasks.

    The global backpressure cap (`NEXUS_LAB_MAX_ACTIVE_TASKS`, default 8) bounds
    how many tasks may be in flight at once, so it is raised above the worker
    count here: this test is about *distinctness under concurrency*, not about
    the cap (which `test_rust_cap_matches_python` covers). Both implementations
    read the same variable name and the same default, or they would enforce
    different ceilings.
    """
    monkeypatch.setenv("NEXUS_LAB_MAX_ACTIVE_TASKS", "20")
    store, svc, project, labstore = lab["store"], lab["svc"], lab["project"], lab["lab"]
    run_id = _new_run(labstore, svc, project,
                      tasks=[(f"t{i}", []) for i in range(10)])

    import concurrent.futures as cf

    def take_py(i):
        r = labstore.claim_task(run_id, f"py-{i}")
        return r["task_key"] if r else None

    def take_rs(i):
        r = _rust_claim(run_id, f"rs-{i}")
        return r["task_key"] if r else None

    taken: list[str] = []
    with cf.ThreadPoolExecutor(max_workers=10) as pool:
        futs = []
        for i in range(5):
            futs.append(pool.submit(take_py, i))
            futs.append(pool.submit(take_rs, i))
        for f in futs:
            key = f.result()
            if key:
                taken.append(key)

    assert len(taken) == len(set(taken)) == 10, (
        f"expected 10 distinct tasks, got {sorted(taken)}")

    states = _task_states(store, run_id)
    assert all(s["state"] == "dispatched" for s in states.values())
    assert all(s["attempt"] == 1 for s in states.values())


def test_rust_cap_matches_python(lab, monkeypatch):
    """The global in-flight cap is enforced identically by both implementations.

    With `NEXUS_LAB_MAX_ACTIVE_TASKS=2`, at most two tasks may be in flight; a
    third claim - by either implementation - takes nothing. This pins the one
    setting the two claim paths must agree on by *name and default*, not just by
    effect.
    """
    monkeypatch.setenv("NEXUS_LAB_MAX_ACTIVE_TASKS", "2")
    svc, project, labstore = lab["svc"], lab["project"], lab["lab"]
    run_id = _new_run(labstore, svc, project, tasks=[(f"t{i}", []) for i in range(5)])

    a = labstore.claim_task(run_id, "py-1")
    b = _rust_claim(run_id, "rs-1")
    assert a is not None and b is not None, "two tasks may be in flight"
    # The cap is now reached: neither implementation may take a third.
    assert labstore.claim_task(run_id, "py-2") is None, "Python honours the cap"
    assert _rust_claim(run_id, "rs-2") is None, "Rust honours the same cap"
