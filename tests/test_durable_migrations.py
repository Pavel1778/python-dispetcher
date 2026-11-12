"""Migrations 0028-0030 on databases that already hold something.

A migration that is only ever applied to an empty schema proves that its SQL
parses. These apply them where it matters:

* to a database with **runs, projects and links from before** - what the live
  service has: a running run becomes durable, a cancelled one leaves the
  interface, a project's bound connection becomes its primary executor and its
  bound repository its main one, and nothing is lost;
* to a database that carries the **draft of this work** (pull request #29's
  ``0026_durable_autonomy.sql``, were it merged first): its report table, its
  ``retrying`` runs and its project executors are brought over, not dropped;
* **twice**: every file can be applied again without changing anything.
"""
from __future__ import annotations

import os
import sys

import psycopg
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.nexus_binding import ProjectRepositoryService  # noqa: E402
from orchestration.nexus_executors import ProjectExecutorService  # noqa: E402
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_openhands_accounts import OpenHandsAccountService  # noqa: E402
from orchestration.nexus_reports import ReportService  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
NEW = ("0028_durable_runs.sql", "0029_openhands_connections.sql",
       "0030_project_repositories.sql")
OWNER = "user:owner"

#: The schema of the draft (PR #29, ``0026_durable_autonomy.sql``), as it would
#: stand in a database where that pull request had been merged and deployed.
DRAFT = """
ALTER TABLE nexus_runs DROP CONSTRAINT IF EXISTS nexus_runs_state_check;
ALTER TABLE nexus_runs ADD CONSTRAINT nexus_runs_state_check CHECK (state IN (
    'running', 'paused', 'waiting_for_approval', 'blocked', 'retrying',
    'completed', 'failed', 'cancelled'));
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS retry_attempt integer NOT NULL DEFAULT 0;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS max_retry_cycles integer NOT NULL DEFAULT 5;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS retry_at timestamptz;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS retry_reason text;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS total_iterations integer NOT NULL DEFAULT 0;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS hidden_at timestamptz;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS cancel_requested_at timestamptz;
ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS source_conversation_id uuid
    REFERENCES nexus_conversations(id) ON DELETE SET NULL;
ALTER TABLE nexus_run_tasks ADD COLUMN IF NOT EXISTS cycle integer NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS nexus_run_reports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL UNIQUE REFERENCES nexus_runs(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    conversation_id uuid REFERENCES nexus_conversations(id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    content text NOT NULL DEFAULT '' CHECK (length(content) <= 60000),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_nexus_run_reports_pending
    ON nexus_run_reports(status, next_attempt_at) WHERE status = 'pending';
CREATE TABLE IF NOT EXISTS nexus_project_openhands (
    project_id uuid NOT NULL REFERENCES nexus_projects(id) ON DELETE CASCADE,
    account_id uuid NOT NULL REFERENCES nexus_openhands_accounts(id) ON DELETE CASCADE,
    role text NOT NULL DEFAULT 'fallback' CHECK (role IN ('primary', 'fallback')),
    position integer NOT NULL DEFAULT 0 CHECK (position >= 0),
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL DEFAULT 'system',
    PRIMARY KEY (project_id, account_id)
);
ALTER TABLE nexus_openhands_accounts ADD COLUMN IF NOT EXISTS api_version text;
ALTER TABLE nexus_project_repositories ADD COLUMN IF NOT EXISTS github_account_id uuid
    REFERENCES nexus_github_accounts(id) ON DELETE SET NULL;
ALTER TABLE nexus_project_repositories ADD COLUMN IF NOT EXISTS is_primary boolean
    NOT NULL DEFAULT false;
ALTER TABLE nexus_project_repositories ADD COLUMN IF NOT EXISTS label text NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_nexus_project_repositories_primary
    ON nexus_project_repositories(project_id) WHERE is_primary;
"""


def apply(conn, names) -> None:
    for name in names:
        conn.execute(open(os.path.join(_MIG, name)).read())


@pytest.fixture
def old():
    """A database as it was before this work: every earlier migration, none of
    the new ones. Yields a connection; the store is opened by the test once the
    schema is what it wants."""
    with psycopg.connect(DSN, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        apply(conn, [name for name in sorted(os.listdir(_MIG))
                     if name.endswith(".sql") and name not in NEW])
        yield conn


def seed(conn) -> dict:
    """A project with a bound repository and a bound OpenHands connection, a
    chat, and three runs: one at work, one cancelled, one completed."""
    project = conn.execute("INSERT INTO nexus_projects(name) VALUES ('Shop') "
                           "RETURNING id").fetchone()["id"]
    conn.execute("INSERT INTO nexus_project_members(project_id, principal, role) "
                 "VALUES (%s, %s, 'owner')", (project, OWNER))
    repo = conn.execute("INSERT INTO repositories(full_name, default_branch, allowed_branches) "
                        "VALUES ('acme/shop', 'main', ARRAY['main', 'dev']) "
                        "RETURNING id").fetchone()["id"]
    other = conn.execute("INSERT INTO repositories(full_name, default_branch) "
                         "VALUES ('acme/docs', 'main') RETURNING id").fetchone()["id"]
    for repository in (repo, other):
        conn.execute("INSERT INTO nexus_project_repositories(project_id, repository_id) "
                     "VALUES (%s, %s)", (project, repository))
    account = conn.execute(
        "INSERT INTO nexus_openhands_accounts(principal, label, base_url, transport, "
        "  api_key_encrypted) VALUES (%s, 'main', 'https://app.all-hands.dev', 'app', %s) "
        "RETURNING id", (OWNER, b"ciphertext")).fetchone()["id"]
    conn.execute("UPDATE nexus_projects SET repository_id=%s, selected_branch='dev', "
                 "openhands_account_id=%s WHERE id=%s", (repo, account, project))
    chat = conn.execute("INSERT INTO nexus_conversations(project_id, title, mode, created_by) "
                        "VALUES (%s, 'Chat', 'autonomous', %s) RETURNING id",
                        (project, OWNER)).fetchone()["id"]
    runs = {}
    for state in ("running", "cancelled", "completed"):
        goal = conn.execute("INSERT INTO nexus_goals(project_id, text, created_by) "
                            "VALUES (%s, %s, %s) RETURNING id",
                            (project, f"Goal that is {state}", OWNER)).fetchone()["id"]
        run = conn.execute("INSERT INTO nexus_runs(project_id, goal_id, state, iteration, "
                           "  created_by) VALUES (%s, %s, %s, 3, %s) RETURNING id",
                           (project, goal, state, OWNER)).fetchone()["id"]
        conn.execute("INSERT INTO nexus_run_tasks(run_id, project_id, task_key, title, state, "
                     "  attempt, evidence) VALUES (%s, %s, 't1', 'Task', %s, 1, "
                     "  '[{\"note\": \"kept\"}]'::jsonb)",
                     (run, project, "running" if state == "running" else "failed"))
        runs[state] = str(run)
    conn.execute("UPDATE nexus_conversations SET run_id=%s WHERE id=%s", (runs["running"], chat))
    return {"project": str(project), "account": str(account), "chat": str(chat), "runs": runs}


def test_a_database_from_before_keeps_everything_and_gains_the_new_rules(old):
    ids = seed(old)
    before = old.execute("SELECT count(*) AS n FROM nexus_run_tasks").fetchone()["n"]
    apply(old, NEW)

    runs = {str(r["id"]): r for r in old.execute("SELECT * FROM nexus_runs").fetchall()}
    running, cancelled, completed = (runs[ids["runs"][s]] for s in
                                     ("running", "cancelled", "completed"))
    # A run that is alive goes on under the new rules, and reports to its chat.
    assert (running["policy"], running["total_iterations"]) == ("durable", 3)
    assert str(running["conversation_id"]) == ids["chat"] and running["hidden_at"] is None
    # A run cancelled earlier leaves the interface; nothing about it is removed.
    assert cancelled["hidden_at"] is not None and cancelled["cancelled_at"] is not None
    assert cancelled["policy"] == "bounded"
    # A finished run is left exactly as it was.
    assert (completed["policy"], completed["hidden_at"]) == ("bounded", None)
    tasks = old.execute("SELECT state, evidence, cycle, retry_at FROM nexus_run_tasks").fetchall()
    assert len(tasks) == before
    assert all(t["evidence"] == [{"note": "kept"}] and t["cycle"] == 0 for t in tasks)

    # The connection the project was bound to is its primary executor.
    executors = old.execute("SELECT account_id, position, enabled FROM nexus_project_executors "
                            "WHERE project_id=%s", (ids["project"],)).fetchall()
    assert [(str(e["account_id"]), e["position"], e["enabled"]) for e in executors] == [
        (ids["account"], 0, True)]
    # The repository the project was bound to is its main one, on its branch.
    links = old.execute(
        "SELECT r.full_name, pr.is_primary, pr.branch, pr.enabled, pr.github_account_id "
        "FROM nexus_project_repositories pr JOIN repositories r ON r.id = pr.repository_id "
        "WHERE pr.project_id=%s ORDER BY r.full_name", (ids["project"],)).fetchall()
    assert [(x["full_name"], x["is_primary"], x["branch"], x["enabled"]) for x in links] == [
        ("acme/docs", False, "", True), ("acme/shop", True, "dev", True)]

    # And the code reads it as that.
    store = PostgresStore(DSN)
    try:
        listed = LabStore(store).list_runs(OWNER, ids["project"])
        assert sorted(r["state"] for r in listed) == ["completed", "running"]
        assert len(LabStore(store).list_runs(OWNER, ids["project"], include_hidden=True)) == 3
        state = ProjectExecutorService(store, OpenHandsAccountService(store)).get(
            OWNER, ids["project"])
        assert [(e["label"], e["role"]) for e in state["executors"]] == [("main", "primary")]
        repositories = ProjectRepositoryService(store).list(OWNER, ids["project"])
        assert [(r["full_name"], r["is_primary"], r["working_branch"]) for r in repositories] \
            == [("acme/shop", True, "dev"), ("acme/docs", False, "main")]
    finally:
        store.close()


def test_every_new_migration_can_be_applied_again(old):
    ids = seed(old)
    apply(old, NEW)

    def snapshot():
        return {
            "runs": old.execute("SELECT id, state, policy, hidden_at, cancelled_at, "
                                "conversation_id, total_iterations FROM nexus_runs "
                                "ORDER BY id").fetchall(),
            "executors": old.execute("SELECT * FROM nexus_project_executors").fetchall(),
            "links": old.execute("SELECT project_id, repository_id, is_primary, branch, enabled "
                                 "FROM nexus_project_repositories ORDER BY repository_id"
                                 ).fetchall(),
        }

    first = snapshot()
    # A person hides nothing and cancels nothing in between; a second run of
    # the files must not decide anything again (e.g. un-hide, or make a run
    # that was started as bounded durable).
    old.execute("UPDATE nexus_runs SET policy='bounded' WHERE id=%s", (ids["runs"]["running"],))
    first["runs"] = snapshot()["runs"]
    apply(old, NEW)
    apply(old, NEW)
    assert snapshot() == first


def test_a_database_that_carries_the_draft_is_brought_over_not_dropped(old):
    ids = seed(old)
    old.execute(DRAFT)
    # What the draft would have written: a report that is already in its chat,
    # one that is not yet, a run waiting for its next cycle, and a project list.
    done, waiting = ids["runs"]["completed"], ids["runs"]["running"]
    old.execute("INSERT INTO nexus_run_reports(run_id, project_id, conversation_id, status, "
                "  content, meta, attempts, sent_at) VALUES (%s, %s, %s, 'sent', "
                "  'Отчёт: сделано.', '{\"tasks\": 1}'::jsonb, 1, now())",
                (done, ids["project"], ids["chat"]))
    old.execute("INSERT INTO nexus_run_reports(run_id, project_id, conversation_id, content) "
                "VALUES (%s, %s, %s, 'Отчёт: ждёт отправки.')",
                (ids["runs"]["cancelled"], ids["project"], ids["chat"]))
    old.execute("UPDATE nexus_runs SET state='retrying', retry_at = now() + interval '10 minutes',"
                " retry_reason='cycle 2', source_conversation_id=%s WHERE id=%s",
                (ids["chat"], waiting))
    second = old.execute(
        "INSERT INTO nexus_openhands_accounts(principal, label, base_url, transport, "
        "  api_key_encrypted) VALUES (%s, 'backup', 'https://app.all-hands.dev', 'app', %s) "
        "RETURNING id", (OWNER, b"ciphertext")).fetchone()["id"]
    old.execute("UPDATE nexus_projects SET openhands_account_id=NULL WHERE id=%s",
                (ids["project"],))
    old.execute("INSERT INTO nexus_project_openhands(project_id, account_id, role, position, "
                "  enabled) VALUES (%s, %s, 'fallback', 0, false), (%s, %s, 'primary', 0, true)",
                (ids["project"], ids["account"], ids["project"], second))

    apply(old, NEW)
    apply(old, NEW)                                # and once more, to no effect

    # The reports are there, in the shape this code reads, delivered ones still
    # delivered - so nothing is sent to the chat a second time.
    store = PostgresStore(DSN)
    try:
        reports = ReportService(store)
        sent = reports.get(OWNER, ids["project"], done)
        assert (sent["state"], sent["body"], sent["summary"]) == (
            "delivered", "Отчёт: сделано.", {"tasks": 1})
        assert sent["delivered_at"] is not None
        pending = reports.get(OWNER, ids["project"], ids["runs"]["cancelled"])
        assert (pending["state"], pending["body"]) == ("pending", "Отчёт: ждёт отправки.")
        # The draft's table is kept beside it, untouched.
        assert old.execute("SELECT count(*) AS n FROM nexus_run_reports_draft").fetchone()["n"] == 2

        # A run that was "retrying" is a durable run that goes on at that time.
        run = LabStore(store).run_row(waiting)
        assert (run["state"], run["policy"], run["wait_reason"]) == ("running", "durable", "cycle 2")
        assert run["resume_at"] is not None and str(run["conversation_id"]) == ids["chat"]

        # The project's list: the draft's primary first, then the rest.
        state = ProjectExecutorService(store, OpenHandsAccountService(store)).get(
            OWNER, ids["project"])
        assert [(e["label"], e["role"], e["enabled"]) for e in state["executors"]] == [
            ("backup", "primary", True), ("main", "fallback", False)]
        bound = old.execute("SELECT openhands_account_id FROM nexus_projects WHERE id=%s",
                            (ids["project"],)).fetchone()["openhands_account_id"]
        assert bound == second
    finally:
        store.close()
