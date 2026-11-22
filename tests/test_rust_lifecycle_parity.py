"""Python vs Rust run-lifecycle parity, on one real PostgreSQL.

This is the gate the authorization requires: a Rust component is eligible for
migration only when it agrees with the Python implementation over the same
cases on the same database. Here both are driven over identical starting
states and the **resulting database state** is compared - not a return value
alone, because the effect (blocked tasks, stop signals, cycle reset, fencing)
is what matters.

What is compared:
* a cancel: both block the same unfinished tasks and owe the same stop;
* a failed run: same, with a different execution outcome;
* an illegal transition: both refuse it (strict) / both skip it (internal);
* a resume that has something to retry: both requeue and reset the cycle;
* a resume with nothing to retry: both refuse;
* reclaim: both requeue an unbound, expired task and leave a bound one;
* a stale fencing token: both make a release a no-op.

Skips cleanly without cargo/`NEXUS_RUST_BIN` or `TEST_SUPABASE_DB_URL`.
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


def _rust(*args: str) -> tuple[int, str]:
    """Run a nexus-rs subcommand; return (exit code, stdout stripped)."""
    proc = subprocess.run(
        [_rust_bin(), *args], capture_output=True,
        env={**os.environ, "NEXUS_DB_URL": _dsn()},
    )
    return proc.returncode, proc.stdout.decode().strip()


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
    project = str(svc.create("user:parity", "Lifecycle parity")["id"])
    yield {"store": store, "svc": svc, "project": project, "lab": LabStore(store)}
    store.close()


def _run(lab, project, *, policy="durable", state="running"):
    goal = str(lab["svc"].create_goal("user:parity", project, "g")[0]["id"])
    run = lab["lab"].start_run("user:parity", project, goal, policy=policy)
    rid = str(run["id"])
    if state != "running":
        with lab["store"]._tx() as cur:
            cur.execute("UPDATE nexus_runs SET state=%s WHERE id=%s", (state, rid))
    return rid


def _task(store, run_id, key, state, *, conversation=None, attempt=None):
    with store._tx() as cur:
        cur.execute(
            "INSERT INTO nexus_run_tasks(run_id, project_id, task_key, title, state) "
            "SELECT id, project_id, %s, %s, %s FROM nexus_runs WHERE id=%s RETURNING id",
            (key, key, state, run_id))
        tid = str(cur.fetchone()["id"])
        if conversation is not None:
            cur.execute("UPDATE nexus_run_tasks SET conversation_id=%s WHERE id=%s",
                        (conversation, tid))
        if attempt is not None:
            cur.execute("UPDATE nexus_run_tasks SET attempt=%s WHERE id=%s", (attempt, tid))
        return tid


def _snapshot(store, run_id):
    """The state that must match between the two implementations."""
    with store._tx() as cur:
        cur.execute("SELECT state, hidden_at IS NOT NULL AS hidden, iteration, "
                    "total_iterations FROM nexus_runs WHERE id=%s", (run_id,))
        run = dict(cur.fetchone())
        cur.execute("SELECT task_key, state, attempt, cycle FROM nexus_run_tasks "
                    "WHERE run_id=%s ORDER BY task_key", (run_id,))
        tasks = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT count(*) AS n FROM nexus_executor_stops WHERE run_id=%s "
                    "AND state='requested'", (run_id,))
        stops = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM nexus_task_executions WHERE run_id=%s "
                    "AND ended_at IS NULL", (run_id,))
        open_exec = cur.fetchone()["n"]
        # The goal mirror, the notifications the transition emitted, and the
        # conversation workflow state (mission items: goal mirroring,
        # notifications, chat events). These must match, not just run/tasks.
        cur.execute("SELECT g.state FROM nexus_goals g "
                    "JOIN nexus_runs r ON r.goal_id=g.id WHERE r.id=%s", (run_id,))
        goal_state = cur.fetchone()["state"]
        cur.execute("SELECT kind FROM nexus_notifications WHERE resource_id=%s "
                    "ORDER BY kind", (run_id,))
        notifs = sorted(r["kind"] for r in cur.fetchall())
        cur.execute("SELECT workflow_state FROM nexus_conversations "
                    "WHERE id = (SELECT conversation_id FROM nexus_runs WHERE id=%s)", (run_id,))
        row = cur.fetchone()
        workflow = row["workflow_state"] if row else None
    return {"run": run, "tasks": tasks, "stops": stops, "open_executions": open_exec,
            "goal_state": goal_state, "notifications": notifs, "workflow": workflow}


def _build_pair(lab, *, policy, state, tasks):
    """Two identical runs; return (python_run, rust_run).

    Conversation ids are made unique per run: a real run never shares a
    conversation id with another, and `nexus_executor_stops` has a UNIQUE
    constraint on it, so sharing one would make the two sides collide on that
    table for a reason that has nothing to do with the code under test.
    """
    project = lab["project"]
    py = _run(lab, project, policy=policy, state=state)
    rs = _run(lab, project, policy=policy, state=state)
    for rid, suffix in ((py, "py"), (rs, "rs")):
        for key, tstate, conv, attempt in tasks:
            conv_id = f"{conv}-{suffix}" if conv else None
            _task(lab["store"], rid, key, tstate, conversation=conv_id, attempt=attempt)
    return py, rs


# --------------------------------------------------------------------- cancel
def test_cancel_matches(lab):
    store, project = lab["store"], lab["project"]
    py, rs = _build_pair(lab, policy="durable", state="running", tasks=[
        ("a", "running", "conv-a", None),
        ("b", "pending", None, None),
    ])
    # Python cancels its run.
    lab["lab"].set_run_state("user:parity", project, py, "cancelled")
    # Rust cancels the other.
    code, out = _rust("run-state", project, rs, "cancelled", "--strict", "--actor", "user:parity")
    assert code == 0 and out != "null", out

    a, b = _snapshot(store, py), _snapshot(store, rs)
    assert a == b, f"cancel diverged:\npy={a}\nrs={b}"
    assert a["run"]["state"] == "cancelled" and a["run"]["hidden"] is True
    assert a["stops"] == 1


# --------------------------------------------------------------------- failed
def test_failed_run_matches(lab):
    store, project = lab["store"], lab["project"]
    py, rs = _build_pair(lab, policy="durable", state="running", tasks=[
        ("a", "running", "conv-a", None),
    ])
    lab["lab"].set_run_state("user:parity", project, py, "failed", last_error="boom")
    code, _ = _rust("run-state", project, rs, "failed", "--strict", "--last-error", "boom")
    assert code == 0
    a, b = _snapshot(store, py), _snapshot(store, rs)
    # hidden is False for a failed run in both; compare the rest.
    assert a == b, f"failed diverged:\npy={a}\nrs={b}"


# ----------------------------------------------------------- illegal transition
def test_illegal_transition_matches(lab):
    _, project = lab["store"], lab["project"]
    py, rs = _build_pair(lab, policy="durable", state="completed", tasks=[])
    # Strict: both refuse.
    with pytest.raises(Exception):
        lab["lab"].set_run_state("user:parity", project, py, "running")
    code, _ = _rust("run-state", project, rs, "running", "--strict")
    assert code != 0, "Rust must refuse an illegal transition when strict"
    # Internal: both return None (no error) and leave the run alone.
    assert lab["lab"].set_run_state_internal(py, "running") is None
    code, out = _rust("run-state", project, rs, "running")  # no --strict
    assert code == 0 and out == "null"


# --------------------------------------------------------------- resume retry
def test_resume_with_retry_matches(lab):
    store, project = lab["store"], lab["project"]
    py, rs = _build_pair(lab, policy="durable", state="blocked", tasks=[
        ("a", "failed", None, 3),
    ])
    lab["lab"].set_run_state("user:parity", project, py, "running")
    code, out = _rust("run-state", project, rs, "running", "--strict")
    assert code == 0 and out != "null", out
    a, b = _snapshot(store, py), _snapshot(store, rs)
    assert a == b, f"resume diverged:\npy={a}\nrs={b}"
    assert a["tasks"][0]["state"] == "pending" and a["tasks"][0]["cycle"] == 1


def test_resume_without_retry_matches(lab):
    _, project = lab["store"], lab["project"]
    py, rs = _build_pair(lab, policy="bounded", state="blocked", tasks=[
        ("a", "completed_verified", None, 1),
    ])
    with pytest.raises(Exception):
        lab["lab"].set_run_state("user:parity", project, py, "running")
    code, _ = _rust("run-state", project, rs, "running", "--strict")
    assert code != 0, "Rust must refuse a resume with nothing to retry"


# -------------------------------------------------------------------- reclaim
def test_reclaim_matches(lab):
    store, _ = lab["store"], lab["project"]
    py, rs = _build_pair(lab, policy="durable", state="running", tasks=[
        ("unbound", "dispatched", None, 3),
        ("bound", "running", "conv-b", None),
    ])
    for rid in (py, rs):
        with store._tx() as cur:
            cur.execute("UPDATE nexus_run_tasks SET lease_expires = now() - interval '1 second' "
                        "WHERE run_id=%s AND state IN ('dispatched','running')", (rid,))
    py_reclaimed = lab["lab"].reclaim_expired(py)
    code, out = _rust("reclaim", rs)
    assert code == 0, out
    rs_reclaimed = json.loads(out)
    assert len(py_reclaimed) == len(rs_reclaimed) == 1, (len(py_reclaimed), len(rs_reclaimed))
    a, b = _snapshot(store, py), _snapshot(store, rs)
    assert a == b, f"reclaim diverged:\npy={a}\nrs={b}"


# ------------------------------------------------------------------- fencing
def test_stale_fencing_token_matches(lab):
    store, _ = lab["store"], lab["project"]
    py, rs = _build_pair(lab, policy="durable", state="running", tasks=[
        ("a", "dispatched", None, None),
    ])
    # Both tasks carry token 5; a stale holder has 4.
    for rid in (py, rs):
        with store._tx() as cur:
            cur.execute("UPDATE nexus_run_tasks SET lease_token=5 WHERE run_id=%s", (rid,))
    py_task = _task_id(store, py, "a")
    rs_task = _task_id(store, rs, "a")

    py_res = lab["lab"].release_task(py_task, error="boom", requeue=True, lease_token=4)
    code, out = _rust("release", rs_task, "--error", "boom", "--lease-token", "4")
    assert code == 0, out
    rs_res = json.loads(out)
    # Both no-ops: still dispatched, token unchanged.
    assert py_res["state"] == rs_res["state"] == "dispatched"
    assert py_res["lease_token"] == rs_res["lease_token"] == 5


def _task_id(store, run_id, key):
    with store._tx() as cur:
        cur.execute("SELECT id FROM nexus_run_tasks WHERE run_id=%s AND task_key=%s",
                    (run_id, key))
        return str(cur.fetchone()["id"])


# --------------------------------------------------- exhaustive transitions
_RUN_STATES = ["running", "paused", "waiting_for_approval", "blocked",
               "completed", "failed", "cancelled"]


@pytest.mark.parametrize("frm", _RUN_STATES)
@pytest.mark.parametrize("to", _RUN_STATES)
def test_every_run_transition_agrees(lab, frm, to):
    """Every (from,to) pair: both implementations agree on legal/illegal and on
    the resulting state. The Rust side is the `run-state --strict` CLI; the
    Python side is `set_run_state`."""
    store, project = lab["store"], lab["project"]
    py = _run(lab, project, policy="durable", state=frm)
    rs = _run(lab, project, policy="durable", state=frm)
    # Python: strict set_run_state (a no-op to the same state returns the row).
    try:
        lab["lab"].set_run_state("user:parity", project, py, to)
        py_ok = True
    except Exception:
        py_ok = False
    code, out = _rust("run-state", project, rs, to, "--strict")
    rs_ok = code == 0 and out != "null"
    assert py_ok == rs_ok, f"{frm}->{to}: python={py_ok} rust={rs_ok} ({out})"
    if py_ok:
        # Both landed on the same state.
        with store._tx() as cur:
            cur.execute("SELECT state FROM nexus_runs WHERE id=%s", (py,))
            py_state = cur.fetchone()["state"]
            cur.execute("SELECT state FROM nexus_runs WHERE id=%s", (rs,))
            rs_state = cur.fetchone()["state"]
        assert py_state == rs_state == to


# --------------------------------------------------- Unicode reason truncation
def test_a_multibyte_fail_reason_truncates_identically(lab):
    """A *failed* run's reason is built as `run failed: <last_error>` and sliced
    to 500 characters. With multibyte characters, Python slices codepoints and
    Rust must too - a byte slice would differ. Both the run's last_error and the
    task/execution reason the transition wrote must match."""
    store, project = lab["store"], lab["project"]
    py = _run(lab, project, state="running")
    rs = _run(lab, project, state="running")
    # A task at work, so the failed transition writes a stop + blocks it.
    _task(store, py, "a", "running", conversation="conv-py")
    _task(store, rs, "a", "running", conversation="conv-rs")
    reason = "歧" * 600  # 600 chars, 1800 bytes in UTF-8
    lab["lab"].set_run_state("user:parity", project, py, "failed", last_error=reason)
    code, _ = _rust("run-state", project, rs, "failed", "--strict", "--last-error", reason)
    assert code == 0
    with store._tx() as cur:
        # The run's last_error is stored via COALESCE (untruncated) in both.
        cur.execute("SELECT last_error FROM nexus_runs WHERE id=%s", (py,))
        py_err = cur.fetchone()["last_error"]
        cur.execute("SELECT last_error FROM nexus_runs WHERE id=%s", (rs,))
        rs_err = cur.fetchone()["last_error"]
        assert py_err == rs_err
        # The task's last_error is the truncated `why` ("run failed: ...")[:500].
        cur.execute("SELECT last_error FROM nexus_run_tasks WHERE run_id=%s AND task_key='a'", (py,))
        py_task_err = cur.fetchone()["last_error"]
        cur.execute("SELECT last_error FROM nexus_run_tasks WHERE run_id=%s AND task_key='a'", (rs,))
        rs_task_err = cur.fetchone()["last_error"]
    assert py_task_err == rs_task_err, "the truncated task reason must match byte for byte"
    assert len(py_task_err) == 500, f"the task reason is truncated to 500 characters, got {len(py_task_err)}"


# --------------------------------------------------- repeated cancel
def test_cancelling_twice_is_idempotent_in_both(lab):
    _, project = lab["store"], lab["project"]
    py = _run(lab, project, state="running")
    rs = _run(lab, project, state="running")
    lab["lab"].set_run_state("user:parity", project, py, "cancelled")
    code, _ = _rust("run-state", project, rs, "cancelled", "--strict")
    assert code == 0
    # A second cancel: Python returns the row (no-op); Rust strict is a no-op too.
    py2 = lab["lab"].set_run_state("user:parity", project, py, "cancelled")
    code2, out2 = _rust("run-state", project, rs, "cancelled", "--strict")
    assert code2 == 0 and out2 != "null", "a repeat cancel is a no-op, not an error"
    assert py2["state"] == "cancelled"
