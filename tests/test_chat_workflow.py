"""The chat-first workflow: Auto runs, Manual confirms, Chat never runs.

The requirement this covers: NEXUS is an intelligent development environment,
not a system that asks permission for everything. Auto keeps deciding and
running understandable work by itself; Chat never starts an agent; Manual shows
a plan and runs only on an explicit launch command.

The first half is the pure logic (no database): the launch classifier and the
dialogue state machine. The second half drives the real server over real HTTP and
PostgreSQL and asserts the observable behaviour: an order in Manual proposes a
plan and starts nothing, an explicit "Запускай" starts exactly one run, a replan
requires a new confirmation, and Auto keeps running.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.nexus_intent import (  # noqa: E402
    classify,
    effective_mode,
)
from orchestration.nexus_workflow import (  # noqa: E402
    AWAITING_APPROVAL,
    DISCOVERY,
    EXECUTING,
    approval_matches,
    can_transition,
    check_transition,
    classify_launch,
    plan_hash_of,
    render_plan,
)
from tests.test_studio_api import DSN, REPO, studio_stack  # noqa: E402

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run workflow tests")


@pytest.fixture
def studio(monkeypatch):
    with studio_stack(monkeypatch) as stack:
        yield stack


PLAN = {"tasks": [
    {"id": "audit", "title": "Изучить код", "description": "Прочитать модуль.",
     "acceptance": ["сводка"], "dependencies": []},
    {"id": "change", "title": "Внести изменение", "description": "Изменить.",
     "acceptance": ["тесты"], "dependencies": ["audit"]},
]}
PLAN_2 = {"tasks": [
    {"id": "other", "title": "Другой план", "description": "Иначе.",
     "acceptance": ["ок"], "dependencies": []},
]}


# ------------------------------------------------------- the launch classifier
@pytest.mark.parametrize("text, is_command", [
    ("Запускай", True),
    ("Начинай", True),
    ("Приступай к выполнению", True),
    ("Выполняй", True),
    ("Запусти задачу", True),
    ("Start", True),
    ("Execute", True),
    ("Proceed with implementation", True),
    ("Run it", True),
    ("Пожалуйста, запускай", True),
    # Not commands: a question, a prohibition, a condition.
    ("когда запустишь?", False),
    ("Запустить?", False),
    ("Ты уже запустил?", False),
    ("Не запускай пока", False),
    ("Не запускай", False),
    ("Don't start yet", False),
    ("Запускай только после проверки", False),
    ("Запусти, когда тесты пройдут", False),
    ("Давай обсудим реализацию", False),
])
def test_the_launch_table(text, is_command):
    assert classify_launch(text).is_command is is_command


def test_a_prohibition_is_named_not_a_silent_non_match():
    launch = classify_launch("Не запускай пока")
    assert launch.prohibition and not launch.command


def test_a_conditional_launch_is_named():
    launch = classify_launch("Запускай только после проверки")
    assert launch.conditional and not launch.command


# ---------------------------------------------------------- the state machine
def test_executing_is_reachable_only_from_awaiting_approval():
    """The invariant of the whole requirement: a discussion cannot become a run."""
    for state in (DISCOVERY, "CLARIFICATION", "PLANNING"):
        assert not can_transition(state, EXECUTING)
    assert can_transition(AWAITING_APPROVAL, EXECUTING)
    with pytest.raises(ValueError):
        check_transition(DISCOVERY, EXECUTING)


def test_a_terminal_state_leaves_room_for_new_work():
    for state in ("COMPLETED", "CANCELLED", "FAILED"):
        assert can_transition(state, DISCOVERY)
        assert can_transition(state, AWAITING_APPROVAL)


def test_the_plan_version_hash_changes_with_the_plan():
    assert plan_hash_of(PLAN) == plan_hash_of(json.loads(json.dumps(PLAN)))
    changed = json.loads(json.dumps(PLAN))
    changed["tasks"][0]["title"] = "Другое"
    assert plan_hash_of(changed) != plan_hash_of(PLAN)


def test_an_approval_matches_only_its_own_version():
    digest = plan_hash_of(PLAN)
    assert approval_matches(digest, digest)
    assert not approval_matches(digest, None)
    assert not approval_matches(digest, plan_hash_of(PLAN_2))
    assert not approval_matches(None, digest)


def test_manual_mode_turns_an_order_into_a_plan_but_keeps_a_question():
    assert effective_mode("manual", classify("Исправь ошибку", mode="manual")) == "plan"
    assert effective_mode("manual", classify("Что готово?", mode="manual")) == "ask"


def test_auto_mode_is_unchanged_by_manual():
    """Auto must keep running understandable work - that is the whole point."""
    intent = classify("Исправь эту ошибку", mode="auto")
    assert intent.starts_work
    assert effective_mode("auto", intent) == "autonomous"


def test_render_plan_says_nothing_has_started():
    text = render_plan(PLAN["tasks"], repository=REPO, branch="main")
    assert "Предлагаемый план" in text and REPO in text
    assert "Запускай" in text and "не запущен" in text
    assert "1. **Изучить код**" in text and "после: audit" in text


# ------------------------------------------------------ the observable behaviour
def _count_runs(stack, project) -> int:
    return len(stack.api.get(f"/api/v1/projects/{project}/runs?include_hidden=1")[1]["runs"])


def _conversation(stack, project, *, mode="manual") -> str:
    status, body = stack.api.post(f"/api/v1/projects/{project}/conversations",
                                  {"title": "", "mode": mode})
    assert status == 201, body
    return body["conversation"]["id"]


def _send(stack, project, conversation, text, mode=None):
    body = {"text": text}
    if mode:
        body["mode"] = mode
    return stack.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages", body)


def _connect(stack, project):
    assert stack.api.post(f"/api/v1/projects/{project}/repository",
                          {"full_name": REPO})[0] == 200


def test_a_manual_order_proposes_a_plan_and_starts_nothing(studio):
    project = studio.project()
    _connect(studio, project)
    conversation = _conversation(studio, project)
    studio.answers(json.dumps(PLAN))

    status, body = _send(studio, project, conversation, "Исправь найденные ошибки")
    assert status == 200, body
    last = body["messages"][-1]
    assert last["meta"].get("awaiting_approval") is True
    assert "Предлагаемый план" in last["content"]
    # Nothing was started: no run, no OpenHands conversation.
    assert _count_runs(studio, project) == 0
    assert studio.cloud.cloud.conversations == {}
    # The conversation is waiting for a confirmation.
    wf = studio.api.get(
        f"/api/v1/projects/{project}/conversations/{conversation}")[1]["conversation"]
    assert wf["workflow_state"] == AWAITING_APPROVAL
    assert wf["plan_hash"]


def test_a_manual_question_is_answered_and_starts_nothing(studio):
    project = studio.project()
    _connect(studio, project)
    conversation = _conversation(studio, project)
    studio.answers("Диспетчер ведёт задачи по прогонам.")

    status, body = _send(studio, project, conversation, "Что уже готово?")
    assert status == 200, body
    assert body["messages"][-1]["meta"]["mode"] in ("ask", "analysis")
    assert _count_runs(studio, project) == 0
    assert studio.llm.calls, "the question was answered by the model"


def test_an_explicit_launch_after_a_plan_starts_exactly_one_run(studio):
    project = studio.project()
    _connect(studio, project)
    conversation = _conversation(studio, project)
    studio.answers(json.dumps(PLAN))

    status, body = _send(studio, project, conversation, "Реализуй функцию")
    assert status == 200, body
    assert _count_runs(studio, project) == 0

    # "Запускай" starts the run the plan was proposed for.
    status, body = _send(studio, project, conversation, "Запускай")
    assert status == 200, body
    run_ids = [m["meta"].get("run_id") for m in body["messages"] if m["meta"].get("run_id")]
    assert run_ids, body
    assert _count_runs(studio, project) == 1
    wf = studio.api.get(
        f"/api/v1/projects/{project}/conversations/{conversation}")[1]["conversation"]
    assert wf["workflow_state"] == EXECUTING

    # A repeated launch does not start a second run.
    status, body = _send(studio, project, conversation, "Запускай")
    assert status == 200, body
    assert _count_runs(studio, project) == 1, "a duplicate run was created"


def test_a_prohibition_does_not_start_the_approved_plan(studio):
    project = studio.project()
    _connect(studio, project)
    conversation = _conversation(studio, project)
    studio.answers(json.dumps(PLAN))
    assert _send(studio, project, conversation, "Реализуй функцию")[0] == 200

    status, body = _send(studio, project, conversation, "Не запускай пока")
    assert status == 200, body
    assert _count_runs(studio, project) == 0
    assert "подтверждении" in body["messages"][-1]["content"]


def test_a_conditional_launch_does_not_start(studio):
    project = studio.project()
    _connect(studio, project)
    conversation = _conversation(studio, project)
    studio.answers(json.dumps(PLAN))
    assert _send(studio, project, conversation, "Реализуй функцию")[0] == 200

    status, body = _send(studio, project, conversation, "Запускай только после проверки")
    assert status == 200, body
    assert _count_runs(studio, project) == 0


def test_a_replan_requires_a_new_confirmation(studio):
    """A plan that changed must not be started by an approval for the old one."""
    project = studio.project()
    _connect(studio, project)
    conversation = _conversation(studio, project)
    studio.answers(json.dumps(PLAN), json.dumps(PLAN_2))

    assert _send(studio, project, conversation, "Реализуй функцию")[0] == 200
    first_hash = studio.api.get(
        f"/api/v1/projects/{project}/conversations/{conversation}")[1]["conversation"]["plan_hash"]

    # A requirement change produces a new plan with a new hash and no approval.
    status, body = _send(studio, project, conversation, "Теперь сделай иначе")
    assert status == 200, body
    assert _count_runs(studio, project) == 0
    wf = studio.api.get(
        f"/api/v1/projects/{project}/conversations/{conversation}")[1]["conversation"]
    assert wf["plan_hash"] != first_hash
    assert wf["plan_approved_hash"] is None

    # The launch starts the *new* plan.
    status, body = _send(studio, project, conversation, "Запускай")
    assert status == 200, body
    assert _count_runs(studio, project) == 1


def test_a_launch_without_a_plan_asks_for_a_task(studio):
    project = studio.project()
    _connect(studio, project)
    conversation = _conversation(studio, project)
    status, body = _send(studio, project, conversation, "Запускай")
    assert status == 200, body
    assert _count_runs(studio, project) == 0
    assert "нечего запускать" in body["messages"][-1]["content"]


def test_auto_mode_still_runs_without_a_confirmation(studio):
    """Auto is the autonomous mode: it must not require an approval."""
    project = studio.project()
    _connect(studio, project)
    conversation = _conversation(studio, project, mode="auto")
    studio.answers(json.dumps(PLAN))

    status, body = _send(studio, project, conversation, "Исправь найденные ошибки")
    assert status == 200, body
    assert _count_runs(studio, project) == 1


def test_a_question_in_auto_mode_starts_nothing(studio):
    project = studio.project()
    _connect(studio, project)
    conversation = _conversation(studio, project, mode="auto")
    studio.answers("Ответ.")

    status, body = _send(studio, project, conversation, "Почему прогон ломается?")
    assert status == 200, body
    assert _count_runs(studio, project) == 0


def test_switching_a_conversation_to_manual_changes_the_behaviour(studio):
    """The same order is a run in Auto and a plan in Manual."""
    project = studio.project()
    _connect(studio, project)
    conversation = _conversation(studio, project, mode="auto")

    # Switch the conversation to Manual.
    status, body = studio.api.patch(
        f"/api/v1/projects/{project}/conversations/{conversation}",
        {"mode": "manual"})
    assert status == 200 and body["conversation"]["mode"] == "manual"

    # Now an order proposes a plan and starts nothing.
    studio.answers(json.dumps(PLAN))
    status, body = _send(studio, project, conversation, "Исправь найденные ошибки")
    assert status == 200, body
    assert body["messages"][-1]["meta"].get("awaiting_approval") is True
    assert _count_runs(studio, project) == 0


# ------------------------------------------------------ run lifecycle / archive
def _runs(stack, project, *, archived=False):
    path = f"/api/v1/projects/{project}/runs" + ("?archived=1" if archived else "")
    return stack.api.get(path)[1]["runs"]


def test_a_completed_run_leaves_the_active_list_but_stays_in_the_archive(studio):
    """Finished work must not clutter the interface; its history must stay."""
    project = studio.project()
    _connect(studio, project)
    conversation = _conversation(studio, project)
    studio.answers(json.dumps(PLAN))
    assert _send(studio, project, conversation, "Реализуй функцию")[0] == 200
    assert _send(studio, project, conversation, "Запускай")[0] == 200
    run_id = _runs(studio, project)[0]["id"]

    # Complete it directly (the loop is off in tests): every task verified.
    with studio.app.store._tx() as cur:
        cur.execute("UPDATE nexus_run_tasks SET state='completed_verified' "
                    "WHERE run_id=%s", (run_id,))
    assert studio.app.lab.set_run_state_internal(run_id, "completed") is not None

    assert _runs(studio, project) == [], "a completed run is still in the active list"
    archived = _runs(studio, project, archived=True)
    assert [r["id"] for r in archived] == [run_id]
    assert archived[0]["state"] == "completed" and archived[0]["hidden"] is True
    # Readable by id, and its tasks remain.
    detail = studio.api.get(f"/api/v1/projects/{project}/runs/{run_id}")
    assert detail[0] == 200 and detail[1]["tasks"]


def test_a_blocked_or_failed_run_stays_visible_for_attention(studio):
    """Only a run that finished cleanly hides itself; a problem stays in sight."""
    project = studio.project()
    _connect(studio, project)
    for state, err in (("blocked", "no usable account"), ("failed", "boom")):
        conversation = _conversation(studio, project)
        studio.answers(json.dumps(PLAN))
        assert _send(studio, project, conversation, "Реализуй функцию")[0] == 200
        assert _send(studio, project, conversation, "Запускай")[0] == 200
        run_id = _runs(studio, project)[0]["id"]
        row = studio.app.lab.set_run_state_internal(run_id, state, last_error=err)
        assert row is not None and row["state"] == state
        visible = {r["id"]: r for r in _runs(studio, project)}
        assert run_id in visible, f"a {state} run disappeared from the active list"
        assert visible[run_id]["hidden"] is False


def test_the_archive_route_does_not_bypass_membership(studio):
    """Project isolation holds for the archive route too: an unknown project is 404."""
    project = studio.project()
    _connect(studio, project)
    assert studio.api.get(f"/api/v1/projects/{project}/runs?archived=1")[0] == 200
    assert studio.api.get(
        "/api/v1/projects/00000000-0000-4000-8000-000000000000/runs?archived=1")[0] == 404


# ------------------------------------------------------------- correctness
def test_two_concurrent_launches_start_exactly_one_run(studio):
    """A double submit of "Запускай" must start one run, not two."""
    import threading

    project = studio.project()
    _connect(studio, project)
    conversation = _conversation(studio, project)
    studio.answers(json.dumps(PLAN))
    assert _send(studio, project, conversation, "Реализуй функцию")[0] == 200

    results: list[int] = []
    lock = threading.Lock()

    def launch():
        status, _body = _send(studio, project, conversation, "Запускай")
        with lock:
            results.append(status)

    threads = [threading.Thread(target=launch) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(status == 200 for status in results), results
    assert _count_runs(studio, project) == 1, "concurrent launches created extra runs"


def test_an_approval_cannot_start_a_plan_it_did_not_see(studio):
    """The confirmation is bound to a version: an approval for a stale hash is refused."""
    project = studio.project()
    _connect(studio, project)
    conversation = _conversation(studio, project)
    studio.answers(json.dumps(PLAN), json.dumps(PLAN_2))
    assert _send(studio, project, conversation, "Реализуй функцию")[0] == 200
    stale = studio.api.get(
        f"/api/v1/projects/{project}/conversations/{conversation}")[1]["conversation"]["plan_hash"]
    # A replan replaces the plan on offer; the old hash is no longer valid.
    assert _send(studio, project, conversation, "Теперь сделай иначе")[0] == 200
    with pytest.raises(ValueError):
        studio.app.conversations.record_approval(
            "user:owner", project, conversation, plan_hash=stale)
    # The plan on offer changed, so the stale hash no longer matches.
    current = studio.api.get(
        f"/api/v1/projects/{project}/conversations/{conversation}")[1]["conversation"]
    assert current["plan_hash"] != stale
