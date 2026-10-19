"""Tests for the durable control plane.

The crash tests work by closing the SQLite connection and opening a fresh one
on the same file -- that is exactly what a process restart looks like from the
store's point of view, so it exercises the real recovery path rather than a
mock.
"""

from __future__ import annotations

import threading
import time

import pytest

from orchestration.control_plane import (
    ControlPlane,
    InvalidTransition,
    LeaseConflict,
    NotFound,
    TaskState,
)


@pytest.fixture()
def cp(tmp_path):
    plane = ControlPlane(tmp_path / "state" / "cp.db")
    yield plane
    plane.close()


def reopen(cp):
    """Simulate a process restart against the same database file."""
    path = cp.path
    cp.close()
    return ControlPlane(path)


# --------------------------------------------------------------- durability
def test_state_survives_restart(tmp_path):
    cp = ControlPlane(tmp_path / "cp.db")
    cp.upsert_project("proj", "/workspace/project/x", default_branch="main")
    cp.register_session("cmd", "commander", capabilities=["dispatch"])
    cp.submit_task("t1", "do the thing", project_id="proj")
    cp.close()

    cp2 = ControlPlane(tmp_path / "cp.db")
    try:
        task = cp2.get_task("t1")
        assert task.state is TaskState.QUEUED
        assert task.goal == "do the thing"
        assert cp2.get_project("proj")["directory"] == "/workspace/project/x"
        assert cp2.get_session("cmd")["role"] == "commander"
    finally:
        cp2.close()


def test_wal_mode_is_enabled(tmp_path):
    cp = ControlPlane(tmp_path / "cp.db")
    try:
        mode = cp._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        cp.close()


# -------------------------------------------------------------- idempotency
def test_submit_is_idempotent(cp):
    a = cp.submit_task("t1", "goal", idempotency_key="key-1")
    b = cp.submit_task("t2", "goal", idempotency_key="key-1")
    assert a.id == b.id == "t1", "second submit must return the first task"
    assert len(cp.list_tasks()) == 1


def test_concurrent_submit_same_key_creates_one_task(tmp_path):
    cp = ControlPlane(tmp_path / "cp.db")
    results: list[str] = []
    errors: list[Exception] = []

    def submit(i):
        try:
            t = cp.submit_task(f"t{i}", "goal", idempotency_key="same")
            results.append(t.id)
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    cp.close()

    assert not errors, errors
    assert len(set(results)) == 1, f"expected one task id, got {set(results)}"


# ------------------------------------------------------------- transitions
def test_transition_table_rejects_illegal_moves(cp):
    cp.submit_task("t1", "goal")
    with pytest.raises(InvalidTransition):
        cp.transition("t1", TaskState.COMPLETED)  # queued -> completed
    with pytest.raises(InvalidTransition):
        cp.transition("t1", TaskState.RUNNING)  # queued -> running


def test_happy_path(cp):
    cp.submit_task("t1", "goal")
    cp.claim_task("t1", "agent-a")
    cp.transition("t1", TaskState.RUNNING, owner="agent-a")
    cp.transition("t1", TaskState.VERIFYING, owner="agent-a")
    done = cp.transition("t1", TaskState.COMPLETED, owner="agent-a", result="ok")
    assert done.state is TaskState.COMPLETED
    assert done.result == "ok"
    with pytest.raises(InvalidTransition):
        cp.transition("t1", TaskState.RUNNING)  # terminal


def test_unknown_task_raises(cp):
    with pytest.raises(NotFound):
        cp.get_task("nope")


# ------------------------------------------------------------------ leases
def test_lease_conflict(cp):
    cp.submit_task("t1", "goal")
    cp.claim_task("t1", "agent-a", lease_seconds=300)
    with pytest.raises(LeaseConflict):
        cp.claim_task("t1", "agent-b", lease_seconds=300)


def test_lease_renewal_extends(cp):
    cp.submit_task("t1", "goal")
    t = cp.claim_task("t1", "agent-a", lease_seconds=10)
    first = t.lease_expires
    time.sleep(0.01)
    t2 = cp.renew_lease("t1", "agent-a", lease_seconds=60)
    assert t2.lease_expires > first


def test_renew_requires_ownership(cp):
    cp.submit_task("t1", "goal")
    cp.claim_task("t1", "agent-a")
    with pytest.raises(LeaseConflict):
        cp.renew_lease("t1", "agent-b")


def test_expired_lease_can_be_reclaimed(cp):
    cp.submit_task("t1", "goal")
    cp.claim_task("t1", "agent-a", lease_seconds=-1)  # already expired
    expired = cp.expired_leases()
    assert [t.id for t in expired] == ["t1"]

    actions = cp.reclaim_expired()
    assert actions == [{"task": "t1", "action": "recovering"}]
    assert cp.get_task("t1").state is TaskState.RECOVERING


def test_dead_letter_after_max_attempts(cp):
    cp.submit_task("t1", "goal", max_attempts=1)
    cp.claim_task("t1", "agent-a")
    cp.transition("t1", TaskState.RUNNING, owner="agent-a")
    cp.transition("t1", TaskState.FAILED, owner="agent-a")  # attempt -> 1
    cp.claim_task("t1", "agent-b", lease_seconds=-1)
    cp.transition("t1", TaskState.RUNNING, owner="agent-b")

    actions = cp.reclaim_expired()
    assert actions == [{"task": "t1", "action": "dead_letter"}]
    assert cp.get_task("t1").state is TaskState.DEAD_LETTER


# -------------------------------------------------------------- dependencies
def test_dependencies_block_claim(cp):
    cp.submit_task("t1", "first")
    cp.submit_task("t2", "second", dependencies=["t1"])
    with pytest.raises(InvalidTransition) as exc:
        cp.claim_task("t2", "agent-a")
    assert "unmet dependencies" in str(exc.value)

    cp.claim_task("t1", "agent-a")
    cp.transition("t1", TaskState.RUNNING, owner="agent-a")
    cp.transition("t1", TaskState.VERIFYING, owner="agent-a")
    cp.transition("t1", TaskState.COMPLETED, owner="agent-a")

    claimed = cp.claim_task("t2", "agent-a")
    assert claimed.state is TaskState.ASSIGNED


# ---------------------------------------------------------------- approvals
def test_approval_gate(cp):
    cp.submit_task("t1", "risky", requires_approval=True)
    with pytest.raises(InvalidTransition) as exc:
        cp.claim_task("t1", "agent-a")
    assert "approval required" in str(exc.value)

    cp.approve("t1", approved=True)
    assert cp.claim_task("t1", "agent-a").state is TaskState.ASSIGNED


def test_rejection_keeps_task_unclaimable(cp):
    cp.submit_task("t1", "risky", requires_approval=True)
    cp.approve("t1", approved=False)
    with pytest.raises(InvalidTransition):
        cp.claim_task("t1", "agent-a")


# ----------------------------------------------------------------- evidence
def test_evidence_accumulates(cp):
    cp.submit_task("t1", "goal")
    cp.add_evidence("t1", {"kind": "test", "detail": "12 passed"})
    cp.add_evidence("t1", {"kind": "diff", "detail": "3 files"})
    t = cp.get_task("t1")
    assert len(t.evidence) == 2
    assert t.evidence[0]["kind"] == "test"


# ---------------------------------------------------------------- recovery
def test_reconcile_after_crash_reports_resumable(tmp_path):
    cp = ControlPlane(tmp_path / "cp.db")
    cp.register_session("agent-a", "worker")
    cp.submit_task("t1", "work in flight")
    cp.claim_task("t1", "agent-a", lease_seconds=-1)
    cp.transition("t1", TaskState.RUNNING, owner="agent-a")
    cp.submit_task("t2", "fresh work")
    cp.close()

    cp2 = reopen(cp)
    try:
        report = cp2.reconcile(session_ttl=0)
        assert "t1" in [a["task"] for a in report["reclaimed"]]
        assert "t2" in report["resumable"]
        assert cp2.get_task("t1").state is TaskState.RECOVERING
    finally:
        cp2.close()


def test_reconcile_marks_stale_sessions(tmp_path):
    cp = ControlPlane(tmp_path / "cp.db")
    cp.register_session("dead-agent", "worker")
    cp.close()

    cp2 = reopen(cp)
    try:
        report = cp2.reconcile(session_ttl=0)
        assert "dead-agent" in report["stale_sessions"]
        assert cp2.get_session("dead-agent")["state"] == "stale"
    finally:
        cp2.close()


def test_reconcile_reports_blocked_and_awaiting(cp):
    cp.submit_task("t1", "first")
    cp.submit_task("t2", "dep", dependencies=["t1"])
    cp.submit_task("t3", "risky", requires_approval=True)
    report = cp.reconcile(session_ttl=120)
    assert "t2" in report["blocked_on_dependencies"]
    assert "t3" in report["awaiting_approval"]
    assert "t1" in report["resumable"]


def test_recovery_does_not_lose_attempt_count(tmp_path):
    """A crash must not reset the attempt counter, or retries would loop."""
    cp = ControlPlane(tmp_path / "cp.db")
    cp.submit_task("t1", "goal", max_attempts=3)
    cp.claim_task("t1", "agent-a")
    cp.transition("t1", TaskState.RUNNING, owner="agent-a")
    cp.transition("t1", TaskState.FAILED, owner="agent-a")  # attempt -> 1
    cp.close()

    cp2 = reopen(cp)
    try:
        assert cp2.get_task("t1").attempt == 1
    finally:
        cp2.close()


# ------------------------------------------------------------------ events
def test_events_record_history(cp):
    cp.submit_task("t1", "goal")
    cp.claim_task("t1", "agent-a")
    cp.transition("t1", TaskState.RUNNING, owner="agent-a")
    kinds = [e["kind"] for e in cp.events(task_id="t1")]
    assert "task_submitted" in kinds
    assert "task_claimed" in kinds
    assert "task_transition" in kinds


def test_events_are_json_safe(cp):
    cp.submit_task("t1", "goal with unicode: задача")
    ev = cp.events(task_id="t1")[0]
    assert "задача" in ev["detail"]


# ----------------------------------------------------------- multi-project
def test_two_projects_are_independent(cp):
    cp.upsert_project("p1", "/workspace/project/a", default_branch="main")
    cp.upsert_project("p2", "/workspace/project/b", default_branch="main")
    cp.submit_task("t1", "a", project_id="p1")
    cp.submit_task("t2", "b", project_id="p2")
    assert cp.get_task("t1").project_id == "p1"
    assert cp.get_task("t2").project_id == "p2"


def test_priority_ordering(cp):
    cp.submit_task("low", "low", priority=1)
    cp.submit_task("high", "high", priority=10)
    cp.submit_task("mid", "mid", priority=5)
    order = [t.id for t in cp.list_tasks()]
    assert order == ["high", "mid", "low"]
