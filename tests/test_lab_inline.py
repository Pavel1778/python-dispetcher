"""An autonomous run from the chat box to "completed", with nothing mocked.

The lab used to need a second process (``python -m orchestration.lab_worker``)
that a one-service deployment never started, so a run begun from the studio sat
at "running" for ever with every task pending. ``NEXUS_LAB_INLINE`` lets the web
process advance runs itself. These tests drive that whole path over HTTP: the
real server on real PostgreSQL, the fake OpenHands Cloud, the fake GitHub and a
fake LLM - the same three socket servers ``test_studio_api.py`` uses.
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_studio_api import (  # noqa: E402
    ACCOUNT_TOKEN,
    DSN,
    REPO,
    SERVER_GITHUB_TOKEN,
    studio_stack,
)

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run lab tests")

HEAD = "9a8b7c6d5e4f30211203f4e5d6c7b8a901234567"
PLAN = {"tasks": [
    {"id": "audit", "title": "Изучить код", "description": "Прочитать модуль.",
     "acceptance": ["сводка"], "dependencies": []},
    {"id": "change", "title": "Внести изменение", "description": "Реализовать.",
     "acceptance": ["тесты"], "dependencies": ["audit"]},
]}


def _until(read, *, timeout=20.0, what="the condition"):
    """Poll *read* until it returns something truthy; fail with *what* if not."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _start_run(studio, *, account=False):
    """Connect the repository and send one autonomous message. Returns ids."""
    project = studio.project()
    body = {"full_name": REPO}
    if account:
        body["account_id"] = studio.account()
    assert studio.api.post(f"/api/v1/projects/{project}/repository", body)[0] == 200
    studio.github.commits = [{"sha": HEAD, "commit": {"message": "work"}}]
    conversation = studio.conversation(project, mode="autonomous")
    studio.answers(json.dumps(PLAN))
    status, reply = studio.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages",
        {"text": "Цель: покрыть модуль тестами", "mode": "autonomous"})
    assert status == 200, reply
    return project, reply["messages"][2]["meta"]["run_id"]


def _detail(studio, project, run):
    status, detail = studio.api.get(f"/api/v1/projects/{project}/runs/{run}")
    assert status == 200, detail
    return detail


def _task_conversation(studio, key):
    """The Cloud conversation the lab started for one task, once it exists."""
    for cid, conversation in list(studio.cloud.cloud.conversations.items()):
        if (conversation.get("tags") or {}).get("nexustask") == key:
            return cid
    return None


def _finish(studio, cid, text):
    studio.cloud.cloud.add_assistant_message(cid, text)
    studio.cloud.cloud.conversations[cid]["execution_status"] = "finished"


def _starts(studio):
    return [call for call in studio.cloud.cloud.calls
            if call == ("POST", "/api/v1/app-conversations")]


def _head_reads(studio):
    """The Authorization header of every read of the branch head."""
    return {header for path, header in zip(studio.github.paths, studio.github.auth_headers)
            if path == f"/repos/{REPO}/commits"}


def test_the_server_alone_takes_a_run_from_the_chat_to_completed(monkeypatch):
    with studio_stack(monkeypatch, lab_inline=True) as studio:
        assert studio.api.get("/api/studio")[1]["lab"] == {"inline": True, "cleanup_hours": 24.0}
        project, run = _start_run(studio, account=True)

        # The first task is started on the Cloud, for the connected repository
        # and branch; the second waits for it.
        first = _until(lambda: _task_conversation(studio, "audit"), what="the first task to start")
        started = studio.cloud.cloud.start_bodies[first]
        assert (started["selected_repository"], started["selected_branch"]) == (REPO, "main")
        time.sleep(0.6)                                   # a few passes of the loop
        assert _task_conversation(studio, "change") is None, "a task ran before its dependency"
        assert len(_starts(studio)) == 1

        # The agent finishes and names the commit; only then does the next start.
        _finish(studio, first, f"Готово. Коммит {HEAD[:10]}, тесты прошли.")
        second = _until(lambda: _task_conversation(studio, "change"),
                        what="the second task to start")
        _finish(studio, second, f"Готово. Коммит {HEAD}.")

        done = _until(lambda: (lambda d: d if d["run"]["state"] != "running" else None)(
            _detail(studio, project, run)), what="the run to finish")
        assert done["run"]["state"] == "completed", done["run"]
        assert done["run"]["iteration"] == 2
        assert {t["task_key"]: t["state"] for t in done["tasks"]} == {
            "audit": "completed_verified", "change": "completed_verified"}
        assert {t["task_key"]: t["conversation_id"] for t in done["tasks"]} == {
            "audit": first, "change": second}
        assert len(_starts(studio)) == 2, "a task was started more than once"

        # The branch was read with the account the project is connected through,
        # never with the server's own token.
        assert _head_reads(studio) == {f"Bearer {ACCOUNT_TOKEN}"}
        assert f"Bearer {SERVER_GITHUB_TOKEN}" not in _head_reads(studio)

        # A finished run is left alone: more passes start nothing.
        time.sleep(0.6)
        assert len(_starts(studio)) == 2
        assert studio.app.last_lab_tick.get("errors") == []


def _messages(studio, project):
    """The chat the run was asked for in, as the studio reads it."""
    status, listing = studio.api.get(f"/api/v1/projects/{project}/conversations")
    assert status == 200, listing
    conversation = listing["conversations"][0]["id"]
    status, body = studio.api.get(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages")
    assert status == 200, body
    return body["messages"]


def _told(studio, cid):
    """What NEXUS said to a conversation after its first message."""
    return list(studio.cloud.cloud.messages.get(cid, []))


def test_a_report_that_names_no_commit_is_put_right_and_the_run_goes_on(monkeypatch):
    """A result that does not verify is not the end of a run from the chat.

    The agent finished and named no commit. Nobody presses anything: the
    conversation that did the work is told what is missing, delivers it, the
    next task runs, the run completes - and its report arrives in the chat it
    was asked for in, with nobody watching."""
    with studio_stack(monkeypatch, lab_inline=True) as studio:
        project, run = _start_run(studio)
        first = _until(lambda: _task_conversation(studio, "audit"), what="the first task to start")
        _finish(studio, first, "Всё сделал, поверьте на слово.")

        told = _until(lambda: _told(studio, first), what="the agent to be told what is missing")
        assert "cannot accept it yet" in told[0] and "names no commit" in told[0]
        assert "Push your work" in told[0] and "do not start over" in told[0]
        detail = _detail(studio, project, run)
        assert detail["run"]["state"] == "running" and detail["run"]["policy"] == "durable"
        tasks = {t["task_key"]: t for t in detail["tasks"]}
        assert (tasks["audit"]["state"], tasks["audit"]["attempt"]) == ("running", 2)
        assert tasks["change"]["state"] == "pending", "a task ran on an unverified dependency"
        assert len(_starts(studio)) == 1, "no second conversation: the first still has the work"
        assert detail["report"] is None

        _finish(studio, first, f"Отправил. Коммит {HEAD}")
        second = _until(lambda: _task_conversation(studio, "change"),
                        what="the second task to start")
        _finish(studio, second, f"Коммит {HEAD}. Тесты: 14 passed.")
        done = _until(lambda: (lambda d: d if d["run"]["state"] != "running" else None)(
            _detail(studio, project, run)), what="the run to finish")
        assert done["run"]["state"] == "completed"
        assert {t["task_key"]: t["attempt"] for t in done["tasks"]} == {"audit": 2, "change": 1}
        assert [(e["task_key"], e["kind"]) for e in done["executions"]] == [
            ("audit", "start"), ("audit", "continue"), ("change", "start")]

        # The report: in the run's record, and in the chat - once.
        report = _until(lambda: (lambda d: d["report"] if d["report"]
                                 and d["report"]["state"] == "delivered" else None)(
            _detail(studio, project, run)), what="the report to be delivered")
        reports = [m for m in _messages(studio, project) if (m.get("meta") or {}).get("report")]
        assert len(reports) == 1 and reports[0]["content"] == report["body"]
        assert reports[0]["meta"]["run_id"] == run
        text = report["body"]
        assert "> Цель: покрыть модуль тестами" in text
        assert f"коммит `{HEAD[:10]}`" in text and "Тесты: 14 passed." in text
        assert "1 из 2 задач потребовали повторных запусков" in text
        time.sleep(0.5)                      # further passes of the loop add nothing
        assert len([m for m in _messages(studio, project)
                    if (m.get("meta") or {}).get("report")]) == 1


def test_a_run_given_a_budget_stops_when_it_is_spent_and_resume_tries_again(monkeypatch):
    """A run started over REST with an explicit budget keeps the behaviour it
    always had: a task that does not verify stops it, and «Продолжить» sends
    the task back with the attempts it has left."""
    with studio_stack(monkeypatch, lab_inline=True) as studio:
        project = studio.project()
        assert studio.api.post(f"/api/v1/projects/{project}/repository",
                               {"full_name": REPO})[0] == 200
        studio.github.commits = [{"sha": HEAD, "commit": {"message": "work"}}]
        studio.answers(json.dumps(PLAN))
        status, goal = studio.api.post(f"/api/v1/projects/{project}/goals",
                                       {"text": "Цель: покрыть модуль тестами"})
        assert status == 201, goal
        status, started = studio.api.post(
            f"/api/v1/projects/{project}/goals/{goal['goal']['id']}/run",
            {"repository": REPO, "branch": "main", "max_iterations": 10})
        assert status == 201, started
        run = started["run"]["id"]
        assert started["run"]["policy"] == "bounded"

        first = _until(lambda: _task_conversation(studio, "audit"), what="the first task to start")
        _finish(studio, first, "Всё сделал, поверьте на слово.")
        blocked = _until(lambda: (lambda d: d if d["run"]["state"] != "running" else None)(
            _detail(studio, project, run)), what="the run to stop")
        assert blocked["run"]["state"] == "blocked"
        tasks = {t["task_key"]: t for t in blocked["tasks"]}
        assert tasks["audit"]["state"] == "completed_unverified"
        assert tasks["audit"]["verification"]["reason"] == (
            "no verifiable commit reported by the agent")
        assert tasks["change"]["state"] == "pending", "a task ran on an unverified dependency"
        assert len(_starts(studio)) == 1 and _told(studio, first) == []

        # «Продолжить» sends the unverified task back; it gets a new conversation.
        status, body = studio.api.post(f"/api/v1/projects/{project}/runs/{run}/resume")
        assert status == 200, body
        again = _until(lambda: next(
            (cid for cid, c in list(studio.cloud.cloud.conversations.items())
             if (c.get("tags") or {}).get("nexustask") == "audit" and cid != first), None),
            what="the retry to start")
        _finish(studio, again, f"Коммит {HEAD}")
        second = _until(lambda: _task_conversation(studio, "change"),
                        what="the second task to start")
        _finish(studio, second, f"Коммит {HEAD}")
        done = _until(lambda: (lambda d: d if d["run"]["state"] != "running" else None)(
            _detail(studio, project, run)), what="the run to finish")
        assert done["run"]["state"] == "completed"
        assert {t["task_key"]: t["attempt"] for t in done["tasks"]} == {"audit": 2, "change": 1}


def test_cancel_tells_openhands_to_stop_and_the_run_leaves_the_list(monkeypatch):
    """«Отменить» in the studio: the run is settled and out of the list at
    once, the conversation at work is really told to stop (the sandbox that
    runs it is paused), and everything the run recorded can still be read."""
    with studio_stack(monkeypatch, lab_inline=True) as studio:
        project, run = _start_run(studio)
        first = _until(lambda: _task_conversation(studio, "audit"), what="the first task to start")
        sandbox = studio.cloud.cloud.conversations[first]["sandbox_id"]
        _until(lambda: _detail(studio, project, run)["tasks"][0]["state"] == "running",
               what="the task to be at work")

        status, body = studio.api.post(f"/api/v1/projects/{project}/runs/{run}/cancel")
        assert status == 200 and body["run"]["state"] == "cancelled", body
        assert body["run"]["hidden"] is True
        assert studio.api.get(f"/api/v1/projects/{project}/runs")[1]["runs"] == []

        _until(lambda: sandbox in studio.cloud.cloud.paused, what="OpenHands to be told to stop")
        stopped = _until(lambda: (lambda d: d if d["stops"] and d["stops"][0]["state"]
                                  == "confirmed" else None)(_detail(studio, project, run)),
                         what="the stop to be confirmed")
        assert stopped["stops"][0]["conversation_id"] == first
        assert stopped["stops"][0]["reason"] == "run cancelled"
        assert studio.cloud.cloud.conversations[first]["sandbox_status"] == "PAUSED"
        # Still there to be read: the run, its plan, who ran what.
        assert stopped["run"]["state"] == "cancelled" and len(stopped["tasks"]) == 2
        assert [e["outcome"] for e in stopped["executions"]] == ["cancelled"]
        assert stopped["report"] is None

        # The agent's late answer changes nothing, and nothing new is started.
        _finish(studio, first, f"Коммит {HEAD}")
        time.sleep(0.6)
        assert _task_conversation(studio, "change") is None and len(_starts(studio)) == 1
        assert studio.cloud.cloud.paused.count(sandbox) == 1, "told once, not on every pass"
        # A second cancel is the same as the first.
        status, again = studio.api.post(f"/api/v1/projects/{project}/runs/{run}/cancel")
        assert status == 200 and again["run"]["cancelled_at"] == body["run"]["cancelled_at"]

        # The same task can be asked for again at once.
        studio.answers(json.dumps(PLAN))
        conversation = studio.api.get(
            f"/api/v1/projects/{project}/conversations")[1]["conversations"][0]["id"]
        status, reply = studio.api.post(
            f"/api/v1/projects/{project}/conversations/{conversation}/messages",
            {"text": "Цель: покрыть модуль тестами", "mode": "autonomous"})
        assert status == 200, reply
        new_run = next(m["meta"]["run_id"] for m in reply["messages"]
                       if (m.get("meta") or {}).get("run_id"))
        assert new_run != run
        assert [r["id"] for r in studio.api.get(
            f"/api/v1/projects/{project}/runs")[1]["runs"]] == [new_run]


def test_cancelling_from_the_studio_stops_the_server_starting_more(monkeypatch):
    with studio_stack(monkeypatch, lab_inline=True) as studio:
        project, run = _start_run(studio)
        first = _until(lambda: _task_conversation(studio, "audit"), what="the first task to start")
        status, body = studio.api.post(f"/api/v1/projects/{project}/runs/{run}/cancel")
        assert status == 200 and body["run"]["state"] == "cancelled"

        # Even a perfect report arriving now changes nothing.
        _finish(studio, first, f"Коммит {HEAD}")
        time.sleep(0.8)
        detail = _detail(studio, project, run)
        assert detail["run"]["state"] == "cancelled"
        assert {t["task_key"]: t["state"] for t in detail["tasks"]} == {
            "audit": "blocked", "change": "blocked"}
        assert len(_starts(studio)) == 1


def test_left_alone_the_server_advances_runs_itself(monkeypatch):
    """The default. A deployment that is one web process and sets nothing - the
    Render blueprint - used to accept a run, plan it and never start a task."""
    monkeypatch.delenv("NEXUS_LAB_INLINE", raising=False)
    with studio_stack(monkeypatch) as studio:
        assert studio.app.lab_inline is True
        assert studio.api.get("/api/studio")[1]["lab"] == {"inline": True, "cleanup_hours": 24.0}
        project, run = _start_run(studio)
        first = _until(lambda: _task_conversation(studio, "audit"),
                       what="the first task to start with nothing configured")
        _finish(studio, first, f"Коммит {HEAD}")
        second = _until(lambda: _task_conversation(studio, "change"),
                        what="the second task to start")
        _finish(studio, second, f"Коммит {HEAD}")
        done = _until(lambda: (lambda d: d if d["run"]["state"] != "running" else None)(
            _detail(studio, project, run)), what="the run to finish")
        assert done["run"]["state"] == "completed"


def _lab_worker_check(studio):
    status, report = studio.api.get("/api/openhands/diagnostics")
    assert status == 200, report
    return next(c for c in report["checks"] if c["name"] == "lab_worker")


def test_the_servers_own_loop_is_a_live_worker_to_the_diagnostics(monkeypatch):
    """The diagnostics tell a live loop from a dead one by its heartbeat. The
    server's own loop wrote none, so on the one-process deployment - where it
    is the only thing advancing runs - the report read "active run(s) exist but
    no worker heartbeat is fresh" while the runs were going."""
    from orchestration.nexus_lab_store import LabStore

    with studio_stack(monkeypatch, lab_inline=True) as studio:
        lab = LabStore(studio.app.store)
        live = _until(lambda: lab.live_workers(max_age_seconds=30),
                      what="the server's loop to register")
        assert [(w["kind"], w["state"]) for w in live] == [("inline", "running")]
        assert live[0]["worker_id"] == studio.app._lab_worker_id

        _start_run(studio)                                  # a run that needs someone
        _until(lambda: _task_conversation(studio, "audit"), what="the first task to start")
        check = _lab_worker_check(studio)
        assert check["status"] == "VERIFIED", check
        assert check["evidence"]["live_workers"] == [studio.app._lab_worker_id]
        assert check["evidence"]["active_runs"] == 1

        # The server goes away - its loop says so on the way out - and one that
        # advances nothing comes up on the same database: the run now has
        # nobody, and the report says exactly that.
        studio.server.stop()
        after = studio.boot(lab_inline=False)
        assert LabStore(after.store).live_workers(max_age_seconds=30) == []
        check = _lab_worker_check(studio)
        assert check["status"] == "FAILED", check
        assert check["evidence"]["active_runs"] == 1


def test_switched_off_the_server_starts_a_run_and_does_not_advance_it(monkeypatch):
    """``NEXUS_LAB_INLINE=0``: for a deployment where a separate
    ``python -m orchestration.lab_worker`` does that work."""
    monkeypatch.setenv("NEXUS_LAB_INLINE", "0")
    with studio_stack(monkeypatch) as studio:
        assert studio.app.lab_inline is False
        project, run = _start_run(studio)
        time.sleep(1.0)                                   # five passes of the main loop
        detail = _detail(studio, project, run)
        assert detail["run"]["state"] == "running" and detail["run"]["iteration"] == 0
        assert {t["state"] for t in detail["tasks"]} == {"pending"}
        assert _starts(studio) == []


@pytest.mark.parametrize("value, expected", [
    ("0", False), ("false", False), ("No", False), (" OFF ", False),
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    # Not a clear "no": the loop stays on. A run that nobody advances is the
    # worse of the two mistakes.
    ("", True), ("maybe", True),
])
def test_the_switch_is_read_from_the_environment(monkeypatch, value, expected):
    monkeypatch.setenv("NEXUS_LAB_INLINE", value)
    with studio_stack(monkeypatch) as studio:
        assert studio.app.lab_inline is expected
        assert studio.api.get("/api/studio")[1]["lab"] == {"inline": expected, "cleanup_hours": 24.0}


def test_an_answer_given_through_the_finish_tool_is_the_report(monkeypatch):
    """An agent may end by calling ``finish`` instead of writing a message. Its
    report is then the tool's message, and the task verifies on it - it is not
    "finished with no answer", started over from nothing."""
    with studio_stack(monkeypatch, lab_inline=True) as studio:
        project, run = _start_run(studio)
        first = _until(lambda: _task_conversation(studio, "audit"), what="the first task to start")
        cloud = studio.cloud.cloud
        cloud.add_assistant_message(first, "Смотрю репозиторий.")     # earlier, not the report
        cloud.add_finish(first, f"Готово. Коммит {HEAD}.")
        cloud.conversations[first]["execution_status"] = "finished"
        second = _until(lambda: _task_conversation(studio, "change"),
                        what="the second task to start")
        tasks = {t["task_key"]: t for t in _detail(studio, project, run)["tasks"]}
        assert (tasks["audit"]["state"], tasks["audit"]["attempt"]) == ("completed_verified", 1)
        assert tasks["audit"]["evidence"][-1]["final_text"] == f"Готово. Коммит {HEAD}."
        assert len(_starts(studio)) == 2, "the first task was started again"
        _finish(studio, second, f"Коммит {HEAD}")
