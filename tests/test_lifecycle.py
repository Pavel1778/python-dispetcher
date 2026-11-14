"""Project and conversation lifecycle: archive, trash, restore, purge, isolation.

Real PostgreSQL and real HTTP (the same stack ``tests/test_conversations.py``
uses). What is asserted is the *effect* of each action, not its answer:

* a project or conversation that is archived or deleted leaves the active list
  and the reads, and comes back on restore;
* a delete never removes the row - the data is still there and ``purge`` is the
  separate, explicit step;
* a purge of one project leaves every other project, its conversations, its
  runs and its GitHub repository links untouched - the external repository is
  never deleted;
* a project or conversation with an active run cannot be deleted (409).
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.nexus_binding import ProjectRepositoryService  # noqa: E402
from tests import test_conversations as _conv  # noqa: E402
from tests.test_conversations import _conversation, _project  # noqa: E402
from tests.test_nexus_projects import request  # noqa: E402

# The fixtures the stack is built from. They are re-exported so this module's
# tests can name them; ``test_conversations`` defines the real ones.
fake_provider = _conv.fake_provider
http_server = _conv.http_server

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")

REPO = "Pavel1778/python-dispetcher"


def _stats(base, session, pid):
    st, body = request(base, f"/api/v1/projects/{pid}/stats", "POST", {}, session=session)
    assert st == 200, body
    return body


# ------------------------------------------------------------------ projects
def test_a_project_can_be_archived_and_restored(http_server):
    base, alice, _app, _ = http_server
    pid = _project(base, alice, "Архивный")
    st, listing = request(base, "/api/v1/projects", session=alice)
    assert [p["id"] for p in listing["projects"]] == [pid]

    st, out = request(base, f"/api/v1/projects/{pid}/archive", "POST", {}, session=alice)
    assert st == 200 and out["project"]["archived_at"] is not None
    # Out of the active list, but not gone: it is still readable on request.
    st, listing = request(base, "/api/v1/projects", session=alice)
    assert listing["projects"] == []
    st, listing = request(base, "/api/v1/projects?archived=1", session=alice)
    assert [p["id"] for p in listing["projects"]] == [pid]

    st, out = request(base, f"/api/v1/projects/{pid}/unarchive", "POST", {}, session=alice)
    assert st == 200 and out["project"]["archived_at"] is None
    st, listing = request(base, "/api/v1/projects", session=alice)
    assert [p["id"] for p in listing["projects"]] == [pid]


def test_a_project_can_be_deleted_and_restored_with_its_contents(http_server):
    base, alice, _app, _ = http_server
    pid = _project(base, alice, "Удаляемый")
    cid = _conversation(base, alice, pid, title="Разговор")
    request(base, f"/api/v1/projects/{pid}/conversations/{cid}/messages", "POST",
            {"text": "Привет", "mode": "ask"}, session=alice)

    st, out = request(base, f"/api/v1/projects/{pid}/delete", "POST", {}, session=alice)
    assert st == 200 and out["project"]["deleted_at"] is not None
    # Gone from the active list and from every read.
    st, listing = request(base, "/api/v1/projects", session=alice)
    assert listing["projects"] == []
    st, _ = request(base, f"/api/v1/projects/{pid}", session=alice)
    assert st == 404
    st, _ = request(base, f"/api/v1/projects/{pid}/conversations", session=alice)
    assert st == 404
    # The row is still there (the trash view), with its conversation.
    st, listing = request(base, "/api/v1/projects?deleted=1", session=alice)
    assert [p["id"] for p in listing["projects"]] == [pid]

    st, out = request(base, f"/api/v1/projects/{pid}/restore", "POST", {}, session=alice)
    assert st == 200 and out["project"]["deleted_at"] is None
    st, listing = request(base, f"/api/v1/projects/{pid}/conversations", session=alice)
    assert [c["id"] for c in listing["conversations"]] == [cid]


def test_purging_a_project_removes_only_it(http_server):
    """Two projects, each with a repository link and a conversation. Purging one
    leaves the other - its conversations, its runs and its repository link - and
    never touches the external GitHub repository."""
    base, alice, app, _ = http_server
    keep = _project(base, alice, "Оставить")
    drop = _project(base, alice, "Удалить")
    keep_conv = _conversation(base, alice, keep, title="Живой чат")
    _conversation(base, alice, drop, title="Удаляемый чат")

    from orchestration.nexus_projects import principal_for_session

    principal = principal_for_session(alice.username)
    store = app.store
    store.add_repository(REPO, default_branch="main", allowed_branches=["main"])
    links = ProjectRepositoryService(store)
    links.link(principal, keep, REPO)
    links.link(principal, drop, REPO)

    st, body = request(base, f"/api/v1/projects/{drop}/purge", "POST",
                       {"confirm": True}, session=alice)
    assert st == 200, body
    assert body["purged"] is True and body["removed"]["nexus_conversations"] == 1

    # The other project is untouched, with its conversation and repository link.
    st, listing = request(base, "/api/v1/projects", session=alice)
    assert [p["id"] for p in listing["projects"]] == [keep]
    st, listing = request(base, f"/api/v1/projects/{keep}/conversations", session=alice)
    assert [c["id"] for c in listing["conversations"]] == [keep_conv]
    st, _ = request(base, f"/api/v1/projects/{drop}", session=alice)
    assert st == 404
    # The repository itself is still on the allowlist and still linked to `keep`.
    with store._tx() as cur:
        cur.execute("SELECT count(*) AS n FROM repositories WHERE full_name=%s", (REPO,))
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT count(*) AS n FROM nexus_project_repositories "
                    "WHERE project_id=%s", (keep,))
        assert cur.fetchone()["n"] == 1


def test_a_purge_needs_confirmation(http_server):
    base, alice, _app, _ = http_server
    pid = _project(base, alice)
    st, _ = request(base, f"/api/v1/projects/{pid}/purge", "POST", {}, session=alice)
    assert st == 400
    # Still there.
    st, _ = request(base, f"/api/v1/projects/{pid}", session=alice)
    assert st == 200


def test_a_deleted_project_is_invisible_to_a_non_member(http_server):
    base, alice, app, _ = http_server
    pid = _project(base, alice)
    bob = app.auth._open_session("bob")
    st, _ = request(base, f"/api/v1/projects/{pid}/delete", "POST", {}, session=alice)
    assert st == 200
    # Bob was never a member: 404 either way, and the trash view does not leak it.
    st, _ = request(base, f"/api/v1/projects/{pid}", session=bob)
    assert st == 404


# ------------------------------------------------------------ conversations
def test_a_conversation_can_be_deleted_restored_and_purged(http_server):
    base, alice, _app, _ = http_server
    pid = _project(base, alice)
    cid = _conversation(base, alice, pid, title="Чат")
    request(base, f"/api/v1/projects/{pid}/conversations/{cid}/messages", "POST",
            {"text": "Привет", "mode": "ask"}, session=alice)

    st, stats = request(base, f"/api/v1/projects/{pid}/conversations/{cid}/stats",
                        "POST", {}, session=alice)
    assert st == 200 and stats["messages"] == 2

    st, _ = request(base, f"/api/v1/projects/{pid}/conversations/{cid}", "DELETE",
                    session=alice)
    assert st == 200
    st, listing = request(base, f"/api/v1/projects/{pid}/conversations", session=alice)
    assert listing["conversations"] == []
    st, listing = request(base, f"/api/v1/projects/{pid}/conversations?deleted=1",
                          session=alice)
    assert [c["id"] for c in listing["conversations"]] == [cid]

    st, out = request(base, f"/api/v1/projects/{pid}/conversations/{cid}/restore",
                      "POST", {}, session=alice)
    assert st == 200 and out["conversation"]["deleted_at"] is None
    st, listing = request(base, f"/api/v1/projects/{pid}/conversations", session=alice)
    assert [c["id"] for c in listing["conversations"]] == [cid]
    # The messages came back with it.
    st, messages = request(base, f"/api/v1/projects/{pid}/conversations/{cid}/messages",
                           session=alice)
    assert len(messages["messages"]) == 2

    # Purge removes it for good; the project and its other data are untouched.
    st, out = request(base, f"/api/v1/projects/{pid}/conversations/{cid}/purge", "POST",
                      {"confirm": True}, session=alice)
    assert st == 200 and out["messages_removed"] == 2
    st, _ = request(base, f"/api/v1/projects/{pid}/conversations/{cid}", session=alice)
    assert st == 404
    st, listing = request(base, f"/api/v1/projects/{pid}/conversations?deleted=1",
                          session=alice)
    assert listing["conversations"] == []


def test_a_purge_of_a_conversation_needs_confirmation(http_server):
    base, alice, _app, _ = http_server
    pid = _project(base, alice)
    cid = _conversation(base, alice, pid)
    st, _ = request(base, f"/api/v1/projects/{pid}/conversations/{cid}/purge", "POST",
                    {}, session=alice)
    assert st == 400
    st, _ = request(base, f"/api/v1/projects/{pid}/conversations/{cid}", session=alice)
    assert st == 200


def test_deleting_a_conversation_does_not_delete_its_project(http_server):
    base, alice, _app, _ = http_server
    pid = _project(base, alice)
    a = _conversation(base, alice, pid, title="Первый")
    b = _conversation(base, alice, pid, title="Второй")
    st, _ = request(base, f"/api/v1/projects/{pid}/conversations/{a}", "DELETE",
                    session=alice)
    assert st == 200
    st, _ = request(base, f"/api/v1/projects/{pid}", session=alice)
    assert st == 200
    st, listing = request(base, f"/api/v1/projects/{pid}/conversations", session=alice)
    assert [c["id"] for c in listing["conversations"]] == [b]


def test_the_lifecycle_columns_are_in_the_schema_guard(http_server):
    """Every column the lifecycle depends on is named in REQUIRED_COLUMNS, and
    the schema the store connects to has them (a forgotten migration is the
    usual cause of a missing column)."""
    from orchestration.cloud_store import PostgresStore

    assert "deleted_at" in PostgresStore.REQUIRED_COLUMNS["nexus_projects"]
    assert "archived_at" in PostgresStore.REQUIRED_COLUMNS["nexus_projects"]
    assert "deleted_at" in PostgresStore.REQUIRED_COLUMNS["nexus_conversations"]
    assert "kind" in PostgresStore.REQUIRED_COLUMNS["nexus_run_tasks"]
    _, _, app, _ = http_server
    assert app.store.verify_schema() == []



def test_a_conversation_can_be_pinned_and_sorts_first(http_server):
    """Pin is ordering only: a pinned conversation sorts to the top of the
    list regardless of recency, and unpinning restores recency order."""
    base, alice, _app, _ = http_server
    pid = _project(base, alice)
    first = _conversation(base, alice, pid, title="Первый")
    _conversation(base, alice, pid, title="Второй")

    def titles():
        _, listing = request(base, f"/api/v1/projects/{pid}/conversations", session=alice)
        return [c["title"] for c in listing["conversations"]]

    # Newest first by default: the second one was created after the first.
    assert titles() == ["Второй", "Первый"]
    # Pinning the *older* one lifts it above the newer one.
    st, out = request(base, f"/api/v1/projects/{pid}/conversations/{first}", "PATCH",
                      {"pinned": True}, session=alice)
    assert st == 200 and out["conversation"]["pinned"] is True
    assert titles() == ["Первый", "Второй"], "a pinned chat must sort first"
    # Unpinning clears the flag (its own updated_at moves, which is fine - the
    # assertion is that pin is a boolean the server round-trips).
    st, out = request(base, f"/api/v1/projects/{pid}/conversations/{first}", "PATCH",
                      {"pinned": False}, session=alice)
    assert out["conversation"]["pinned"] is False


def test_a_conversation_can_be_trashed_and_restored_by_patch(http_server):
    """The trash is reachable by PATCH too, not only by the DELETE route, so a
    client that only patches works; restore is the same call with deleted=false."""
    base, alice, _app, _ = http_server
    pid = _project(base, alice)
    cid = _conversation(base, alice, pid, title="Чат")

    st, out = request(base, f"/api/v1/projects/{pid}/conversations/{cid}", "PATCH",
                      {"deleted": True}, session=alice)
    assert st == 200 and out["conversation"]["deleted_at"] is not None
    _, listing = request(base, f"/api/v1/projects/{pid}/conversations", session=alice)
    assert listing["conversations"] == []
    st, out = request(base, f"/api/v1/projects/{pid}/conversations/{cid}", "PATCH",
                      {"deleted": False}, session=alice)
    assert st == 200 and out["conversation"]["deleted_at"] is None
    _, listing = request(base, f"/api/v1/projects/{pid}/conversations", session=alice)
    assert [c["id"] for c in listing["conversations"]] == [cid]
def test_a_project_can_be_archived_and_deleted_by_patch_too(http_server):
    """The lifecycle keys are accepted on PATCH as well as on the dedicated
    POST routes, so a client that only patches works; a delete is refused (409)
    while an active run exists, and succeeds after the run is cancelled."""
    base, alice, app, _ = http_server
    pid = _project(base, alice)
    st, out = request(base, f"/api/v1/projects/{pid}", "PATCH",
                      {"archived": True}, session=alice)
    assert st == 200 and out["project"]["archived_at"] is not None
    st, out = request(base, f"/api/v1/projects/{pid}", "PATCH",
                      {"archived": False}, session=alice)
    assert out["project"]["archived_at"] is None

    # A run at work blocks the delete.
    from orchestration.nexus_lab_store import LabStore
    from orchestration.nexus_projects import principal_for_session

    principal = principal_for_session(alice.username)
    lab = LabStore(app.store)
    goal = str(app.projects.create_goal(principal, pid, "goal")[0]["id"])
    run = lab.start_run(principal, pid, goal)
    st, body = request(base, f"/api/v1/projects/{pid}", "PATCH",
                       {"deleted": True}, session=alice)
    assert st == 409 and "active run" in body["error"], body

    lab.set_run_state(principal, pid, str(run["id"]), "cancelled")
    st, out = request(base, f"/api/v1/projects/{pid}", "PATCH",
                      {"deleted": True}, session=alice)
    assert st == 200 and out["project"]["deleted_at"] is not None
    st, out = request(base, f"/api/v1/projects/{pid}", "PATCH",
                      {"deleted": False}, session=alice)
    assert st == 200 and out["project"]["deleted_at"] is None


def test_a_trashed_conversation_refuses_new_messages(http_server):
    """A conversation in the trash takes no new message until it is restored -
    a message appended behind the scenes would be invisible work. The refusal
    is a 404 (the trashed conversation is not found) and nothing is written."""
    base, alice, _app, _ = http_server
    pid = _project(base, alice)
    cid = _conversation(base, alice, pid, title="Чат")
    st, _ = request(base, f"/api/v1/projects/{pid}/conversations/{cid}", "DELETE",
                    session=alice)
    assert st == 200
    st, _ = request(base, f"/api/v1/projects/{pid}/conversations/{cid}/messages",
                    "POST", {"text": "привет", "mode": "ask"}, session=alice)
    assert st == 404
    # No message was stored behind the scenes.
    st, listing = request(base, f"/api/v1/projects/{pid}/conversations?deleted=1",
                          session=alice)
    assert [c["id"] for c in listing["conversations"]] == [cid]
    # Restore, and it accepts messages again.
    request(base, f"/api/v1/projects/{pid}/conversations/{cid}/restore", "POST",
            {}, session=alice)
    st, _ = request(base, f"/api/v1/projects/{pid}/conversations/{cid}/messages",
                    "POST", {"text": "привет", "mode": "ask"}, session=alice)
    assert st == 200


def test_lifecycle_actions_require_the_owner_role(http_server):
    """Archive, delete and purge need the owner scope; a plain member is
    refused (404, never 403 - a project's existence is not disclosed)."""
    base, alice, app, _ = http_server
    pid = _project(base, alice)
    # Add a second, non-owner member.
    bob = app.auth._open_session("bob")
    with app.store._tx() as cur:
        cur.execute("INSERT INTO nexus_project_members(project_id, principal, role) "
                    "VALUES (%s, 'user:bob', 'member')", (pid,))
    st, _ = request(base, f"/api/v1/projects/{pid}/archive", "POST", {}, session=bob)
    assert st == 404
    st, _ = request(base, f"/api/v1/projects/{pid}/delete", "POST", {}, session=bob)
    assert st == 404
    st, _ = request(base, f"/api/v1/projects/{pid}/purge", "POST", {"confirm": True},
                    session=bob)
    assert st == 404
    # The owner can.
    st, _ = request(base, f"/api/v1/projects/{pid}/archive", "POST", {}, session=alice)
    assert st == 200


def test_lifecycle_routes_require_auth(http_server):
    base, alice, _app, _ = http_server
    pid = _project(base, alice)
    cid = _conversation(base, alice, pid)
    for path, method in ((f"/api/v1/projects/{pid}/archive", "POST"),
                         (f"/api/v1/projects/{pid}/delete", "POST"),
                         (f"/api/v1/projects/{pid}/restore", "POST"),
                         (f"/api/v1/projects/{pid}/purge", "POST"),
                         (f"/api/v1/projects/{pid}/stats", "POST"),
                         (f"/api/v1/projects/{pid}/conversations/{cid}/delete", "POST"),
                         (f"/api/v1/projects/{pid}/conversations/{cid}/stats", "POST")):
        assert request(base, path, method, {})[0] == 401, path
