"""GitHub discovery tests: real HTTP against a real fake GitHub server.

The client under test makes real requests over a socket, so auth headers,
pagination and error mapping are exercised for real rather than mocked.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.github_client import (  # noqa: E402
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubUnavailable,
)
from tests.fake_github import FakeGitHubServer  # noqa: E402

TOKEN = "ghp_test_token_value_never_logged"


@pytest.fixture
def gh_server():
    with FakeGitHubServer() as server:
        yield server


def client_for(server, **kw) -> GitHubClient:
    return GitHubClient(server.base_url, token=TOKEN, timeout=5.0, **kw)


# ------------------------------------------------------------------ discovery
def test_lists_repositories_from_the_api(gh_server):
    repos = client_for(gh_server).list_repositories()
    names = [r.full_name for r in repos]
    assert "Pavel1778/python-dispetcher" in names
    assert "Pavel1778/Klyrshift" in names


def test_archived_repositories_are_skipped_by_default(gh_server):
    repos = client_for(gh_server).list_repositories()
    assert "Pavel1778/old-archive" not in [r.full_name for r in repos]

    with_archived = client_for(gh_server).list_repositories(include_archived=True)
    assert "Pavel1778/old-archive" in [r.full_name for r in with_archived]


def test_default_branch_is_carried_through(gh_server):
    repos = {r.full_name: r for r in client_for(gh_server).list_repositories()}
    assert repos["Pavel1778/python-dispetcher"].default_branch == "main"
    assert repos["Pavel1778/python-dispetcher"].private is True


def test_pagination_walks_every_page(gh_server):
    # 3 repos, 2 per page -> two real requests, every repo seen.
    repos = client_for(gh_server, page_size=2).list_repositories()
    assert len(gh_server.gh.paths) == 2
    assert gh_server.gh.paths == ["/user/repos", "/user/repos"]
    assert "Pavel1778/python-dispetcher" in [r.full_name for r in repos]


def test_a_full_page_is_followed_by_another_request(gh_server):
    """The loop must not stop early when a page is exactly full."""
    repos = client_for(gh_server, page_size=3).list_repositories(include_archived=True)
    assert len(repos) == 3
    # First page was full (3), so a second request is made and comes back empty.
    assert len(gh_server.gh.paths) == 2


def test_token_is_sent_as_bearer(gh_server):
    client_for(gh_server).list_repositories()
    assert gh_server.gh.auth_headers
    assert all(h == f"Bearer {TOKEN}" for h in gh_server.gh.auth_headers)


def test_token_is_never_exposed_by_repr(gh_server):
    """A stray repr() in a log line must not print the token."""
    c = client_for(gh_server)
    assert c.configured is True
    assert TOKEN not in repr(c)
    assert "configured=True" in repr(c)


# --------------------------------------------------------------------- errors
def test_missing_token_raises_auth_error(gh_server):
    c = GitHubClient(gh_server.base_url, token="", timeout=5.0)
    assert c.configured is False
    with pytest.raises(GitHubAuthError):
        c.list_repositories()


def test_401_is_an_auth_error(gh_server):
    gh_server.gh.force_status = 401
    with pytest.raises(GitHubAuthError):
        client_for(gh_server).list_repositories()


def test_403_with_exhausted_quota_is_transient_not_auth(gh_server):
    """A rate-limited token is valid; telling the operator to rotate it is wrong."""
    gh_server.gh.force_status = 403
    gh_server.gh.rate_remaining = "0"
    with pytest.raises(GitHubUnavailable):
        client_for(gh_server).list_repositories()


def test_403_without_quota_exhaustion_is_an_auth_error(gh_server):
    gh_server.gh.force_status = 403
    gh_server.gh.rate_remaining = "42"
    with pytest.raises(GitHubAuthError):
        client_for(gh_server).list_repositories()


def test_500_is_transient(gh_server):
    gh_server.gh.force_status = 500
    with pytest.raises(GitHubUnavailable):
        client_for(gh_server).list_repositories()


def test_404_is_a_plain_error(gh_server):
    gh_server.gh.force_status = 404
    with pytest.raises(GitHubError):
        client_for(gh_server).list_repositories()


def test_unreachable_host_is_transient():
    c = GitHubClient("http://127.0.0.1:1", token=TOKEN, timeout=2.0)
    with pytest.raises(GitHubUnavailable):
        c.list_repositories()


# -------------------------------------------------------------------- mapping
def test_payload_without_full_name_is_skipped():
    from orchestration.github_client import _repo_from_payload

    assert _repo_from_payload({"name": "no-owner"}) is None
    assert _repo_from_payload("not-a-dict") is None
    repo = _repo_from_payload({"full_name": "a/b"})
    assert repo is not None and repo.default_branch == "main"


# ------------------------------------------------------------- detail reads
def test_repository_detail_is_read_only(gh_server):
    repo = client_for(gh_server).get_repository("Pavel1778/python-dispetcher")
    assert repo["full_name"] == "Pavel1778/python-dispetcher"
    assert gh_server.gh.writes == []


def test_branches_commits_issues_and_pulls(gh_server):
    c = client_for(gh_server)
    assert [b["name"] for b in c.list_branches("Pavel1778/python-dispetcher")] == ["main", "dev"]
    commits = c.list_commits("Pavel1778/python-dispetcher")
    assert commits[0]["sha"] == "abc123" and commits[0]["message"] == "Initial commit"
    # The issues endpoint mixes PRs in; the client filters them out.
    assert [i["number"] for i in c.list_issues("Pavel1778/python-dispetcher")] == [1]
    assert c.list_pulls("Pavel1778/python-dispetcher")[0]["head"] == "feature"
    assert gh_server.gh.writes == []


def test_detail_rejects_a_bad_full_name(gh_server):
    with pytest.raises(ValueError):
        client_for(gh_server).get_repository("no-slash")
    with pytest.raises(ValueError):
        client_for(gh_server).list_issues("a/b", state="bogus")


# ------------------------------------------------------------ guarded writes
def test_writer_create_repository_sends_the_body(gh_server):
    from orchestration.github_client import GitHubWriter

    writer = GitHubWriter(client_for(gh_server))
    result = writer.create_repository(name="nexus-project", private=True,
                                      description="lab")
    assert result["full_name"] == "Pavel1778/nexus-project"
    method, path, body = gh_server.gh.writes[-1]
    assert method == "POST" and path == "/user/repos"
    assert body == {"name": "nexus-project", "private": True,
                    "description": "lab", "auto_init": True}


def test_writer_rejects_a_slash_in_the_name(gh_server):
    from orchestration.github_client import GitHubWriter

    writer = GitHubWriter(client_for(gh_server))
    with pytest.raises(ValueError):
        writer.create_repository(name="owner/name", private=True)
    assert gh_server.gh.writes == []


def test_public_visibility_needs_acknowledgement(gh_server):
    from orchestration.github_client import GitHubWriter, VisibilityError

    writer = GitHubWriter(client_for(gh_server))
    with pytest.raises(VisibilityError):
        writer.set_repository_visibility(full_name="Pavel1778/python-dispetcher",
                                         private=False)
    assert gh_server.gh.writes == []  # nothing was sent


def test_public_visibility_with_acknowledgement_writes(gh_server):
    from orchestration.github_client import GitHubWriter

    writer = GitHubWriter(client_for(gh_server))
    result = writer.set_repository_visibility(
        full_name="Pavel1778/python-dispetcher", private=False, acknowledge_public=True)
    assert result["private"] is False
    assert gh_server.gh.writes[-1] == ("PATCH", "/repos/Pavel1778/python-dispetcher",
                                       {"private": False})


def test_making_a_repo_private_needs_no_acknowledgement(gh_server):
    from orchestration.github_client import GitHubWriter

    writer = GitHubWriter(client_for(gh_server))
    writer.set_repository_visibility(full_name="Pavel1778/Klyrshift", private=True)
    assert gh_server.gh.writes[-1] == ("PATCH", "/repos/Pavel1778/Klyrshift", {"private": True})


def test_writer_repr_never_exposes_the_token(gh_server):
    from orchestration.github_client import GitHubWriter

    writer = GitHubWriter(client_for(gh_server))
    assert TOKEN not in repr(writer)
    assert "configured=True" in repr(writer)
