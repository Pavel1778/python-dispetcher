"""Several repositories in one project, each read with its own GitHub account.

Everything goes over HTTP on the real server and PostgreSQL. GitHub is a socket
server on which a token sees only the repositories it was given - what a
private repository of another account looks like: ``acme-api/service`` is
visible to one token, ``acme-web/site`` to another, and the server's own token
sees neither. So "the right account read the right repository" is something
the fake refuses to get wrong, not something a test asserts about a mock.

Shown here:

* two repositories of two GitHub accounts are full members of one project;
* access is checked per repository, with that repository's own identity;
* the planner is told about both and may give a task to either; the agent of
  a task is told which repositories there are and which one is its own;
* tasks on different repositories run side by side, each in a workspace of
  its own, and each result is checked with the account of its repository;
* the report of the run speaks of every repository that was changed;
* a link that is switched off authorizes nothing, and another project sees
  none of this.
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_studio_api import DSN, SERVER_GITHUB_TOKEN, Api, studio_stack  # noqa: E402

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run these tests")

API = "acme-api/service"
WEB = "acme-web/site"
TOKEN_API = "token-of-the-api-account"
TOKEN_WEB = "token-of-the-web-account"
BASE_API = "a0" + "0" * 38
BASE_WEB = "b0" + "0" * 38


@pytest.fixture
def studio(monkeypatch):
    with studio_stack(monkeypatch) as stack:
        yield stack


def until(read, *, timeout=20.0, what="the condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def two_accounts(studio) -> tuple[str, str]:
    """Two private repositories, each visible to one GitHub account only."""
    gh = studio.github
    for name in (API, WEB):
        gh.repos.append({"full_name": name, "default_branch": "main", "private": True,
                         "archived": False, "pushed_at": "2026-01-05T00:00:00Z"})
    gh.push(API, BASE_API, "initial")
    gh.push(WEB, BASE_WEB, "initial")
    gh.token_repos = {TOKEN_API: {API}, TOKEN_WEB: {WEB}, SERVER_GITHUB_TOKEN: set()}
    return studio.account("api team", TOKEN_API), studio.account("web team", TOKEN_WEB)


def project_with_both(studio) -> tuple[str, str, str]:
    api_account, web_account = two_accounts(studio)
    project = studio.project("Shop")
    status, body = studio.api.post(f"/api/v1/projects/{project}/repository",
                                   {"full_name": API, "account_id": api_account})
    assert status == 200, body
    status, body = studio.api.post(
        f"/api/v1/projects/{project}/repository",
        {"full_name": WEB, "account_id": web_account, "additional": True,
         "role_note": "Сайт: показывает то, что отдаёт сервис"})
    assert status == 200, body
    return project, api_account, web_account


def repositories(studio, project) -> dict[str, dict]:
    status, body = studio.api.get(f"/api/v1/projects/{project}/repositories")
    assert status == 200, body
    return {item["full_name"]: item for item in body["repositories"]}


def tokens_that_read(studio, path: str) -> set[str]:
    """Every token GitHub saw on requests for *path*."""
    return {(header or "").split()[-1]
            for seen, header in zip(studio.github.paths, studio.github.auth_headers)
            if seen == path}


# ------------------------------------------------------------------ the links
def test_one_project_holds_repositories_of_two_github_accounts(studio):
    api_account, web_account = two_accounts(studio)
    project = studio.project("Shop")
    status, body = studio.api.post(f"/api/v1/projects/{project}/repository",
                                   {"full_name": API, "account_id": api_account})
    assert status == 200, body

    # The second repository belongs to another account: the project's own
    # identity cannot even see it, so it is not added by mistake...
    status, body = studio.api.post(f"/api/v1/projects/{project}/repository",
                                   {"full_name": WEB, "additional": True})
    assert status == 400 and "cannot see it" in body["error"], body
    status, body = studio.api.post(
        f"/api/v1/projects/{project}/repository",
        {"full_name": WEB, "account_id": api_account, "additional": True})
    assert status == 400 and "cannot see it" in body["error"], body
    assert list(repositories(studio, project)) == [API]

    # ...and is added with the account that owns it.
    status, body = studio.api.post(
        f"/api/v1/projects/{project}/repository",
        {"full_name": WEB, "account_id": web_account, "additional": True,
         "role_note": "Сайт: показывает то, что отдаёт сервис"})
    assert status == 200, body
    added = body["repository"]
    assert added["full_name"] == WEB and added["is_primary"] is False
    assert (added["identity"], added["github_account_id"], added["account_label"]) == (
        "account", web_account, "web team")
    assert added["access"] == {
        "readable": True, "push": True, "admin": True, "private": True,
        "default_branch": "main", "branch": "main", "branch_exists": True, "error": None}
    assert added["checked_at"] is not None

    links = repositories(studio, project)
    assert list(links) == [API, WEB], "the main repository first"
    assert links[API]["is_primary"] is True and links[API]["identity"] == "project_account"
    assert links[WEB]["role_note"].startswith("Сайт")
    assert all(link["usable"] and link["working_branch"] == "main" for link in links.values())
    # The project is still bound to its main repository and its own account.
    binding = studio.api.get(f"/api/v1/projects/{project}/binding")[1]["binding"]
    assert binding["full_name"] == API and binding["github_account_id"] == api_account
    # Nothing a client reads carries a token.
    text = json.dumps(links)
    assert TOKEN_API not in text and TOKEN_WEB not in text


def test_access_is_checked_for_each_repository_with_its_own_identity(studio):
    project, _api_account, web_account = project_with_both(studio)
    studio.github.paths.clear()
    studio.github.auth_headers.clear()
    studio.github.token_permissions[(TOKEN_WEB, WEB)] = {"push": False, "admin": False}

    status, body = studio.api.post(f"/api/v1/projects/{project}/repositories/check")
    assert status == 200, body
    checked = {item["full_name"]: item["access"] for item in body["repositories"]}
    assert checked[API]["readable"] and checked[API]["push"] and checked[API]["private"]
    assert checked[WEB]["readable"] and checked[WEB]["push"] is False, (
        "the web account may read its repository and not push to it - and that is said")
    assert tokens_that_read(studio, f"/repos/{API}") == {TOKEN_API}
    assert tokens_that_read(studio, f"/repos/{WEB}") == {TOKEN_WEB}

    # An account that was revoked is not replaced by another identity: the
    # check says the repository cannot be read, and with what.
    assert studio.api.post(f"/api/github/accounts/{web_account}/revoke")[0] == 200
    status, body = studio.api.post(
        f"/api/v1/projects/{project}/repositories/{WEB}/check")
    assert status == 200, body
    access = body["repository"]["access"]
    assert access["readable"] is False and "cannot be used" in access["error"]
    assert body["repository"]["account_state"] == "revoked"

    # Given back to the project's identity, which cannot see it either.
    status, body = studio.api.post(
        f"/api/v1/projects/{project}/repositories/{WEB}/account", {"account_id": None})
    assert status == 200 and body["repository"]["identity"] == "project_account", body
    assert body["repository"]["access"] is None, "what the old identity found is not kept"
    status, body = studio.api.post(
        f"/api/v1/projects/{project}/repositories/{WEB}/check")
    assert body["repository"]["access"]["readable"] is False
    assert "cannot see it" in body["repository"]["access"]["error"]


def test_the_main_repository_the_switch_and_the_branch(studio):
    project, _api_account, _web_account = project_with_both(studio)
    base = f"/api/v1/projects/{project}/repositories"

    # Switched off for this project: still listed, and authorizes nothing.
    status, body = studio.api.post(f"{base}/{WEB}/disable")
    assert status == 200 and body["repository"]["enabled"] is False, body
    assert body["repository"]["usable"] is False
    status, goal = studio.api.post(f"/api/v1/projects/{project}/goals", {"text": "Сайт"})
    assert status == 201, goal
    status, body = studio.api.post(
        f"/api/v1/projects/{project}/goals/{goal['goal']['id']}/run",
        {"repository": WEB, "branch": "main"})
    assert status == 409 and "switched off" in body["error"], body
    assert studio.api.post(f"{base}/{WEB}/primary")[0] == 400, "nor can it become the main one"

    # The main repository cannot be switched off; another has to take its place.
    status, body = studio.api.post(f"{base}/{API}/disable")
    assert status == 400 and "main repository" in body["error"], body
    assert studio.api.post(f"{base}/{WEB}/enable")[0] == 200
    status, body = studio.api.post(f"{base}/{WEB}/primary")
    assert status == 200 and body["repository"]["is_primary"] is True, body
    links = repositories(studio, project)
    assert [name for name, link in links.items() if link["is_primary"]] == [WEB]
    binding = studio.api.get(f"/api/v1/projects/{project}/binding")[1]["binding"]
    assert binding["full_name"] == WEB, "the project is bound to its main repository"
    assert studio.api.post(f"{base}/{API}/disable")[0] == 200

    # A branch that the repository does not allow is refused; a note is kept.
    status, body = studio.api.post(f"{base}/{API}/update", {"branch": "no-such"})
    assert status == 400 and "allowed branches" in body["error"], body
    status, body = studio.api.post(
        f"{base}/{API}/update", {"role_note": "Сервис: API и база", "label": "backend"})
    assert status == 200, body
    assert (body["repository"]["role_note"], body["repository"]["label"]) == (
        "Сервис: API и база", "backend")
    assert body["repository"]["enabled"] is False, "only what was given is changed"

    # Removing a link: the main one leaves the project bound to nothing.
    status, body = studio.api.post(f"{base}/remove", {"full_name": WEB})
    assert status == 200, body
    assert list(repositories(studio, project)) == [API]
    binding = studio.api.get(f"/api/v1/projects/{project}/binding")[1]["binding"]
    assert binding["full_name"] is None


def test_another_person_cannot_use_these_repositories_or_accounts(studio):
    project, api_account, _web_account = project_with_both(studio)
    # Another project of the same owner has none of these repositories.
    other = studio.project("Elsewhere")
    assert repositories(studio, other) == {}
    status, goal = studio.api.post(f"/api/v1/projects/{other}/goals", {"text": "Чужое"})
    status, body = studio.api.post(
        f"/api/v1/projects/{other}/goals/{goal['goal']['id']}/run",
        {"repository": API, "branch": "main"})
    assert status == 409 and "not linked" in body["error"], body

    # Another principal (the machine token is one) is not a member of the
    # project and owns none of its accounts.
    from orchestration.cloud_store import NotFound
    from orchestration.nexus_binding import ProjectRepositoryService

    service = ProjectRepositoryService(studio.app.store)
    with pytest.raises(NotFound):
        service.list("user:mallory", project)
    with pytest.raises(NotFound):
        service.update("user:mallory", project, API, enabled=False)
    theirs = str(studio.app.projects.create("user:mallory", "Theirs")["id"])
    studio.app.store.add_repository("mallory/app", default_branch="main",
                                    allowed_branches=["main"])
    with pytest.raises(NotFound, match="GitHub account not found"):
        service.link("user:mallory", theirs, "mallory/app", account_id=api_account)
    assert Api(studio.api.base, token=None).get(
        f"/api/v1/projects/{project}/repositories")[0] == 401


# --------------------------------------------------------------------- a run
PLAN = {"tasks": [
    {"id": "api", "title": "Эндпоинт /health в сервисе", "repository": API,
     "acceptance": ["отвечает 200"], "dependencies": []},
    {"id": "site", "title": "Показать статус на сайте", "repository": WEB,
     "acceptance": ["статус виден"], "dependencies": []},
    {"id": "docs", "title": "Описать в README", "repository": "somebody/else",
     "acceptance": ["раздел есть"], "dependencies": ["api"]},
]}


def start_run(studio, project) -> tuple[str, str]:
    conversation = studio.conversation(project, mode="autonomous")
    studio.answers(json.dumps(PLAN))
    status, reply = studio.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages",
        {"text": "Цель: сервис отдаёт статус, сайт его показывает", "mode": "autonomous"})
    assert status == 200, reply
    run = next(m["meta"]["run_id"] for m in reply["messages"] if (m.get("meta") or {}).get("run_id"))
    return conversation, run


def detail(studio, project, run) -> dict:
    status, body = studio.api.get(f"/api/v1/projects/{project}/runs/{run}")
    assert status == 200, body
    return body


def conversation_of(studio, key):
    for cid, conversation in list(studio.cloud.cloud.conversations.items()):
        if (conversation.get("tags") or {}).get("nexustask") == key:
            return cid
    return None


def first_message(studio, cid) -> str:
    return studio.cloud.cloud.start_bodies[cid]["initial_message"]["content"][0]["text"]


def finish(studio, cid, repository, commit, message):
    studio.github.push(repository, commit, message, [
        {"filename": "README.md", "status": "modified", "additions": 3, "deletions": 1}])
    studio.cloud.cloud.add_assistant_message(cid, f"Готово. Коммит {commit}. Тесты: 5 passed.")
    studio.cloud.cloud.conversations[cid]["execution_status"] = "finished"


def test_a_run_works_in_both_repositories_and_reports_on_each(monkeypatch):
    with studio_stack(monkeypatch, lab_inline=True) as studio:
        project, _api_account, _web_account = project_with_both(studio)
        _conversation, run = start_run(studio, project)

        # The planner was told which repositories there are and what for.
        prompt = json.dumps(studio.llm.calls[-1], ensure_ascii=False)
        assert "This project has several repositories" in prompt
        assert f"- {API} (the main repository)" in prompt
        assert f"- {WEB}: Сайт: показывает то, что отдаёт сервис" in prompt

        # Each task is stored with the repository it named; one that named a
        # repository the project does not have works in the main one.
        tasks = {t["task_key"]: t for t in detail(studio, project, run)["tasks"]}
        assert {key: (t["repository"], t["branch"]) for key, t in tasks.items()} == {
            "api": (API, "main"), "site": (WEB, "main"), "docs": (API, "main")}

        # The two independent tasks are at work at the same time, each in a
        # conversation - and a sandbox - of its own.
        api_cid = until(lambda: conversation_of(studio, "api"), what="the api task to start")
        site_cid = until(lambda: conversation_of(studio, "site"), what="the site task to start")
        conversations = studio.cloud.cloud.conversations
        assert conversations[api_cid]["sandbox_id"] != conversations[site_cid]["sandbox_id"]
        bodies = studio.cloud.cloud.start_bodies
        assert bodies[api_cid]["selected_repository"] == API
        assert bodies[site_cid]["selected_repository"] == WEB
        assert conversation_of(studio, "docs") is None, "it waits for 'api' and for its branch"

        # Each agent is told which repositories the project has and which one
        # is its own.
        for cid, mine, other in ((api_cid, API, WEB), (site_cid, WEB, API)):
            message = first_message(studio, cid)
            assert f"Repository: {mine}" in message
            assert "This project has several repositories:" in message
            assert f"- {mine}, branch main" in message and "<- this task" in message
            lines = message.splitlines()
            assert next(x for x in lines if x.startswith(f"- {mine},")).endswith("<- this task")
            assert "<- this task" not in next(x for x in lines if x.startswith(f"- {other},"))
            assert f"This task changes {mine} only." in message
        assert "(the main repository)" in first_message(studio, api_cid)
        assert "Сайт: показывает то, что отдаёт сервис" in first_message(studio, api_cid)

        # Each result is checked with the account of its own repository.
        studio.github.paths.clear()
        studio.github.auth_headers.clear()
        finish(studio, api_cid, API, "a1" + "0" * 38, "feat: health endpoint")
        finish(studio, site_cid, WEB, "b1" + "0" * 38, "feat: status badge")
        docs_cid = until(lambda: conversation_of(studio, "docs"), what="the docs task to start")
        assert tokens_that_read(studio, f"/repos/{API}/commits") == {TOKEN_API}
        assert tokens_that_read(studio, f"/repos/{WEB}/commits") == {TOKEN_WEB}
        assert bodies[docs_cid]["selected_repository"] == API
        finish(studio, docs_cid, API, "a2" + "0" * 38, "docs: health endpoint")

        done = until(lambda: (lambda d: d if d["run"]["state"] != "running" else None)(
            detail(studio, project, run)), what="the run to finish")
        assert done["run"]["state"] == "completed", done["run"]
        report = until(lambda: (lambda d: d["report"] if d["report"]
                                and d["report"]["state"] == "delivered" else None)(
            detail(studio, project, run)), what="the report to be delivered")

        # The report speaks of each repository, from GitHub's own answer.
        text = report["body"]
        assert f"#### `{API}` · ветка `main`" in text and f"#### `{WEB}` · ветка `main`" in text
        assert "Было `a000000000` → стало `a200000000`: 2 коммита" in text
        assert "Было `b000000000` → стало `b100000000`: 1 коммит" in text
        assert "feat: health endpoint" in text and "feat: status badge" in text
        assert "docs: health endpoint" in text
        summary = report["summary"]
        assert [(r["repository"], r["total_commits"], r["tasks"]) for r in summary["repositories"]] \
            == [(API, 2, ["api", "docs"]), (WEB, 1, ["site"])]
        assert summary["outcome"] == "success" and summary["task_count"] == 3


def test_tasks_on_one_repository_take_turns_and_each_repository_has_its_own_sandbox(monkeypatch):
    """The direct transport reuses sandboxes, and every conversation of a
    sandbox works in the same directory. A sandbox is therefore reused only
    for the repository and branch it was started for."""
    with studio_stack(monkeypatch, transport="direct", lab_inline=True) as studio:
        project, _api_account, _web_account = project_with_both(studio)
        _conversation, run = start_run(studio, project)

        def running() -> dict[str, dict]:
            return {t["task_key"]: t for t in detail(studio, project, run)["tasks"]
                    if t["state"] == "running" and t["conversation_id"]}

        at_work = until(lambda: (lambda r: r if {"api", "site"} <= set(r) else None)(running()),
                        what="both independent tasks to be at work")
        assert "docs" not in at_work

        def sandboxes() -> dict[str, str]:
            with studio.app.store._tx() as cur:
                cur.execute("SELECT sandbox_id, workspace FROM cloud_sandboxes "
                            "WHERE status='RUNNING'")
                return {row["workspace"]: row["sandbox_id"] for row in cur.fetchall()}

        first = sandboxes()
        assert set(first) == {f"{API}#main", f"{WEB}#main"}
        assert first[f"{API}#main"] != first[f"{WEB}#main"]

        def route(conversation_id: str) -> str:
            with studio.app.store._tx() as cur:
                cur.execute("SELECT sandbox_id FROM conversation_routes "
                            "WHERE external_conversation_id=%s", (conversation_id,))
                return cur.fetchone()["sandbox_id"]

        assert route(at_work["api"]["conversation_id"]) == first[f"{API}#main"]
        assert route(at_work["site"]["conversation_id"]) == first[f"{WEB}#main"]

        # The api task ends; "docs" works on the same repository and branch,
        # after it, in the sandbox that holds that checkout - not in the site's.
        studio.github.push(API, "a1" + "0" * 38, "feat: health endpoint")
        studio.sandbox.finish(at_work["api"]["conversation_id"],
                              f"Готово. Коммит {'a1' + '0' * 38}.")
        docs = until(lambda: running().get("docs"), what="the docs task to start")
        assert route(docs["conversation_id"]) == first[f"{API}#main"]
        assert sandboxes() == first, "no third sandbox was started"


def test_a_task_whose_repository_is_switched_off_waits_and_goes_on_when_it_is_back(monkeypatch):
    monkeypatch.setenv("NEXUS_RETRY_BASE_SECONDS", "1")
    with studio_stack(monkeypatch, lab_inline=True) as studio:
        project, _api_account, _web_account = project_with_both(studio)
        base = f"/api/v1/projects/{project}/repositories"
        assert studio.api.post(f"{base}/{WEB}/disable")[0] == 200
        # Planned while the site is off: the planner is told of one repository
        # only, so a task that names the site works in the main repository.
        _conversation, run = start_run(studio, project)
        tasks = {t["task_key"]: t for t in detail(studio, project, run)["tasks"]}
        assert tasks["site"]["repository"] == API

        # Switched off while a run has tasks on it: the task is not started,
        # the run does not stop, and the reason is on the task.
        until(lambda: conversation_of(studio, "api"), what="the first task to start")
        assert studio.api.post(f"{base}/{WEB}/enable")[0] == 200
        with studio.app.store._tx() as cur:
            cur.execute("UPDATE nexus_run_tasks SET repository=%s WHERE run_id=%s "
                        "AND task_key='docs'", (WEB, run))
        assert studio.api.post(f"{base}/{WEB}/disable")[0] == 200
        finish(studio, conversation_of(studio, "api"), API, "a1" + "0" * 38, "feat: api")
        waiting = until(lambda: next(
            (t for t in detail(studio, project, run)["tasks"]
             if t["task_key"] == "docs" and t["state"] == "waiting"), None),
            what="the docs task to wait")
        assert "switched off" in waiting["wait_reason"]
        assert detail(studio, project, run)["run"]["state"] == "running"
        assert conversation_of(studio, "docs") is None
        # A repository with a live run's tasks on it cannot be removed.
        status, body = studio.api.post(f"{base}/remove", {"full_name": WEB})
        assert status == 409 and "autonomous run" in body["error"], body

        assert studio.api.post(f"{base}/{WEB}/enable")[0] == 200
        docs = until(lambda: conversation_of(studio, "docs"), timeout=30,
                     what="the docs task to start once its repository is back")
        assert studio.cloud.cloud.start_bodies[docs]["selected_repository"] == WEB
