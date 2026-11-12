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
        assert studio.api.get("/api/studio")[1]["lab"] == {"inline": True}
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


def test_a_report_that_names_no_commit_stops_the_run_and_resume_tries_again(monkeypatch):
    with studio_stack(monkeypatch, lab_inline=True) as studio:
        project, run = _start_run(studio)
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
        assert len(_starts(studio)) == 1

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
        assert studio.api.get("/api/studio")[1]["lab"] == {"inline": True}
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
        assert studio.api.get("/api/studio")[1]["lab"] == {"inline": expected}


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
