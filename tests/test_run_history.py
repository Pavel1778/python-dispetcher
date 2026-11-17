"""Run history management: hide/show, remove from history, retry failed tasks.

Over real HTTP and PostgreSQL. Removing a finished run from history removes the
run's own record and nothing else: the goal, the specification, the chat, the
evidence and the repository are all untouched. Retrying failed tasks requeues
*only* the unsuccessful ones and never a task that already verified.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_studio_api import DSN, REPO, studio_stack  # noqa: E402

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run these tests")


@pytest.fixture
def studio(monkeypatch):
    with studio_stack(monkeypatch) as stack:
        yield stack


def _conversation(stack, project, mode="autonomous"):
    status, body = stack.api.post(f"/api/v1/projects/{project}/conversations",
                                   {"title": "", "mode": mode})
    assert status == 201, body
    return body["conversation"]["id"]


def _start_run(stack, project, *, tasks=None):
    """Start a real run through the chat and return ``(run_id, conversation)``."""
    conversation = _conversation(stack, project)
    plan = {"tasks": tasks or [
        {"id": "a", "title": "Шаг A", "description": "сделать A",
         "acceptance": ["ок"], "dependencies": []},
        {"id": "b", "title": "Шаг B", "description": "сделать B",
         "acceptance": ["ок"], "dependencies": ["a"]},
    ]}
    import json
    stack.answers(json.dumps(plan))
    status, body = stack.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages",
        {"text": "Исправь найденные ошибки"})
    assert status == 200, body
    run_id = next(m["meta"]["run_id"] for m in body["messages"] if m["meta"].get("run_id"))
    return run_id, conversation


def _settle(studio, run_id, *, state):
    """Force a run to a terminal state directly, as the worker would."""
    with studio.app.store._tx() as cur:
        cur.execute("UPDATE nexus_runs SET state=%s, hidden_at="
                    "CASE WHEN %s='cancelled' THEN COALESCE(hidden_at, now()) ELSE hidden_at END "
                    "WHERE id=%s", (state, state, run_id))


# -------------------------------------------------------------- hide/show
def test_hide_and_show_a_finished_run(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    run_id, _ = _start_run(studio, project)
    # An active run cannot be hidden.
    status, body = studio.api.post(
        f"/api/v1/projects/{project}/runs/{run_id}/hide", {})
    assert status == 409, body
    _settle(studio, run_id, state="completed")
    # Now it can.
    assert studio.api.post(f"/api/v1/projects/{project}/runs/{run_id}/hide", {})[0] == 200
    active = studio.api.get(f"/api/v1/projects/{project}/runs")[1]["runs"]
    assert run_id not in [r["id"] for r in active]
    archived = studio.api.get(f"/api/v1/projects/{project}/runs?archived=1")[1]["runs"]
    assert run_id in [r["id"] for r in archived]
    # Show puts it back.
    assert studio.api.post(f"/api/v1/projects/{project}/runs/{run_id}/show", {})[0] == 200
    active = studio.api.get(f"/api/v1/projects/{project}/runs")[1]["runs"]
    assert run_id in [r["id"] for r in active]


# ----------------------------------------------------- remove from history
def test_removing_a_finished_run_keeps_the_goal_chat_and_repository(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    run_id, conversation = _start_run(studio, project)
    _settle(studio, run_id, state="completed")

    goals_before = studio.api.get(f"/api/v1/projects/{project}/goals")[1]["goals"]
    assert goals_before, "the run had a goal"
    messages_before = studio.api.get(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages")[1]["messages"]

    status, body = studio.api("DELETE", f"/api/v1/projects/{project}/runs/{run_id}")
    assert status == 200, body
    assert body["deleted"] == run_id

    # The run is gone from every listing.
    assert run_id not in [r["id"] for r in
                          studio.api.get(f"/api/v1/projects/{project}/runs?archived=1")[1]["runs"]]
    assert studio.api.get(f"/api/v1/projects/{project}/runs/{run_id}")[0] == 404
    # The goal and the specification are still there.
    assert studio.api.get(f"/api/v1/projects/{project}/goals")[1]["goals"] == goals_before
    # The chat and its messages are untouched.
    messages_after = studio.api.get(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages")[1]["messages"]
    assert len(messages_after) == len(messages_before)
    # The repository binding is untouched.
    assert studio.api.get(f"/api/v1/projects/{project}/binding")[1]["binding"]["full_name"] == REPO


def test_an_active_run_cannot_be_removed_from_history(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    run_id, _ = _start_run(studio, project)
    status, body = studio.api("DELETE", f"/api/v1/projects/{project}/runs/{run_id}")
    assert status == 409
    assert "cancel" in body["error"]


def test_removing_a_run_keeps_the_evidence(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    run_id, _ = _start_run(studio, project)
    # Write an evidence row that names the run's task, as the loop would.
    with studio.app.store._tx() as cur:
        cur.execute("SELECT id FROM nexus_run_tasks WHERE run_id=%s LIMIT 1", (run_id,))
        task_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO nexus_evidence(id, project_id, format_version, subject_kind, "
            "  subject_id, body, signed_payload, key_id, signature, verdict, entry_hash, "
            "  created_by) VALUES (gen_random_uuid(), %s, 2, 'task', %s, '{}'::jsonb, "
            "  '{}', 'k', 'sig', 'UNVERIFIED', 'h', 'tester')", (project, task_id))
    _settle(studio, run_id, state="completed")
    assert studio.api("DELETE", f"/api/v1/projects/{project}/runs/{run_id}")[0] == 200
    with studio.app.store._tx() as cur:
        cur.execute("SELECT count(*) AS n FROM nexus_evidence WHERE subject_id=%s", (task_id,))
        assert cur.fetchone()["n"] == 1, "evidence must survive the run's removal"


# ------------------------------------------------------- retry failed tasks
def test_retry_failed_requeues_only_the_unsuccessful_tasks(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    run_id, _ = _start_run(studio, project)
    # Settle task A as verified, task B as failed, and the run as failed.
    with studio.app.store._tx() as cur:
        cur.execute("SELECT id, task_key FROM nexus_run_tasks WHERE run_id=%s "
                    "ORDER BY created_at, id", (run_id,))
        rows = cur.fetchall()
        a, b = str(rows[0]["id"]), str(rows[1]["id"])
        cur.execute("UPDATE nexus_run_tasks SET state='completed_verified' WHERE id=%s", (a,))
        cur.execute("UPDATE nexus_run_tasks SET state='failed' WHERE id=%s", (b,))
        cur.execute("UPDATE nexus_runs SET state='failed' WHERE id=%s", (run_id,))

    status, body = studio.api.post(
        f"/api/v1/projects/{project}/runs/{run_id}/retry-failed", {})
    assert status == 200, body
    assert body["run"]["state"] == "running"
    assert body["run"]["retried"] == 1

    tasks = {t["id"]: t for t in
             studio.api.get(f"/api/v1/projects/{project}/runs/{run_id}")[1]["tasks"]}
    assert tasks[a]["state"] == "completed_verified", "a verified task is never requeued"
    assert tasks[b]["state"] == "pending"


def test_retry_failed_with_nothing_to_retry_is_refused(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    run_id, _ = _start_run(studio, project)
    with studio.app.store._tx() as cur:
        cur.execute("UPDATE nexus_run_tasks SET state='completed_verified' WHERE run_id=%s",
                    (run_id,))
        cur.execute("UPDATE nexus_runs SET state='completed' WHERE id=%s", (run_id,))
    status, body = studio.api.post(
        f"/api/v1/projects/{project}/runs/{run_id}/retry-failed", {})
    assert status == 409
    assert "verified" in body["error"]


def test_retry_failed_on_an_active_run_is_refused(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    run_id, _ = _start_run(studio, project)
    status, body = studio.api.post(
        f"/api/v1/projects/{project}/runs/{run_id}/retry-failed", {})
    assert status == 409
    assert "not over" in body["error"]


# ------------------------------------------------------- restart / duplicate
def _tasks(studio, project, run_id):
    """The run's tasks keyed by their plan id (``task_key``)."""
    body = studio.api.get(f"/api/v1/projects/{project}/runs/{run_id}")[1]
    return {t["task_key"]: t for t in body["tasks"]}


def test_restart_run_re_runs_the_whole_plan_as_a_new_run(studio):
    """Restart re-runs *every* task as a new run under the same goal.

    Unlike retry-failed (same run, only the unsuccessful tasks), a restart is a
    fresh run of the whole plan: all tasks pending with attempt 0, and the old
    run left untouched as history.
    """
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    run_id, _ = _start_run(studio, project)
    # Settle the run: A verified, B failed, run failed.
    with studio.app.store._tx() as cur:
        cur.execute("SELECT id, task_key FROM nexus_run_tasks WHERE run_id=%s "
                    "ORDER BY created_at, id", (run_id,))
        rows = cur.fetchall()
        a = str(rows[0]["id"])
        a_key = rows[0]["task_key"]
        cur.execute("UPDATE nexus_run_tasks SET state='completed_verified', attempt=2 "
                    "WHERE id=%s", (a,))
        cur.execute("UPDATE nexus_runs SET state='failed' WHERE id=%s", (run_id,))

    status, body = studio.api.post(
        f"/api/v1/projects/{project}/runs/{run_id}/restart", {})
    assert status == 200, body
    new_run = body["run"]
    assert new_run["state"] == "running"
    assert new_run["restarted_from"] == run_id
    assert new_run["id"] != run_id
    # A fresh run of the same goal, every task pending at attempt 0.
    new_tasks = _tasks(studio, project, new_run["id"])
    assert set(new_tasks) == {"a", "b"}
    assert all(t["state"] == "pending" and t["attempt"] == 0 for t in new_tasks.values())
    # The old run is untouched: still failed, its verified task still verified.
    old = studio.api.get(f"/api/v1/projects/{project}/runs/{run_id}")[1]["run"]
    assert old["state"] == "failed"
    assert _tasks(studio, project, run_id)[a_key]["state"] == "completed_verified"


def test_restart_run_refuses_an_active_run(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    run_id, _ = _start_run(studio, project)
    status, body = studio.api.post(
        f"/api/v1/projects/{project}/runs/{run_id}/restart", {})
    assert status == 409
    assert "not over" in body["error"]


def test_duplicate_run_makes_an_independent_goal_and_run(studio):
    """Duplicate copies the plan into a *new* goal and run.

    A run has at most one active run per goal, so a copy that had to run
    alongside the original must live under its own goal; the original is kept.
    """
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    run_id, _ = _start_run(studio, project)
    with studio.app.store._tx() as cur:
        cur.execute("UPDATE nexus_runs SET state='completed' WHERE id=%s", (run_id,))

    status, body = studio.api.post(
        f"/api/v1/projects/{project}/runs/{run_id}/duplicate", {})
    assert status == 200, body
    copy = body["run"]
    assert copy["id"] != run_id
    assert copy["goal_id"] != studio.api.get(
        f"/api/v1/projects/{project}/runs/{run_id}")[1]["run"]["goal_id"]
    assert copy["duplicated_from"] == run_id
    # The copy has the same plan, all pending.
    new_tasks = _tasks(studio, project, copy["id"])
    assert set(new_tasks) == {"a", "b"}
    assert all(t["state"] == "pending" for t in new_tasks.values())
    # The original goal still exists and the original run is untouched.
    assert studio.api.get(f"/api/v1/projects/{project}/runs/{run_id}")[1]["run"]["state"] \
        == "completed"


def test_duplicate_run_is_idempotent_with_a_key(studio):
    """A retried duplicate with the same key returns the same copy, not a second."""
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    run_id, _ = _start_run(studio, project)
    with studio.app.store._tx() as cur:
        cur.execute("UPDATE nexus_runs SET state='failed' WHERE id=%s", (run_id,))
    first = studio.api.post(f"/api/v1/projects/{project}/runs/{run_id}/duplicate",
                            {"idempotency_key": "dup-1"})
    assert first[0] == 200, first
    second = studio.api.post(f"/api/v1/projects/{project}/runs/{run_id}/duplicate",
                             {"idempotency_key": "dup-1"})
    assert second[0] == 200, second
    assert first[1]["run"]["id"] == second[1]["run"]["id"]
    assert second[1]["run"]["created"] is False


def test_restart_and_duplicate_are_member_only(studio):
    """A non-member cannot restart or duplicate another project's run."""
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    run_id, _ = _start_run(studio, project)
    with studio.app.store._tx() as cur:
        cur.execute("UPDATE nexus_runs SET state='failed' WHERE id=%s", (run_id,))
    # A foreign principal is a different member set; both routes are project
    # scoped, so a non-member gets "not found", never a 200.
    from orchestration.nexus_lab_store import LabStore
    from orchestration.cloud_store import NotFound
    lab = LabStore(studio.app.store)
    for call in (lambda: lab.restart_run("user:stranger", project, run_id),
                 lambda: lab.duplicate_run("user:stranger", project, run_id)):
        with pytest.raises(NotFound):
            call()
