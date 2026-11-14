"""Project and conversation lifecycle: archive, pin, trash (soft delete).

Phase E/F of the NEXUS project/conversation management work (see
``docs/P0-INCIDENT-FINAL-REPORT.md`` for the earlier phases). Before this,
a project could be created, renamed and read, but never archived or put
aside; a conversation could already be archived/restored, but had no pin
and no delete distinct from archive - the existing ``DELETE
/conversations/{id}`` route is, and remains, an alias for archiving (see
``tests/test_conversations.py::test_rename_archive_and_mode_change``).

These tests exercise the service layer directly (``ProjectService``,
``ConversationService``) and the real HTTP server together, over real
PostgreSQL, matching the existing test style in ``tests/test_nexus_projects.py``
and ``tests/test_concurrent_runs_policy.py``.
"""
from __future__ import annotations

import json

import pytest

from orchestration.cloud_store import NotFound, PostgresStore
from orchestration.nexus_conversations import ConversationService
from orchestration.nexus_projects import ProjectService
from tests.test_studio_api import DSN, REPO, studio_stack

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run these tests")


@pytest.fixture
def store():
    instance = PostgresStore(DSN)
    with instance._tx() as cur:
        cur.execute(
            "TRUNCATE nexus_agent_sessions, nexus_conversation_attachments, "
            "nexus_conversation_messages, nexus_conversations, "
            "nexus_openhands_pool, nexus_openhands_accounts, nexus_evidence, "
            "nexus_project_repositories, "
            "nexus_voice_notes, nexus_pending_actions, nexus_workspace_files, "
            "nexus_workspaces, nexus_files, nexus_plan_dependencies, nexus_plan_tasks, "
            "nexus_plans, nexus_project_events, nexus_goals, nexus_project_members, "
            "nexus_projects, nexus_runs RESTART IDENTITY CASCADE"
        )
    yield instance
    instance.close()


# --------------------------------------------------------------- project archive
def test_project_archive_hides_from_default_list_and_restore_undoes_it(store):
    svc = ProjectService(store)
    alice = "user:alice"
    a = str(svc.create(alice, "Alpha")["id"])
    b = str(svc.create(alice, "Beta")["id"])

    archived = svc.set_archived(alice, a, True)
    assert archived["archived_at"] is not None

    visible = {str(p["id"]) for p in svc.list(alice)}
    assert visible == {b}, "an archived project must drop out of the default list"

    with_archived = {str(p["id"]) for p in svc.list(alice, include_archived=True)}
    assert with_archived == {a, b}, "include_archived must still show it"

    restored = svc.set_archived(alice, a, False)
    assert restored["archived_at"] is None
    assert {str(p["id"]) for p in svc.list(alice)} == {a, b}

    # Archiving touches nothing about Beta.
    assert svc.get(alice, b)["archived_at"] is None


def test_project_archive_requires_admin_scope_and_membership(store):
    svc = ProjectService(store)
    alice, bob = "user:alice", "user:bob"
    a = str(svc.create(alice, "Alpha")["id"])
    with pytest.raises(NotFound):
        svc.set_archived(bob, a, True)
    with pytest.raises(NotFound):
        svc.delete(bob, a)


# ---------------------------------------------------------------- project trash
def test_project_delete_is_soft_restorable_and_isolated(store):
    svc = ProjectService(store)
    alice = "user:alice"
    a = str(svc.create(alice, "Alpha", "to be deleted")["id"])
    b = str(svc.create(alice, "Beta", "must stay untouched")["id"])

    deleted = svc.delete(alice, a)
    assert deleted["deleted_at"] is not None

    # Gone from the default list and from the archived-only view; only the
    # trash view shows it.
    assert {str(p["id"]) for p in svc.list(alice)} == {b}
    assert {str(p["id"]) for p in svc.list(alice, include_archived=True)} == {b}
    assert {str(p["id"]) for p in svc.list(alice, include_deleted=True)} == {a, b}

    # Nothing about Beta changed: still present, still with its own data.
    beta = svc.get(alice, b)
    assert beta["deleted_at"] is None and beta["description"] == "must stay untouched"

    # Repeat delete is idempotent, not an error.
    again = svc.delete(alice, a)
    assert again["deleted_at"] is not None

    restored = svc.restore(alice, a)
    assert restored["deleted_at"] is None
    assert {str(p["id"]) for p in svc.list(alice)} == {a, b}


def test_project_delete_refused_while_an_active_run_exists(store):
    svc = ProjectService(store)
    alice = "user:alice"
    a = str(svc.create(alice, "Alpha")["id"])
    with store._tx() as cur:
        cur.execute(
            "INSERT INTO nexus_goals(project_id, text, created_by) VALUES (%s, 'g', %s) "
            "RETURNING id", (a, alice))
        goal_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO nexus_runs(project_id, goal_id, state, created_by) "
            "VALUES (%s, %s, 'running', %s)", (a, goal_id, alice))
    with pytest.raises(ValueError, match="active run"):
        svc.delete(alice, a)

    with store._tx() as cur:
        cur.execute("UPDATE nexus_runs SET state='cancelled' WHERE project_id=%s", (a,))
    # Once the run is no longer active, delete succeeds.
    deleted = svc.delete(alice, a)
    assert deleted["deleted_at"] is not None


def test_deleted_project_refuses_new_conversations_but_keeps_existing_ones(store):
    projects = ProjectService(store)
    conversations = ConversationService(store)
    alice = "user:alice"
    a = str(projects.create(alice, "Alpha")["id"])
    existing = conversations.create(alice, a, title="before delete")["id"]
    projects.delete(alice, a)

    with pytest.raises(ValueError, match="deleted"):
        conversations.create(alice, a, title="after delete")

    # The conversation that already existed is untouched and still readable.
    assert conversations.get(alice, a, existing)["title"] == "before delete"


# ----------------------------------------------------------- conversation pin
def test_conversation_pin_sorts_first_regardless_of_recency(store):
    projects = ProjectService(store)
    conversations = ConversationService(store)
    alice = "user:alice"
    p = str(projects.create(alice, "P")["id"])
    older = conversations.create(alice, p, title="older")["id"]
    newer = conversations.create(alice, p, title="newer")["id"]

    # Without pinning, the more recently updated one (newer) sorts first.
    order = [c["id"] for c in conversations.list(alice, p)]
    assert order[0] == newer

    pinned = conversations.set_pinned(alice, p, older, True)
    assert pinned["pinned"] is True
    order = [c["id"] for c in conversations.list(alice, p)]
    assert order[0] == older, "a pinned conversation must sort before a more recent one"

    # Unpinning (like any other update) also bumps ``updated_at``, so right
    # after unpinning "older" it is briefly the most-recently-touched one too.
    # Touch "newer" again (a rename) to make the comparison meaningful: with
    # neither pinned, plain recency decides, and "newer" wins.
    conversations.set_pinned(alice, p, older, False)
    conversations.rename(alice, p, newer, "newer (renamed)")
    order = [c["id"] for c in conversations.list(alice, p)]
    assert order[0] == newer


# -------------------------------------------------------- conversation trash
def test_conversation_delete_is_distinct_from_archive_and_restorable(store):
    projects = ProjectService(store)
    conversations = ConversationService(store)
    alice = "user:alice"
    p = str(projects.create(alice, "P")["id"])
    keep = conversations.create(alice, p, title="keep")["id"]
    doomed = conversations.create(alice, p, title="doomed")["id"]

    # Archiving alone (the pre-existing capability) must not be affected by
    # the new delete: archive doomed, then un-archive it, to show the two
    # states are independent before deleting it for real.
    conversations.set_archived(alice, p, doomed, True)
    assert conversations.get(alice, p, doomed)["archived"] is True
    conversations.set_archived(alice, p, doomed, False)

    deleted = conversations.delete(alice, p, doomed)
    assert deleted["deleted_at"] is not None

    # Gone from both the default list and the archived-only list; only the
    # trash view (include_deleted) shows it. "keep" is unaffected throughout.
    assert {c["id"] for c in conversations.list(alice, p)} == {keep}
    assert {c["id"] for c in conversations.list(alice, p, include_archived=True)} == {keep}
    assert {c["id"] for c in conversations.list(alice, p, include_deleted=True)} == {keep, doomed}
    assert conversations.get(alice, p, keep)["deleted_at"] is None

    # Repeat delete is idempotent.
    again = conversations.delete(alice, p, doomed)
    assert again["deleted_at"] is not None

    restored = conversations.restore(alice, p, doomed)
    assert restored["deleted_at"] is None
    assert {c["id"] for c in conversations.list(alice, p)} == {keep, doomed}


def test_deleted_conversation_refuses_new_messages(store):
    projects = ProjectService(store)
    conversations = ConversationService(store)
    alice = "user:alice"
    p = str(projects.create(alice, "P")["id"])
    c = conversations.create(alice, p, title="c")["id"]
    conversations.delete(alice, p, c)
    with pytest.raises(ValueError, match="deleted"):
        conversations.add_message(alice, p, c, role="user", content="hello")


def test_conversation_delete_refused_while_its_run_is_active(store):
    projects = ProjectService(store)
    conversations = ConversationService(store)
    alice = "user:alice"
    p = str(projects.create(alice, "P")["id"])
    c = conversations.create(alice, p, title="c")["id"]
    with store._tx() as cur:
        cur.execute(
            "INSERT INTO nexus_goals(project_id, text, created_by) VALUES (%s, 'g', %s) "
            "RETURNING id", (p, alice))
        goal_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO nexus_runs(project_id, goal_id, state, created_by) "
            "VALUES (%s, %s, 'running', %s) RETURNING id", (p, goal_id, alice))
        run_id = cur.fetchone()["id"]
        cur.execute("UPDATE nexus_conversations SET run_id=%s WHERE id=%s", (run_id, c))

    with pytest.raises(ValueError, match="active run"):
        conversations.delete(alice, p, c)

    with store._tx() as cur:
        cur.execute("UPDATE nexus_runs SET state='completed' WHERE id=%s", (run_id,))
    deleted = conversations.delete(alice, p, c)
    assert deleted["deleted_at"] is not None


# --------------------------------------------------------------------- HTTP
def _plan(label: str) -> str:
    return json.dumps({"tasks": [
        {"id": f"t-{label}", "title": f"Задача {label}", "description": "Описание.",
         "acceptance": ["готово"], "dependencies": []},
    ]})


@pytest.fixture
def studio(monkeypatch):
    with studio_stack(monkeypatch) as stack:
        yield stack


def test_http_project_patch_archives_deletes_and_restores(studio):
    project = studio.project("Alpha")
    other = studio.project("Beta")

    status, body = studio.api.patch(f"/api/v1/projects/{project}", {"archived": True})
    assert status == 200 and body["project"]["archived_at"] is not None

    status, body = studio.api.get("/api/v1/projects")
    assert status == 200
    assert {p["id"] for p in body["projects"]} == {other}

    status, body = studio.api.get("/api/v1/projects?archived=1")
    assert {p["id"] for p in body["projects"]} == {project, other}

    status, body = studio.api.patch(f"/api/v1/projects/{project}", {"deleted": True})
    assert status == 200 and body["project"]["deleted_at"] is not None

    status, body = studio.api.get("/api/v1/projects?archived=1")
    assert {p["id"] for p in body["projects"]} == {other}, "deleted project leaked into archived view"

    status, body = studio.api.get("/api/v1/projects?deleted=1")
    assert {p["id"] for p in body["projects"]} == {project, other}

    status, body = studio.api.patch(f"/api/v1/projects/{project}", {"deleted": False})
    assert status == 200 and body["project"]["deleted_at"] is None

    status, body = studio.api.patch(f"/api/v1/projects/{project}",
                                    {"name": "Alpha 2", "archived": False})
    assert status == 200
    assert body["project"]["name"] == "Alpha 2" and body["project"]["archived_at"] is None


def test_http_project_delete_refused_with_active_run_then_succeeds_after_cancel(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = studio.conversation(project, mode="auto")
    studio.answers(_plan("A"))
    status, body = studio.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages",
        {"text": "Реализуй функцию А"})
    assert status == 200, body
    run_id = next(m["meta"]["run_id"] for m in body["messages"] if m["meta"].get("run_id"))

    status, body = studio.api.patch(f"/api/v1/projects/{project}", {"deleted": True})
    assert status == 400, body
    assert "active run" in body["error"]

    assert studio.api.post(f"/api/v1/projects/{project}/runs/{run_id}/cancel", {})[0] == 200
    status, body = studio.api.patch(f"/api/v1/projects/{project}", {"deleted": True})
    assert status == 200 and body["project"]["deleted_at"] is not None


def test_http_conversation_patch_pin_and_delete_are_distinct_from_archive(studio):
    project = studio.project()
    keep = studio.conversation(project, title="keep")
    doomed = studio.conversation(project, title="doomed")

    status, body = studio.api.patch(
        f"/api/v1/projects/{project}/conversations/{keep}", {"pinned": True})
    assert status == 200 and body["conversation"]["pinned"] is True

    status, body = studio.api.get(f"/api/v1/projects/{project}/conversations")
    assert status == 200
    assert body["conversations"][0]["id"] == keep, "pinned conversation must list first"

    # The DELETE verb still only archives - unchanged, existing behaviour.
    status, _ = studio.api("DELETE", f"/api/v1/projects/{project}/conversations/{doomed}")
    assert status == 200
    status, body = studio.api.get(f"/api/v1/projects/{project}/conversations")
    assert {c["id"] for c in body["conversations"]} == {keep}
    status, body = studio.api.get(f"/api/v1/projects/{project}/conversations?archived=1")
    assert {c["id"] for c in body["conversations"]} == {keep, doomed}

    # Restore the archive, then use the new "deleted" key for a real trash.
    status, body = studio.api.patch(
        f"/api/v1/projects/{project}/conversations/{doomed}", {"archived": False})
    assert status == 200

    status, body = studio.api.patch(
        f"/api/v1/projects/{project}/conversations/{doomed}", {"deleted": True})
    assert status == 200 and body["conversation"]["deleted_at"] is not None

    status, body = studio.api.get(f"/api/v1/projects/{project}/conversations?archived=1")
    assert {c["id"] for c in body["conversations"]} == {keep}, "trashed item leaked into archive view"

    status, body = studio.api.get(f"/api/v1/projects/{project}/conversations?deleted=1")
    assert {c["id"] for c in body["conversations"]} == {keep, doomed}

    status, body = studio.api.patch(
        f"/api/v1/projects/{project}/conversations/{doomed}", {"deleted": False})
    assert status == 200 and body["conversation"]["deleted_at"] is None
    status, body = studio.api.get(f"/api/v1/projects/{project}/conversations")
    assert {c["id"] for c in body["conversations"]} == {keep, doomed}
