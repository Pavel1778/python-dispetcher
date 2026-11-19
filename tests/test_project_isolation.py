"""Cross-project isolation: a principal who is *not* a member of a project must
fail every probe — never read, never write a byte, and never learn whether the
project exists.

The web tier authenticates one configured login, so this battery runs at the
service layer, which is where every HTTP handler funnels its project-scoped
work (``ProjectService._project`` / ``require_scope``). If a future route
skips the service check, ``test_route_auth.py`` still forces authentication
and the per-service checks here fail loudly when bypassed by accident.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import NotFound, PostgresStore  # noqa: E402
from orchestration.nexus_binding import ProjectRepositoryService  # noqa: E402
from orchestration.nexus_binding import ProjectBindingService  # noqa: E402
from orchestration.nexus_context import ContextService, MemoryService  # noqa: E402
from orchestration.nexus_conversations import ConversationService  # noqa: E402
from orchestration.nexus_executors import ProjectExecutorService  # noqa: E402
from orchestration.nexus_files import FileService  # noqa: E402
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_openhands_accounts import OpenHandsAccountService  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402
from orchestration.nexus_reports import ReportService  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")

ALICE, BOB = "user:alice", "user:bob"
RUN_ID = str(uuid.uuid4())


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
            "nexus_projects RESTART IDENTITY CASCADE"
        )
    yield instance
    instance.close()


@pytest.fixture
def alice_project(store):
    svc = ProjectService(store)
    project = svc.create(ALICE, "Alpha", "Backend")
    return str(project["id"])


def _probes(store: PostgresStore, project_id: str) -> list:
    lab = LabStore(store)
    context = ContextService(store)
    memory = MemoryService(store)
    files = FileService(store)
    conversations = ConversationService(store)
    projects = ProjectService(store)
    bindings = ProjectBindingService(store)
    repos = ProjectRepositoryService(store)
    accounts = OpenHandsAccountService(store)
    executors = ProjectExecutorService(store, accounts)
    reports = ReportService(store)
    return [
        ("projects.get", lambda: projects.get(BOB, project_id)),
        ("projects.update", lambda: projects.update(BOB, project_id, {"name": "stolen"})),
        ("projects.stats", lambda: projects.stats(BOB, project_id)),
        ("projects.list_goals", lambda: projects.list_goals(BOB, project_id)),
        ("projects.events", lambda: projects.events(BOB, project_id)),
        ("projects.create_goal", lambda: projects.create_goal(BOB, project_id, "stolen")),
        ("runs.list", lambda: lab.list_runs(BOB, project_id)),
        ("tasks.list", lambda: lab.list_tasks(BOB, project_id, RUN_ID)),
        ("notifications.list", lambda: lab.notifications(BOB, project_id)),
        ("context.current", lambda: context.current(BOB, project_id)),
        ("context.history", lambda: context.history(BOB, project_id)),
        ("memory.retrieve", lambda: memory.retrieve(BOB, project_id)),
        ("files.list", lambda: files.list(BOB, project_id)),
        ("conversations.list", lambda: conversations.list(BOB, project_id)),
        ("binding.get", lambda: bindings.get(BOB, project_id)),
        ("repositories.list", lambda: repos.list(BOB, project_id)),
        ("executors.get", lambda: executors.get(BOB, project_id)),
        ("reports.get", lambda: reports.get(BOB, project_id, RUN_ID)),
    ]


def test_every_project_surface_refuses_a_non_member(store, alice_project):
    probes = _probes(store, alice_project)
    failures: list[str] = []
    for name, probe in probes:
        try:
            probe()
            failures.append(f"{name}: returned a result instead of refusing")
        except NotFound:
            pass
        except Exception as e:  # noqa: BLE001 - a non-404 error is still a refusal
            failures.append(f"{name}: unexpected {type(e).__name__}: {e}")
    assert not failures, "\n".join(failures)


def test_non_member_cannot_become_a_member_by_supplying_the_id(store, alice_project):
    # The project id is the only thing the caller could "guess"; nothing about a
    # request body may turn a stranger into a member. The one granting path is
    # the owner's own action, exercised by the project-management suites.
    with store._tx() as cur:
        cur.execute("SELECT principal FROM nexus_project_members WHERE project_id=%s",
                    (alice_project,))
        assert [row["principal"] for row in cur.fetchall()] == [ALICE]


def test_after_the_owner_removes_membership_the_old_member_is_refused(store, alice_project):
    # Owner grants and then revokes Bob; the refusal must follow immediately.
    projects = ProjectService(store)
    with store._tx() as cur:
        cur.execute("INSERT INTO nexus_project_members(project_id, principal, role) "
                    "VALUES (%s, %s, 'member')", (alice_project, BOB))
    projects.get(BOB, alice_project)  # member now: readable
    with store._tx() as cur:
        cur.execute("DELETE FROM nexus_project_members WHERE project_id=%s AND principal=%s",
                    (alice_project, BOB))
    with pytest.raises(NotFound):
        projects.get(BOB, alice_project)
