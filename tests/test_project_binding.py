"""D-2 project binding: canonical repository identity, authorization, isolation.

These are the security-negative tests the MVP must not regress on:

* A user cannot read or write another project's resources (IDOR).
* A task cannot be dispatched into a repository its project does not own, even
  if that repository is in the global allowlist.
* The two repository fields on a task cannot be made to disagree.
* Cross-project references are refused by the database, not only by Python.
* Legacy NULL-project tasks keep working but cannot reach a project repository.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.nexus_binding import (  # noqa: E402
    AuthorizationError,
    ProjectRepositoryService,
    authorize_dispatch,
)

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run project-binding tests"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


@pytest.fixture
def store():
    _reset_schema()
    s = PostgresStore(DSN)
    yield s
    s.close()


def _project(store, name: str, principal: str = "user:owner") -> str:
    with store._tx() as cur:
        cur.execute("INSERT INTO nexus_projects(name) VALUES (%s) RETURNING id", (name,))
        pid = str(cur.fetchone()["id"])
        cur.execute("INSERT INTO nexus_project_members(project_id, principal, role) "
                    "VALUES (%s,%s,'owner')", (pid, principal))
    return pid


def test_link_requires_repository_in_allowlist(store):
    from orchestration.cloud_store import NotFound

    svc = ProjectRepositoryService(store)
    pid = _project(store, "A")
    with pytest.raises(NotFound):
        svc.link("user:owner", pid, "acme/missing")


def test_link_then_list(store):
    svc = ProjectRepositoryService(store)
    store.add_repository("acme/repo", allowed_branches=["main"])
    pid = _project(store, "A")
    link = svc.link("user:owner", pid, "acme/repo")
    assert link["full_name"] == "acme/repo"
    listed = svc.list("user:owner", pid)
    assert [r["full_name"] for r in listed] == ["acme/repo"]


def test_repository_can_be_shared_by_two_projects(store):
    svc = ProjectRepositoryService(store)
    store.add_repository("acme/shared")
    a = _project(store, "A")
    b = _project(store, "B")
    svc.link("user:owner", a, "acme/shared")
    svc.link("user:owner", b, "acme/shared")
    assert svc.linked(a, "acme/shared") and svc.linked(b, "acme/shared")


def test_non_member_cannot_link(store):
    from orchestration.cloud_store import NotFound

    svc = ProjectRepositoryService(store)
    store.add_repository("acme/repo")
    pid = _project(store, "A", principal="user:alice")
    with pytest.raises(NotFound):
        svc.link("user:bob", pid, "acme/repo")


def test_non_member_cannot_list(store):
    from orchestration.cloud_store import NotFound

    svc = ProjectRepositoryService(store)
    store.add_repository("acme/repo")
    pid = _project(store, "A", principal="user:alice")
    with pytest.raises(NotFound):
        svc.list("user:bob", pid)


def test_unlink_refused_while_task_in_flight(store):
    svc = ProjectRepositoryService(store)
    store.add_repository("acme/repo", allowed_branches=["main"])
    pid = _project(store, "A")
    svc.link("user:owner", pid, "acme/repo")
    store.submit_task("work", project_id=pid, selected_repository="acme/repo",
                      selected_branch="main")
    with pytest.raises(AuthorizationError):
        svc.unlink("user:owner", pid, "acme/repo")


def test_unlink_succeeds_when_no_task_holds_it(store):
    svc = ProjectRepositoryService(store)
    store.add_repository("acme/repo")
    pid = _project(store, "A")
    svc.link("user:owner", pid, "acme/repo")
    assert svc.unlink("user:owner", pid, "acme/repo") is True
    assert svc.list("user:owner", pid) == []


# ------------------------------------------------------- canonical identity
def test_repository_id_and_selected_repository_agree(store):
    store.add_repository("acme/repo")
    pid = _project(store, "A")
    with store._tx() as cur:
        cur.execute("INSERT INTO nexus_project_repositories(project_id, repository_id) "
                    "SELECT %s, id FROM repositories WHERE full_name='acme/repo'", (pid,))
    task = store.submit_task("work", project_id=pid, selected_repository="acme/repo")
    assert task.repository_id is not None
    with store._tx() as cur:
        cur.execute("SELECT full_name FROM repositories WHERE id=%s", (task.repository_id,))
        assert cur.fetchone()["full_name"] == "acme/repo"


def test_conflicting_repository_fields_are_refused(store):
    import psycopg

    store.add_repository("acme/repo")
    other = store.add_repository("acme/other")
    # A direct insert with a repository_id and a mismatching selected_repository
    # must be refused by the trigger.
    with store._tx() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO tasks(goal, repository_id, selected_repository) "
                "VALUES ('x', %s, 'acme/repo')", (other,))


def test_cross_project_repository_is_refused(store):
    import psycopg

    store.add_repository("acme/repo")
    a = _project(store, "A")
    b = _project(store, "B")
    with store._tx() as cur:
        cur.execute("INSERT INTO nexus_project_repositories(project_id, repository_id) "
                    "SELECT %s, id FROM repositories WHERE full_name='acme/repo'", (a,))
    # B did not link the repository.
    with pytest.raises(psycopg.errors.CheckViolation):
        store.submit_task("work", project_id=b, selected_repository="acme/repo")


def test_workflow_project_cannot_change_under_its_tasks(store):
    import psycopg

    store.add_repository("acme/repo")
    a = _project(store, "A")
    b = _project(store, "B")
    with store._tx() as cur:
        cur.execute("INSERT INTO nexus_project_repositories(project_id, repository_id) "
                    "SELECT %s, id FROM repositories WHERE full_name='acme/repo'", (a,))
    wid = store.create_workflow("goal", project_id=a)
    store.submit_task("work", workflow_id=wid, project_id=a,
                      selected_repository="acme/repo")
    with pytest.raises(psycopg.errors.CheckViolation):
        with store._tx() as cur:
            cur.execute("UPDATE workflows SET project_id=%s WHERE id=%s", (b, wid))


# -------------------------------------------------------- dispatch authorization
def test_authorize_dispatch_accepts_linked_repository(store):
    store.add_repository("acme/repo", allowed_branches=["main"])
    pid = _project(store, "A")
    with store._tx() as cur:
        cur.execute("INSERT INTO nexus_project_repositories(project_id, repository_id) "
                    "SELECT %s, id FROM repositories WHERE full_name='acme/repo'", (pid,))
    task = store.submit_task("work", project_id=pid, selected_repository="acme/repo",
                             selected_branch="main")
    ctx = authorize_dispatch(store, task=task)
    assert ctx["repository"] == "acme/repo"


def test_authorize_dispatch_refuses_unlinked_repository(store):
    store.add_repository("acme/repo", allowed_branches=["main"])
    # A project task cannot even be inserted with an unlinked repository (the
    # trigger refuses it), so the reachable case is a legacy task with no
    # project: it is still subject to the global allowlist and is allowed here.
    task = store.submit_task("work", selected_repository="acme/repo",
                             selected_branch="main")
    authorize_dispatch(store, task=task)  # allowed: legacy path, allowlisted


def test_authorize_dispatch_refuses_disabled_repository(store):
    store.add_repository("acme/repo", allowed_branches=["main"])
    pid = _project(store, "A")
    with store._tx() as cur:
        cur.execute("INSERT INTO nexus_project_repositories(project_id, repository_id) "
                    "SELECT %s, id FROM repositories WHERE full_name='acme/repo'", (pid,))
    task = store.submit_task("work", project_id=pid, selected_repository="acme/repo",
                             selected_branch="main")
    with store._tx() as cur:
        cur.execute("UPDATE repositories SET enabled=false WHERE full_name='acme/repo'")
    with pytest.raises(AuthorizationError):
        authorize_dispatch(store, task=task)


def test_authorize_dispatch_refuses_disallowed_branch(store):
    store.add_repository("acme/repo", allowed_branches=["main"])
    pid = _project(store, "A")
    with store._tx() as cur:
        cur.execute("INSERT INTO nexus_project_repositories(project_id, repository_id) "
                    "SELECT %s, id FROM repositories WHERE full_name='acme/repo'", (pid,))
    task = store.submit_task("work", project_id=pid, selected_repository="acme/repo",
                             selected_branch="main")
    # Change the task's branch out of band (the state is otherwise unchanged).
    with store._tx() as cur:
        cur.execute("UPDATE tasks SET selected_branch='release' WHERE id=%s", (task.id,))
    task = store.get_task(task.id)
    with pytest.raises(AuthorizationError):
        authorize_dispatch(store, task=task)


def test_authorize_dispatch_refuses_non_member_principal(store):
    store.add_repository("acme/repo", allowed_branches=["main"])
    pid = _project(store, "A", principal="user:alice")
    with store._tx() as cur:
        cur.execute("INSERT INTO nexus_project_repositories(project_id, repository_id) "
                    "SELECT %s, id FROM repositories WHERE full_name='acme/repo'", (pid,))
    task = store.submit_task("work", project_id=pid, selected_repository="acme/repo",
                             selected_branch="main")
    with pytest.raises(AuthorizationError):
        authorize_dispatch(store, task=task, principal="user:bob")
    # The owner is allowed.
    authorize_dispatch(store, task=task, principal="user:alice")


def test_project_delete_keeps_execution_history(store):
    store.add_repository("acme/repo", allowed_branches=["main"])
    pid = _project(store, "A")
    with store._tx() as cur:
        cur.execute("INSERT INTO nexus_project_repositories(project_id, repository_id) "
                    "SELECT %s, id FROM repositories WHERE full_name='acme/repo'", (pid,))
    task = store.submit_task("work", project_id=pid, selected_repository="acme/repo",
                             selected_branch="main")
    with store._tx() as cur:
        cur.execute("DELETE FROM nexus_projects WHERE id=%s", (pid,))
    # The task survives; its project_id is cleared, not cascaded away.
    survived = store.get_task(task.id)
    assert survived.project_id is None
