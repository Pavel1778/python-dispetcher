"""The report of a completed run reaches the chat it was asked for in - once.

Real PostgreSQL, the real scheduler, the real ``GitVerifier`` reading a GitHub
that is a socket server (``tests/fake_github.py``): the commits, files and
checks the report lists are what that server answered, not what a test wrote
into a dictionary. The conversations are scripted (``tests/test_durable_runs``).

Shown here:

* the report appears in the chat by itself when the run completes, and only
  then - not for a run that failed, was cancelled, or is still at work;
* it is written in the transaction that completes the run, so a restart
  between "completed" and "in the chat" still delivers it;
* a delivery that fails is tried again; nothing is ever sent twice, by two
  workers or by one worker twice;
* it says what was asked, what was done, in which repositories, by which
  executor, what was checked independently and what is only the agents' word.
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import NotFound, PostgresStore  # noqa: E402
from orchestration.github_client import GitHubClient  # noqa: E402
from orchestration.nexus_binding import ProjectRepositoryService  # noqa: E402
from orchestration.nexus_conversations import ConversationService  # noqa: E402
from orchestration.nexus_lab import GitVerifier, LabScheduler  # noqa: E402
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402
from orchestration.nexus_reports import ReportService  # noqa: E402
from tests.fake_github import FakeGitHubServer  # noqa: E402
from tests.test_durable_runs import ALICE, DSN, Conversations, reset_schema, sql  # noqa: E402

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")

APP = "Pavel1778/python-dispetcher"
SITE = "Pavel1778/Klyrshift"
BASE = "ba5e" + "0" * 36
TASK = ("Сделай две вещи.\n\n1. В сервисе добавь эндпоинт /health.\n"
        "2. На сайте покажи статус сервиса.\n")


def sha(n: int) -> str:
    """A commit id that is told apart by its beginning, as real ones are."""
    return f"{n:x}".ljust(40, "0")


@pytest.fixture
def env():
    reset_schema()
    store = PostgresStore(DSN)
    projects = ProjectService(store)
    project = str(projects.create(ALICE, "Reports")["id"])
    conversations = ConversationService(store)
    chat = str(conversations.create(ALICE, project, title="Задача", mode="autonomous")["id"])
    goal = str(projects.create_goal(ALICE, project, TASK)[0]["id"])
    with FakeGitHubServer() as gh:
        for name in (APP, SITE):
            store.add_repository(name, default_branch="main", allowed_branches=["main"])
            ProjectRepositoryService(store).link(ALICE, project, name)
            gh.gh.push(name, BASE, "initial")
        github = GitHubClient(gh.base_url, token="server-token", timeout=5.0)
        lab = LabStore(store)
        reports = ReportService(store)
        lab.reports = reports
        yield {"store": store, "projects": projects, "project": project, "goal": goal,
               "chat": chat, "conversations": conversations, "lab": lab, "gh": gh.gh,
               "github": github, "reports": reports}
    store.close()


def start(env, tasks, *, chat=True, policy="durable"):
    run = env["lab"].start_run(ALICE, env["project"], env["goal"], policy=policy,
                               conversation_id=env["chat"] if chat else None)
    for task in tasks:
        env["lab"].add_task(ALICE, env["project"], run["id"], **{"branch": "main", **task})
    return str(run["id"])


def scheduler(env, executor, **options) -> LabScheduler:
    return LabScheduler(env["lab"], executor, GitVerifier(env["github"]),
                        reports=env["reports"], **options)


def messages(env) -> list[dict]:
    return env["conversations"].list_messages(ALICE, env["project"], env["chat"])


def reports_in_chat(env) -> list[dict]:
    return [m for m in messages(env) if (m.get("meta") or {}).get("report")]


def do_task(env, sched, executor, run_id, key, repository, commit, *, files=None,
            text="Тесты: 12 passed."):
    """One task from start to accepted: the agent pushes *commit* and names it."""
    sched.tick(run_id)
    conversation = executor.of(key)[-1]
    env["gh"].push(repository, commit, f"feat: {key}", files or [
        {"filename": f"src/{key}.py", "status": "added", "additions": 20, "deletions": 0}])
    executor.finish(conversation, f"Готово. Коммит {commit}. {text}")
    return conversation


TWO = [{"task_key": "api", "title": "Эндпоинт /health", "repository": APP},
       {"task_key": "site", "title": "Статус на сайте", "repository": SITE,
        "dependencies": ["api"]}]


def complete(env, sched, executor, run_id):
    do_task(env, sched, executor, run_id, "api", APP, sha(0xA1),
            files=[{"filename": "orchestration/health.py", "status": "added",
                    "additions": 31, "deletions": 0},
                   {"filename": "tests/test_health.py", "status": "added",
                    "additions": 18, "deletions": 0}])
    sched.tick(run_id)
    do_task(env, sched, executor, run_id, "site", SITE, sha(0xB2),
            files=[{"filename": "index.html", "status": "modified",
                    "additions": 4, "deletions": 1}], text="Тесты: npm test — 3 passed.")
    return sched.run_until_done(run_id)


# ----------------------------------------------------------------- in the chat
def test_the_report_is_in_the_chat_when_the_run_completes(env):
    run_id = start(env, TWO)
    executor = Conversations()
    sched = scheduler(env, executor)
    assert reports_in_chat(env) == []
    do_task(env, sched, executor, run_id, "api", APP, sha(0xA1))
    sched.tick(run_id)
    assert reports_in_chat(env) == [], "one task of two is not a finished run"
    do_task(env, sched, executor, run_id, "site", SITE, sha(0xB2))
    sched.run_until_done(run_id)

    assert env["lab"].run_row(run_id)["state"] == "completed"
    [message] = reports_in_chat(env)
    assert message["role"] == "assistant"
    assert message["meta"]["run_id"] == run_id and message["meta"]["outcome"] == "success"
    report = env["reports"].get(ALICE, env["project"], run_id)
    assert report["state"] == "delivered" and report["message_id"] == str(message["id"])
    assert report["delivered_at"] is not None and report["body"] == message["content"]
    # Nobody has to look for it: the project is told as well.
    kinds = [n["kind"] for n in env["lab"].notifications(ALICE, env["project"])]
    assert "run_report" in kinds


def test_the_report_says_what_was_asked_done_and_checked(env):
    run_id = start(env, TWO)
    executor = Conversations()
    sched = scheduler(env, executor)
    env["gh"].check_runs[sha(0xA1)] = [
        {"name": "tests", "status": "completed", "conclusion": "success"},
        {"name": "lint", "status": "completed", "conclusion": "success"}]
    complete(env, sched, executor, run_id)
    text = reports_in_chat(env)[0]["content"]

    # What was asked, word for word.
    assert "### Исходное задание" in text
    for line in TASK.strip().splitlines():
        if line:
            assert f"> {line}" in text
    # What was done, task by task, with the commit each was accepted for and
    # who ran it.
    assert "**Эндпоинт /health** — `Pavel1778/python-dispetcher` · `main`" in text
    assert f"коммит `{sha(0xA1)[:10]}`" in text and f"коммит `{sha(0xB2)[:10]}`" in text
    assert "исполнитель: серверный ключ OpenHands" in text
    # Each repository, from GitHub's own answer: commits and files.
    assert "#### `Pavel1778/python-dispetcher` · ветка `main`" in text
    assert "#### `Pavel1778/Klyrshift` · ветка `main`" in text
    assert f"Было `{BASE[:10]}` → стало `{sha(0xA1)[:10]}`: 1 коммит, 2 файла (+49 −0)" in text
    assert "`orchestration/health.py` (added, +31 −0)" in text
    assert "`index.html` (modified, +4 −1)" in text
    assert "feat: api" in text and "feat: site" in text
    # The repository's own checks, where it has any.
    assert f"Проверки репозитория на `{sha(0xA1)[:10]}`: пройдены (2 из 2)" in text
    assert f"Проверок (CI) на `{sha(0xB2)[:10]}` в репозитории нет" in text
    # What the agents said is quoted as theirs, not asserted.
    assert "> Готово. Коммит" in text and "npm test — 3 passed" in text
    assert "со слов агентов" in text
    assert "### Независимая проверка" in text and "Принято 2 из 2" in text
    assert "**Статус:** выполнено, все задачи проверены и подтверждены в репозиториях." in text
    assert "### Ограничения и блокеры" in text

    data = env["reports"].get(ALICE, env["project"], run_id)["summary"]
    assert data["outcome"] == "success" and data["task_count"] == 2
    assert [(r["repository"], r["head_sha"], r["total_commits"]) for r in data["repositories"]] \
        == [(APP, sha(0xA1), 1), (SITE, sha(0xB2), 1)]
    assert [t["commit"] for t in data["tasks"]] == [sha(0xA1), sha(0xB2)]


def test_a_report_never_claims_more_than_was_seen(env):
    """Failed checks, retries and a task accepted without a change are said."""
    run_id = start(env, TWO)
    executor = Conversations()
    sched = scheduler(env, executor)
    # The first attempt of "api" names no commit: reworked, then accepted.
    sched.tick(run_id)
    executor.finish(executor.of("api")[0], "Сделал, но ничего не отправил.")
    sched.tick(run_id)
    sched.deliver_stops()
    conversation = executor.of("api")[-1]
    env["gh"].push(APP, sha(0xA1), "feat: api")
    executor.finish(conversation, f"Отправил. Коммит {sha(0xA1)}.")
    sched.tick(run_id)
    # "site" needed no change: its agent names the head as it stands.
    sched.tick(run_id)
    executor.finish(executor.of("site")[-1], f"Менять нечего. Вершина ветки {BASE}.")
    env["gh"].check_runs[sha(0xA1)] = [
        {"name": "tests", "status": "completed", "conclusion": "failure"}]
    sched.run_until_done(run_id)

    report = env["reports"].get(ALICE, env["project"], run_id)
    assert report["outcome"] == "success_with_notes"
    text = report["body"]
    assert "**Статус:** выполнено и подтверждено в репозиториях, с замечаниями:" in text
    assert "**не пройдены** 1 из 1 (tests)" in text
    assert "NEXUS их не перезапускал" in text
    assert "1 из 2 задач потребовали повторных запусков (всего запусков: 3" in text
    assert "Вершина ветки не изменилась" in text
    assert "Приняты без изменения ветки" in text and "site" in text
    assert reports_in_chat(env)[0]["meta"]["outcome"] == "success_with_notes"


def test_a_run_in_which_nothing_was_pushed_is_not_reported_as_done(env):
    """What the live service did on the day this was deployed. The agent's push
    was refused; it said so, and named the head of the branch as it had found
    it. The rule of acceptance - the agent named the head of the branch - holds
    for that, so the task verified and the run completed. The report must not
    call it done: NEXUS saw no new commit."""
    run_id = start(env, [TWO[0]])
    executor = Conversations()
    sched = scheduler(env, executor)
    sched.tick(run_id)
    refused = ("## Report\n\n**Blocker: the push was refused.** Remote "
               f"`{APP}@main` is still **`{BASE}`** — unchanged.\n\n"
               f"remote: Permission to {APP}.git denied. The requested URL returned error: 403")
    executor.finish(executor.of("api")[-1], refused)
    assert sched.run_until_done(run_id).finished
    # The rule of acceptance itself is as it was.
    assert env["lab"].get_run(ALICE, env["project"], run_id)["state"] == "completed"

    report = env["reports"].get(ALICE, env["project"], run_id)
    assert report["outcome"] == "no_changes"
    assert report["summary"]["changes"] == {"changed": [], "unchanged": [APP], "unknown": []}
    text = report["body"]
    assert "**Статус:** изменений в репозитории нет — результат не подтверждён." in text
    assert "выполнено" not in text and "Блокеров не осталось" not in text
    assert "ни одного нового коммита" in text and "она не доставлена" in text
    assert "Вершина ветки не изменилась" in text
    assert "Новых коммитов нет: подтверждать в репозитории нечего." in text
    assert "Blocker: the push was refused." in text, "the agent's own words are there to read"
    message = reports_in_chat(env)[0]
    assert message["meta"]["outcome"] == "no_changes" and message["content"] == text


def test_a_run_whose_starting_commit_is_not_known_claims_no_change(env):
    """A run that was at work when the starting commit began to be recorded.
    With one task there is nothing to tell from: whether the branch moved is
    not known, and the report says that instead of "done"."""
    run_id = start(env, [TWO[0]])
    executor = Conversations()
    sched = scheduler(env, executor)
    sched.tick(run_id)
    # As the code before left it: no record of the attempt, no starting commit.
    sql(env, "DELETE FROM nexus_task_executions WHERE run_id=%s", run_id)
    sql(env, "UPDATE nexus_run_tasks SET base_sha=NULL WHERE run_id=%s", run_id)
    executor.finish(executor.of("api")[-1], f"The push was refused. Remote is still {BASE}.")
    assert sched.run_until_done(run_id).finished

    report = env["reports"].get(ALICE, env["project"], run_id)
    assert report["outcome"] == "unconfirmed"
    assert report["summary"]["changes"] == {"changed": [], "unchanged": [], "unknown": [APP]}
    text = report["body"]
    assert "**Статус:** завершено, но изменения в репозитории не подтверждены." in text
    assert "выполнено" not in text and "Блокеров не осталось" not in text
    assert "С какого коммита прогон начал, установить не удалось" in text
    assert "Начальный коммит неизвестен" in text
    assert reports_in_chat(env)[0]["meta"]["outcome"] == "unconfirmed"


def test_tasks_accepted_for_different_commits_show_that_the_branch_moved(env):
    """...even when the starting commit is not known: a long run that was half
    done when this was deployed did deliver, and is not told otherwise."""
    tasks = [{"task_key": "api", "title": "Эндпоинт", "repository": APP},
             {"task_key": "more", "title": "Тесты к нему", "repository": APP,
              "dependencies": ["api"]}]
    run_id = start(env, tasks)
    executor = Conversations()
    sched = scheduler(env, executor)
    do_task(env, sched, executor, run_id, "api", APP, sha(0xA1))
    sched.tick(run_id)
    do_task(env, sched, executor, run_id, "more", APP, sha(0xA2))
    sql(env, "UPDATE nexus_task_executions SET base_sha=NULL WHERE run_id=%s", run_id)
    assert sched.run_until_done(run_id).finished

    report = env["reports"].get(ALICE, env["project"], run_id)
    assert report["outcome"] == "success_with_notes"
    assert report["summary"]["changes"] == {"changed": [APP], "unchanged": [], "unknown": []}
    text = report["body"]
    assert "**Статус:** выполнено и подтверждено в репозиториях, с замечаниями:" in text
    assert "Ветка менялась за время прогона: задачи приняты по разным коммитам (2)." in text
    assert f"список изменений в {APP} не построен" in text


def test_a_long_task_is_quoted_by_its_beginning_and_named_by_its_size(env):
    from orchestration.nexus_specs import SpecService

    long_text = "\n".join(f"{n}. Сервис должен сделать шаг номер {n}." for n in range(1, 400))
    goal, spec, _ = SpecService(env["store"]).create_with_goal(
        ALICE, env["project"], long_text, source="chat", conversation_id=env["chat"])
    env["goal"] = str(goal["id"])
    run_id = start(env, [TWO[0]])
    executor = Conversations()
    sched = scheduler(env, executor)
    do_task(env, sched, executor, run_id, "api", APP, sha(0xA1))
    sched.run_until_done(run_id)
    text = reports_in_chat(env)[0]["content"]
    assert "> 1. Сервис должен сделать шаг номер 1." in text
    assert f"Задание длиннее: {len(long_text)} символов" in text
    assert spec["sha256"][:12] in text
    assert "шаг номер 399" not in text, "the whole text stays in the specification"
    assert len(text) < 60_000


@pytest.mark.parametrize("ending", ["completed", "cancelled", "failed"])
def test_a_short_order_is_quoted_without_the_conversation_it_took_along(env, ending):
    """«Делай» goes into the task with the conversation before it
    (``nexus_chat.task_with_context``). Whatever way the run ends, its report
    quotes the message and says that the conversation went with it: the
    conversation is right above the report in the chat, and a report that
    repeated it would carry it into the next short order too."""
    from orchestration.nexus_chat import task_with_context
    from orchestration.nexus_specs import CONTEXT_MARKER, SpecService

    wish = "Нужно, чтобы сервис отвечал на /health, а сайт показывал его статус."
    task, attached = task_with_context("Делай", [
        {"role": "user", "content": wish},
        {"role": "assistant", "content": "Это две задачи: сервис и сайт.",
         "meta": {"mode": "ask"}}])
    assert attached == 2 and wish in task
    goal, _spec, _ = SpecService(env["store"]).create_with_goal(
        ALICE, env["project"], task, source="chat", conversation_id=env["chat"])
    env["goal"] = str(goal["id"])
    run_id = start(env, TWO, policy="bounded" if ending == "failed" else "durable")
    executor = Conversations()
    sched = scheduler(env, executor)
    if ending == "completed":
        complete(env, sched, executor, run_id)
    else:
        do_task(env, sched, executor, run_id, "api", APP, sha(0xA1))
        sched.tick(run_id)
        if ending == "cancelled":
            env["lab"].set_run_state(ALICE, env["project"], run_id, "cancelled")
        else:
            env["lab"].set_run_state_internal(run_id, "failed", last_error="deadline passed")
        sched.run_until_done(run_id)
    env["reports"].deliver_due()
    assert env["lab"].run_row(run_id)["state"] == ending
    chat = reports_in_chat(env)
    assert len(chat) == 1
    body = chat[0]["content"]
    assert "### Исходное задание\n\n> Делай\n" in body
    assert "в задание вошла и предшествующая переписка этого чата" in body
    assert wish not in body and CONTEXT_MARKER not in body


# ------------------------------------------------------------ only when earned
@pytest.mark.parametrize("ending", ["blocked", "paused"])
def test_no_report_for_a_run_that_did_not_end(env, ending):
    """A run that is merely paused or blocked is not over: no report yet."""
    policy = "bounded" if ending == "blocked" else "durable"
    run_id = start(env, TWO, policy=policy)
    executor = Conversations()
    sched = scheduler(env, executor)
    do_task(env, sched, executor, run_id, "api", APP, sha(0xA1))
    sched.tick(run_id)
    if ending == "paused":
        env["lab"].set_run_state(ALICE, env["project"], run_id, "paused")
    else:
        sched.tick(run_id)
        executor.states[executor.of("site")[-1]] = "error"
        for _ in range(8):
            sched.tick(run_id)
            if env["lab"].run_row(run_id)["state"] == "blocked":
                break
            executor.states[executor.of("site")[-1]] = "error"
            env["lab"].set_run_state(ALICE, env["project"], run_id, "running") \
                if env["lab"].run_row(run_id)["state"] == "blocked" else None
    sched.run_until_done(run_id)
    env["reports"].deliver_due()
    assert env["lab"].run_row(run_id)["state"] == ending
    with pytest.raises(NotFound):
        env["reports"].get(ALICE, env["project"], run_id)


@pytest.mark.parametrize("ending", ["cancelled", "failed"])
def test_a_run_that_ended_without_completing_still_reports_honestly(env, ending):
    """The requirement: a cancelled or failed run tells its chat at once, and
    never presents itself as a success. The report is delivered like any other."""
    policy = "bounded" if ending == "failed" else "durable"
    run_id = start(env, TWO, policy=policy)
    executor = Conversations()
    sched = scheduler(env, executor)
    do_task(env, sched, executor, run_id, "api", APP, sha(0xA1))
    sched.tick(run_id)
    if ending == "cancelled":
        env["lab"].set_run_state(ALICE, env["project"], run_id, "cancelled")
    else:
        env["lab"].set_run_state_internal(run_id, "failed", last_error="deadline passed")
    sched.run_until_done(run_id)
    env["reports"].deliver_due()
    assert env["lab"].run_row(run_id)["state"] == ending
    chat = reports_in_chat(env)
    assert len(chat) == 1, "a run that ended without completing reports once"
    body = chat[0]["content"]
    assert body.startswith("## Отчёт автономного прогона")
    assert "выполнено" not in body.splitlines()[1], "an unfinished run is not called done"
    if ending == "cancelled":
        assert "отменён" in body
    else:
        assert "не выполнен" in body and "deadline passed" in body
    report = env["reports"].get(ALICE, env["project"], run_id)
    assert report["outcome"] == ending
    assert report["state"] == "delivered"


def test_a_blocked_run_tells_its_chat_at_once(env):
    """A blocked run needs a person; the chat hears the reason immediately."""
    run_id = start(env, TWO, policy="bounded")
    executor = Conversations()
    sched = scheduler(env, executor)
    do_task(env, sched, executor, run_id, "api", APP, sha(0xA1))
    sched.tick(run_id)
    executor.states[executor.of("site")[-1]] = "error"
    for _ in range(8):
        sched.tick(run_id)
        if env["lab"].run_row(run_id)["state"] == "blocked":
            break
        executor.states[executor.of("site")[-1]] = "error"
        env["lab"].set_run_state(ALICE, env["project"], run_id, "running") \
            if env["lab"].run_row(run_id)["state"] == "blocked" else None
    assert env["lab"].run_row(run_id)["state"] == "blocked"
    notes = [m for m in messages(env) if (m.get("meta") or {}).get("blocked")]
    assert notes, "the chat was not told the run is blocked"
    assert "заблокирован" in notes[-1]["content"].lower()


def test_a_commit_that_left_the_branch_is_done_again_before_any_report(env):
    """Every task verified - but meanwhile the branch of the first was rewound
    past the commit it was accepted for. The run is not reported as done: that
    task goes back to work, and the report comes when its work is in the
    branch again."""
    run_id = start(env, TWO)
    executor = Conversations()
    sched = scheduler(env, executor)
    do_task(env, sched, executor, run_id, "api", APP, sha(0xA1))
    sched.tick(run_id)                          # "api" is accepted at a1; "site" starts
    assert env["lab"].tasks_internal(run_id)[0]["state"] == "completed_verified"
    # Somebody resets the branch to where it was and pushes something else.
    env["gh"].history[APP] = [env["gh"].history[APP][0]]
    env["gh"].push(APP, sha(0xC3), "unrelated")
    env["gh"].push(SITE, sha(0xB2), "feat: site")
    executor.finish(executor.of("site")[-1], f"Готово. Коммит {sha(0xB2)}.")
    for _ in range(3):
        sched.tick(run_id)

    assert env["lab"].run_row(run_id)["state"] == "running" and reports_in_chat(env) == []
    api = env["lab"].tasks_internal(run_id)[0]
    assert api["state"] == "running" and len(executor.of("api")) == 2, "done again"
    assert "is no longer in branch" in str(api["evidence"])

    env["gh"].push(APP, sha(0xD4), "feat: api again")
    executor.finish(executor.of("api")[-1], f"Сделал заново. Коммит {sha(0xD4)}.")
    sched.run_until_done(run_id)
    assert env["lab"].run_row(run_id)["state"] == "completed"
    [message] = reports_in_chat(env)
    assert sha(0xD4)[:10] in message["content"] and sha(0xA1)[:10] not in message["content"]


def test_a_result_is_not_judged_while_github_cannot_be_asked(env):
    """GitHub is down when the agent finishes. The result is neither accepted
    nor rejected: it is looked at again, and nothing is spent meanwhile."""
    run_id = start(env, [TWO[0]])
    executor = Conversations()
    sched = scheduler(env, executor)
    do_task(env, sched, executor, run_id, "api", APP, sha(0xA1))
    env["gh"].force_status = 503
    for _ in range(3):
        sched.tick(run_id)
    task = env["lab"].tasks_internal(run_id)[0]
    assert (task["state"], task["attempt"]) == ("running", 1)
    assert "waiting to be checked" in task["wait_reason"]
    assert len(executor.of("api")) == 1 and reports_in_chat(env) == []

    env["gh"].force_status = 0
    sql(env, "UPDATE nexus_run_tasks SET retry_at = now() - interval '1 second' "
             "WHERE run_id=%s", run_id)
    sched.run_until_done(run_id)
    assert env["lab"].run_row(run_id)["state"] == "completed"
    assert len(reports_in_chat(env)) == 1 and len(executor.of("api")) == 1


def test_a_final_confirmation_that_cannot_be_made_delays_the_report(env):
    """Every task verified, and the last look at the repositories fails. The
    run is not completed on what could not be confirmed; it asks again."""
    class Unreachable(GitVerifier):
        down = 2

        def confirm(self, tasks, *, bases=None):
            if self.down:
                self.down -= 1
                return {"unavailable": "GitHubUnavailable: 503", "reopen": [],
                        "repositories": []}
            return super().confirm(tasks, bases=bases)

    run_id = start(env, [TWO[0]])
    executor = Conversations()
    sched = LabScheduler(env["lab"], executor, Unreachable(env["github"]),
                         reports=env["reports"])
    do_task(env, sched, executor, run_id, "api", APP, sha(0xA1))
    waits = []
    for _ in range(2):
        sched.tick(run_id)
        sched.tick(run_id)                      # nothing happens before the pause is over
        run = env["lab"].run_row(run_id)
        assert run["state"] == "running" and reports_in_chat(env) == []
        assert "итоговая сверка с GitHub не выполнена" in run["wait_reason"]
        waits.append(float(sql(env, "SELECT extract(epoch FROM resume_at - now()) AS s "
                                    "FROM nexus_runs WHERE id=%s", run_id)[0]["s"]))
        sql(env, "UPDATE nexus_runs SET resume_at = now() - interval '1 second' WHERE id=%s",
            run_id)
    assert 29 <= waits[0] <= 34 and 59 <= waits[1] <= 67, waits     # it grows
    sched.run_until_done(run_id)
    assert env["lab"].run_row(run_id)["state"] == "completed"
    assert len(reports_in_chat(env)) == 1


# ------------------------------------------------- once, and after a restart
def test_a_report_written_before_a_restart_is_delivered_after_it(env):
    """The process stops between "the run is completed" and "the message is in
    the chat". The report is a row written with the completion; the next
    process finds it."""
    run_id = start(env, TWO)
    executor = Conversations()
    stopped = ReportService(env["store"])
    stopped.deliver_due = lambda **_: (_ for _ in ()).throw(SystemExit("process stopped"))
    sched = LabScheduler(env["lab"], executor, GitVerifier(env["github"]), reports=stopped)
    with pytest.raises(SystemExit):
        complete(env, sched, executor, run_id)
    assert env["lab"].run_row(run_id)["state"] == "completed"
    assert reports_in_chat(env) == []
    assert env["reports"].get(ALICE, env["project"], run_id)["state"] == "pending"

    other = PostgresStore(DSN)
    try:
        assert ReportService(other).deliver_due() == {
            "delivered": 1, "failed": 0, "undeliverable": 0}
    finally:
        other.close()
    assert len(reports_in_chat(env)) == 1


def test_a_delivery_that_fails_is_tried_again_and_the_wait_grows(env):
    run_id = start(env, [TWO[0]])
    executor = Conversations()
    flaky = ReportService(env["store"])
    posted = flaky._post
    failures = [RuntimeError("the database hiccuped"), RuntimeError("again")]

    def post(cur, conversation_id, report):
        if failures:
            raise failures.pop(0)
        return posted(cur, conversation_id, report)

    flaky._post = post
    sched = LabScheduler(env["lab"], executor, GitVerifier(env["github"]), reports=flaky)
    do_task(env, sched, executor, run_id, "api", APP, sha(0xA1))
    sched.run_until_done(run_id)                # the first try fails; the run is completed
    assert env["lab"].run_row(run_id)["state"] == "completed"
    report = env["reports"].get(ALICE, env["project"], run_id)
    assert (report["state"], report["attempts"]) == ("pending", 1)
    assert "hiccuped" in report["last_error"] and reports_in_chat(env) == []

    assert flaky.deliver_due()["delivered"] == 0, "not before its next try is due"
    waits = []
    for _ in range(2):
        waits.append(float(sql(env, "SELECT extract(epoch FROM next_attempt_at - now()) AS s "
                                    "FROM nexus_run_reports WHERE run_id=%s", run_id)[0]["s"]))
        sql(env, "UPDATE nexus_run_reports SET next_attempt_at = now() WHERE run_id=%s", run_id)
        flaky.deliver_due()
    assert 4 <= waits[0] <= 6 and 9 <= waits[1] <= 11, waits
    report = env["reports"].get(ALICE, env["project"], run_id)
    assert (report["state"], report["attempts"], report["last_error"]) == ("delivered", 3, None)
    assert len(reports_in_chat(env)) == 1


def test_one_run_one_report_however_many_deliver_it(env):
    run_id = start(env, [TWO[0]])
    executor = Conversations()
    sched = scheduler(env, executor)
    do_task(env, sched, executor, run_id, "api", APP, sha(0xA1))
    # Completion writes the report; the delivery is held back for the race below.
    original = env["reports"].deliver_due
    env["reports"].deliver_due = lambda **_: {}
    sched.run_until_done(run_id)
    env["reports"].deliver_due = original
    assert env["reports"].get(ALICE, env["project"], run_id)["state"] == "pending"

    stores = [PostgresStore(DSN) for _ in range(6)]
    barrier = threading.Barrier(len(stores))
    results: list[dict] = []

    def deliver(index: int) -> None:
        service = ReportService(stores[index])
        barrier.wait(10)
        results.append(service.deliver_due())

    threads = [threading.Thread(target=deliver, args=(i,)) for i in range(len(stores))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    for store in stores:
        store.close()
    assert sum(r["delivered"] for r in results) == 1
    assert len(reports_in_chat(env)) == 1
    # And again, later, by anyone: nothing more.
    assert env["reports"].deliver_due() == {"delivered": 0, "failed": 0, "undeliverable": 0}
    sched.tick(run_id)
    assert len(reports_in_chat(env)) == 1
    assert sql(env, "SELECT count(*) AS n FROM nexus_run_reports")[0]["n"] == 1


def test_a_run_started_outside_a_chat_keeps_its_report_with_the_run(env):
    run_id = start(env, [TWO[0]], chat=False)
    executor = Conversations()
    sched = scheduler(env, executor)
    do_task(env, sched, executor, run_id, "api", APP, sha(0xA1))
    sched.run_until_done(run_id)
    report = env["reports"].get(ALICE, env["project"], run_id)
    assert report["state"] == "undeliverable" and "Эндпоинт /health" in report["body"]
    assert reports_in_chat(env) == []
    assert env["reports"].for_run(run_id)["body"] == report["body"]


def test_a_chat_that_was_deleted_meanwhile_does_not_lose_the_report(env):
    run_id = start(env, [TWO[0]])
    executor = Conversations()
    sched = scheduler(env, executor)
    do_task(env, sched, executor, run_id, "api", APP, sha(0xA1))
    sql(env, "DELETE FROM nexus_conversations WHERE id=%s", env["chat"])
    sched.run_until_done(run_id)
    report = env["reports"].get(ALICE, env["project"], run_id)
    assert report["state"] == "undeliverable" and report["conversation_id"] is None
    assert env["reports"].deliver_due() == {"delivered": 0, "failed": 0, "undeliverable": 0}


def test_a_report_is_read_only_by_a_member_of_its_project(env):
    run_id = start(env, [TWO[0]])
    executor = Conversations()
    sched = scheduler(env, executor)
    do_task(env, sched, executor, run_id, "api", APP, sha(0xA1))
    sched.run_until_done(run_id)
    with pytest.raises(NotFound):
        env["reports"].get("user:mallory", env["project"], run_id)
    other = str(env["projects"].create("user:mallory", "Other")["id"])
    with pytest.raises(NotFound):
        env["reports"].get("user:mallory", other, run_id)
