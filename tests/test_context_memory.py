"""Project context, unified memory and repository analysis.

Real PostgreSQL for the stores; a real local HTTP GitHub server for the
analyzer. Nothing is mocked at the client boundary.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import NotFound, PostgresStore  # noqa: E402
from orchestration.github_client import GitHubClient  # noqa: E402
from orchestration.nexus_analysis import analyze_repository  # noqa: E402
from orchestration.nexus_context import (  # noqa: E402
    ContextService,
    MemoryService,
    analyze_tree,
)
from orchestration.nexus_projects import ProjectService  # noqa: E402
from tests.fake_github import FakeGitHubServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def _reset() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


@pytest.fixture
def store():
    _reset()
    instance = PostgresStore(DSN)
    yield instance
    instance.close()


@pytest.fixture
def gh_server():
    with FakeGitHubServer() as server:
        yield server


# ------------------------------------------------------------------ analyze_tree
def test_analyze_tree_reports_observed_facts():
    tree = [
        {"path": "README.md", "type": "blob"},
        {"path": "src/app.py", "type": "blob"},
        {"path": "src/util.py", "type": "blob"},
        {"path": "tests/test_app.py", "type": "blob"},
        {"path": ".github/workflows/ci.yml", "type": "blob"},
        {"path": "requirements.txt", "type": "blob"},
    ]
    summary = analyze_tree(tree, {"README.md": "# Hi\n"})
    assert summary["languages"]["Python"] == 3
    assert "src" in summary["top_level"]
    assert summary["has_ci"] is True
    assert summary["readme_present"] is True
    assert summary["build_commands"] == ["pip install -r requirements.txt"]
    assert summary["test_files"] == ["tests/test_app.py"]


def test_analyze_tree_is_empty_without_a_tree():
    summary = analyze_tree([], {})
    assert summary["languages"] == {} and summary["build_commands"] == []
    assert summary["readme_present"] is False


# ------------------------------------------------------------------ analysis live
def test_analyze_repository_against_the_fake_server(gh_server):
    client = GitHubClient(gh_server.base_url, token="t", timeout=5.0)
    summary = analyze_repository(client, "Pavel1778/python-dispetcher")
    assert summary["repository"] == "Pavel1778/python-dispetcher"
    assert summary["branch"] == "main"
    assert summary["revision"] == "abc123"
    assert summary["languages"].get("Python") == 2
    assert summary["readme_present"] is True
    assert summary["build_commands"] == ["pip install -r requirements.txt"]
    assert gh_server.gh.writes == []  # analysis never writes


# -------------------------------------------------------------------- context
def test_context_refresh_is_idempotent_per_revision(store):
    svc = ProjectService(store)
    project = str(svc.create("user:alice", "P")["id"])
    ctx = ContextService(store)
    row, created = ctx.refresh("user:alice", project, repository="a/b",
                               revision="r1", summary={"languages": {"Python": 1}})
    assert created is True
    again, created2 = ctx.refresh("user:alice", project, repository="a/b",
                                  revision="r1", summary={"languages": {}})
    assert created2 is False and again["id"] == row["id"]
    assert ctx.current("user:alice", project)["revision"] == "r1"


def test_new_revision_supersedes_the_old_snapshot(store):
    svc = ProjectService(store)
    project = str(svc.create("user:alice", "P")["id"])
    ctx = ContextService(store)
    ctx.refresh("user:alice", project, repository="a/b", revision="r1", summary={"n": 1})
    ctx.refresh("user:alice", project, repository="a/b", revision="r2", summary={"n": 2})
    current = ctx.current("user:alice", project)
    assert current["revision"] == "r2"
    states = {h["revision"]: h["state"] for h in ctx.history("user:alice", project)}
    assert states == {"r1": "superseded", "r2": "current"}


def test_context_is_project_scoped(store):
    svc = ProjectService(store)
    a = str(svc.create("user:alice", "A")["id"])
    b = str(svc.create("user:bob", "B")["id"])
    ctx = ContextService(store)
    ctx.refresh("user:alice", a, repository="a/b", revision="r", summary={})
    with pytest.raises(NotFound):
        ctx.current("user:bob", a)
    assert ctx.current("user:bob", b) is None


# --------------------------------------------------------------------- memory
def test_memory_deduplicates_and_keeps_the_stronger_provenance(store):
    svc = ProjectService(store)
    project = str(svc.create("user:alice", "P")["id"])
    mem = MemoryService(store)
    first = mem.remember("user:alice", kind="fact", content="Uses PostgreSQL",
                         provenance="agent_reported", project_id=project)
    # A weaker repeat does not overwrite the stronger one.
    same = mem.remember("user:alice", kind="fact", content="uses postgresql",
                        provenance="agent_reported", project_id=project)
    assert same["id"] == first["id"]
    stronger = mem.remember("user:alice", kind="fact", content="Uses PostgreSQL",
                            provenance="test_verified", project_id=project)
    assert stronger["id"] == first["id"] and stronger["provenance"] == "test_verified"


def test_memory_verified_only_filters_agent_claims(store):
    svc = ProjectService(store)
    project = str(svc.create("user:alice", "P")["id"])
    mem = MemoryService(store)
    mem.remember("user:alice", kind="fact", content="agent says done",
                 provenance="agent_reported", project_id=project)
    mem.remember("user:alice", kind="fact", content="tests pass",
                 provenance="test_verified", project_id=project)
    verified = mem.retrieve("user:alice", project, verified_only=True)
    assert [m["content"] for m in verified] == ["tests pass"]


def test_memory_is_project_scoped_and_global_is_shared(store):
    svc = ProjectService(store)
    a = str(svc.create("user:alice", "A")["id"])
    b = str(svc.create("user:bob", "B")["id"])
    mem = MemoryService(store)
    mem.remember("user:alice", kind="fact", content="only in A",
                 provenance="repository_observed", project_id=a)
    mem.remember("user:alice", kind="decision", content="global rule",
                 provenance="repository_observed", project_id=None)
    alice = [m["content"] for m in mem.retrieve("user:alice", a)]
    assert "only in A" in alice and "global rule" in alice
    # Bob sees the global memory but not Alice's project memory.
    bob = [m["content"] for m in mem.retrieve("user:bob", b)]
    assert "only in A" not in bob and "global rule" in bob


def test_memory_supersede_points_the_old_row_at_the_new_one(store):
    svc = ProjectService(store)
    project = str(svc.create("user:alice", "P")["id"])
    mem = MemoryService(store)
    old = mem.remember("user:alice", kind="fact", content="uses SQLite",
                       provenance="repository_observed", project_id=project)
    new = mem.supersede("user:alice", project, str(old["id"]), kind="fact",
                        content="uses PostgreSQL", provenance="test_verified")
    live = [m["content"] for m in mem.retrieve("user:alice", project)]
    assert live == ["uses PostgreSQL"]
    with store._tx() as cur:
        cur.execute("SELECT superseded_by FROM nexus_memory WHERE id=%s", (old["id"],))
        assert str(cur.fetchone()["superseded_by"]) == str(new["id"])


def test_memory_rejects_unknown_kind_and_provenance(store):
    svc = ProjectService(store)
    project = str(svc.create("user:alice", "P")["id"])
    mem = MemoryService(store)
    with pytest.raises(ValueError):
        mem.remember("user:alice", kind="nope", content="x", provenance="fact", project_id=project)
    with pytest.raises(ValueError):
        mem.remember("user:alice", kind="fact", content="x", provenance="nope", project_id=project)
