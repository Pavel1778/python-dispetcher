"""The lab's independent verifier, against a real (fake) GitHub server.

``GitVerifier`` is what stands between "the agent said it is done" and a task
being recorded as verified, so every verdict it can give is pinned here: what
earns ``completed_verified``, and - more important - everything that does not.

The GitHub side is ``tests/fake_github.py`` over a socket; the part that picks a
project's GitHub identity runs on real PostgreSQL.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import NotFound, PostgresStore  # noqa: E402
from orchestration.github_client import GitHubClient  # noqa: E402
from orchestration.nexus_github_accounts import GitHubAccountService, SecretsError  # noqa: E402
from orchestration.nexus_lab import (  # noqa: E402
    GitVerifier,
    TaskReport,
    _named_shas,
    project_github,
)
from orchestration.nexus_projects import ProjectService  # noqa: E402
from tests.fake_github import FakeGitHub, FakeGitHubServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
needs_db = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")

REPO = "Pavel1778/python-dispetcher"
HEAD = "3f2a1b9c0d4e5f60718293a4b5c6d7e8f9012345"
OTHER = "aaaaaaaabbbbbbbbccccccccdddddddd00000000"
SERVER_TOKEN = "server-token-value"
ACCOUNT_TOKEN = "account-token-value"


@pytest.fixture
def github():
    fake = FakeGitHub()
    fake.commits = [{"sha": HEAD, "commit": {"message": "work", "author": {"name": "agent"}}}]
    with FakeGitHubServer(fake) as server:
        yield fake, GitHubClient(base_url=server.base_url, token=SERVER_TOKEN, timeout=5.0), server


def _task(**over):
    return {"task_key": "a", "title": "A", "repository": REPO, "branch": "main",
            "project_id": "00000000-0000-4000-8000-000000000000", **over}


def _finished(text):
    return TaskReport(state="finished", final_text=text, conversation_id="conv-1")


# ---------------------------------------------------------------- what verifies
@pytest.mark.parametrize("report", [
    f"Done. Commit {HEAD}.",                                   # the full SHA
    f"Done. Commit {HEAD[:7]}.",                               # git's short form
    f"pushed {HEAD[:12]} to main, tests: 14 passed",
    f"Touched deadbeef.txt and cafe1234.py, then committed {HEAD[:10]}.",   # not the first token
])
def test_a_report_that_names_the_branch_head_is_verified(github, report):
    fake, client, _ = github
    verdict = GitVerifier(client).verify(_task(), _finished(report))
    assert verdict.verdict == "completed_verified", verdict.reason
    assert verdict.reason == "remote branch is at the reported commit"
    assert verdict.evidence["remote_sha"] == HEAD
    assert HEAD.startswith(verdict.evidence["named_sha"])
    assert verdict.evidence["branch"] == "main"
    # The head was read from GitHub, for this repository and this branch.
    assert f"/repos/{REPO}/commits" in fake.paths


# --------------------------------------------------- what does not, and why not
@pytest.mark.parametrize("report, reason", [
    (f"Done. Commit {OTHER}.", "reported commit is not the branch head"),
    (f"Done. Commit {OTHER[:9]}.", "reported commit is not the branch head"),
    # One character off at the end of an otherwise right abbreviation.
    (f"Done. Commit {HEAD[:9]}f.", "reported commit is not the branch head"),
    ("All done, everything works, trust me.", "no verifiable commit reported by the agent"),
    # Too short to be an abbreviation: six characters is not a commit.
    (f"Done. Commit {HEAD[:6]}.", "no verifiable commit reported by the agent"),
    # Digits alone are a size or an id, never taken for a commit.
    ("Changed 1234567 lines in 20260101 files.", "no verifiable commit reported by the agent"),
    ("", "no verifiable commit reported by the agent"),
])
def test_a_finished_conversation_alone_is_never_verified(github, report, reason):
    _, client, _ = github
    verdict = GitVerifier(client).verify(_task(), _finished(report))
    assert (verdict.verdict, verdict.reason) == ("completed_unverified", reason)
    assert verdict.evidence["remote_sha"] == HEAD


def test_the_head_is_what_github_says_now_not_what_the_agent_says(github):
    """The same report stops verifying the moment the branch is somewhere else."""
    fake, client, _ = github
    report = _finished(f"Commit {HEAD}")
    assert GitVerifier(client).verify(_task(), report).verdict == "completed_verified"
    fake.commits = [{"sha": OTHER, "commit": {"message": "someone else"}}]
    moved = GitVerifier(client).verify(_task(), report)
    assert (moved.verdict, moved.evidence["remote_sha"]) == ("completed_unverified", OTHER)


def test_work_with_no_repository_cannot_be_verified(github):
    fake, client, _ = github
    verdict = GitVerifier(client).verify(_task(repository="", branch=""),
                                         _finished(f"Commit {HEAD}"))
    assert verdict.verdict == "completed_unverified"
    assert verdict.reason == "no repository bound; cannot verify remote state"
    assert fake.paths == [], "GitHub was asked about a task that has no repository"


def test_a_branch_with_no_commit_is_not_verified(github):
    fake, client, _ = github
    fake.commits = []
    verdict = GitVerifier(client).verify(_task(), _finished(f"Commit {HEAD}"))
    assert (verdict.verdict, verdict.reason) == (
        "completed_unverified", "branch has no readable commit")


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_a_branch_that_cannot_be_read_blocks_the_task(github, status):
    """GitHub being down, or the token not seeing the repository, is not a
    verdict on the work: the task is blocked and can be retried."""
    fake, client, _ = github
    fake.force_status = status
    verdict = GitVerifier(client).verify(_task(), _finished(f"Commit {HEAD}"))
    assert verdict.verdict == "blocked"
    assert verdict.reason.startswith("github unavailable: ")
    assert SERVER_TOKEN not in str(verdict.evidence) + verdict.reason


@pytest.mark.parametrize("state, verdict, reason", [
    ("missing", "failed", "conversation vanished"),
    ("error", "failed", "conversation error"),
    ("stuck", "failed", "conversation stuck"),
    ("paused", "blocked", "conversation did not finish"),
    ("unknown", "blocked", "conversation did not finish"),
])
def test_a_conversation_that_did_not_finish_is_judged_without_reading_github(
        github, state, verdict, reason):
    fake, client, _ = github
    # Even a report that names the head does not count if the agent never finished.
    report = TaskReport(state=state, final_text=f"Commit {HEAD}", conversation_id="conv-1")
    result = GitVerifier(client).verify(_task(), report)
    assert (result.verdict, result.reason) == (verdict, reason)
    assert fake.paths == []


def test_the_evidence_keeps_the_report_but_bounded(github):
    _, client, _ = github
    verdict = GitVerifier(client).verify(_task(), _finished("x" * 5000 + f" {HEAD}"))
    assert len(verdict.evidence["final_text"]) == 2000


def test_named_shas_are_distinct_and_in_order():
    text = f"first {HEAD[:8]}, again {HEAD[:8]}, then {OTHER}, size 123456789, ZZZ deadbee"
    assert _named_shas(text) == [HEAD[:8], OTHER, "deadbee"]
    assert _named_shas("") == [] and _named_shas(None) == []


# -------------------------------------------------- whose eyes the branch is read with
@pytest.mark.parametrize("error", [
    NotFound("account not found"), ValueError("account is revoked"),
    SecretsError("NEXUS_SECRET_KEY is not set; refusing to store a credential"),
])
def test_an_identity_that_cannot_be_used_blocks_and_is_not_replaced(github, error):
    """The project's account failing must not quietly become the server token."""
    fake, client, _ = github

    def client_for(task):
        raise error

    verdict = GitVerifier(client, client_for=client_for).verify(
        _task(), _finished(f"Commit {HEAD}"))
    assert verdict.verdict == "blocked"
    assert verdict.reason == f"github unavailable: {type(error).__name__}"
    assert fake.paths == [], "the branch was read with another identity"


@pytest.fixture
def bound(github, monkeypatch):
    """A project in PostgreSQL, and a GitHub account service on the fake."""
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())
    monkeypatch.setenv("NEXUS_SECRET_KEY", "verifier-tests-secret-key")
    fake, server_client, server = github
    store = PostgresStore(DSN)
    accounts = GitHubAccountService(store, client_factory=lambda token: GitHubClient(
        base_url=server.base_url, token=token, timeout=5.0))
    project = str(ProjectService(store).create("user:alice", "Lab")["id"])
    yield {"store": store, "accounts": accounts, "project": project, "fake": fake,
           "server_client": server_client}
    store.close()


def _commit_reads(fake):
    """The Authorization header of every read of the branch head."""
    return [header for path, header in zip(fake.paths, fake.auth_headers)
            if path == f"/repos/{REPO}/commits"]


@needs_db
def test_a_project_without_an_account_is_read_with_the_server_token(bound):
    client_for = project_github(bound["store"], bound["server_client"],
                                accounts=bound["accounts"])
    verdict = GitVerifier(bound["server_client"], client_for=client_for).verify(
        _task(project_id=bound["project"]), _finished(f"Commit {HEAD}"))
    assert verdict.verdict == "completed_verified"
    assert _commit_reads(bound["fake"]) == [f"Bearer {SERVER_TOKEN}"]


@needs_db
def test_a_project_with_an_account_is_read_with_that_account(bound):
    account = bound["accounts"].add("user:alice", label="work", token=ACCOUNT_TOKEN)
    bound["accounts"].bind_project("user:alice", bound["project"], account["id"])
    client_for = project_github(bound["store"], bound["server_client"],
                                accounts=bound["accounts"])
    verdict = GitVerifier(bound["server_client"], client_for=client_for).verify(
        _task(project_id=bound["project"]), _finished(f"Commit {HEAD}"))
    assert verdict.verdict == "completed_verified"
    assert _commit_reads(bound["fake"]) == [f"Bearer {ACCOUNT_TOKEN}"]


@needs_db
def test_a_revoked_account_blocks_instead_of_falling_back(bound):
    account = bound["accounts"].add("user:alice", label="work", token=ACCOUNT_TOKEN)
    bound["accounts"].bind_project("user:alice", bound["project"], account["id"])
    bound["accounts"].revoke("user:alice", account["id"])
    client_for = project_github(bound["store"], bound["server_client"],
                                accounts=bound["accounts"])
    verdict = GitVerifier(bound["server_client"], client_for=client_for).verify(
        _task(project_id=bound["project"]), _finished(f"Commit {HEAD}"))
    assert verdict.verdict == "blocked"
    assert _commit_reads(bound["fake"]) == [], "a revoked account's project was read anyway"
    assert ACCOUNT_TOKEN not in str(verdict.evidence)


@needs_db
def test_a_missing_encryption_key_blocks_instead_of_falling_back(bound, monkeypatch):
    account = bound["accounts"].add("user:alice", label="work", token=ACCOUNT_TOKEN)
    bound["accounts"].bind_project("user:alice", bound["project"], account["id"])
    monkeypatch.delenv("NEXUS_SECRET_KEY")
    client_for = project_github(bound["store"], bound["server_client"],
                                accounts=bound["accounts"])
    verdict = GitVerifier(bound["server_client"], client_for=client_for).verify(
        _task(project_id=bound["project"]), _finished(f"Commit {HEAD}"))
    assert (verdict.verdict, verdict.reason) == ("blocked", "github unavailable: SecretsError")
    assert _commit_reads(bound["fake"]) == []
