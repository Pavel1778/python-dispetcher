"""The full autonomous pipeline, end to end, over real HTTP.

This is the acceptance for "Проверь весь pipeline" and "OpenHands должен уметь
…": every stage is driven for real and asserted on what it *did*, never on what
it said.

    USER PROMPT → PLANNER → DAG → SCHEDULER → OPENHANDS → EXECUTION
                → QA → EVIDENCE → ACCEPTANCE → NOTIFICATION

The server is the real :class:`orchestration.web_server.WebServer` on real
PostgreSQL, and the outside world is the same three local socket servers the
studio tests use: ``tests/fake_cloud.py`` (OpenHands Cloud), ``tests/fake_github.py``
(GitHub, which records the commit it is asked for and the identity of every
read) and a fake LLM provider (the PLANNER). Nothing about the pipeline is
mocked: the client speaks real HTTP, the store is real SQL, and the loop is the
server's own.

Two rules from the task text are enforced throughout:

* "Не считай текстовый ответ OpenHands доказательством выполнения." — a task is
  ``completed_verified`` only when the head of its branch on GitHub is a commit
  the report names. A glowing report that names no commit is not accepted:
  the task goes back to work (a run with explicit budgets stops instead).
* "NEXUS должен контролировать каждый этап." — each stage has its own assertion,
  and the stages are asserted to be causally ordered (no task before its
  dependency; no evidence before a verdict; no notification before the run ends).
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_studio_api import (  # noqa: E402
    DSN,
    REPO,
    studio_stack,
)

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run pipeline tests")


@pytest.fixture(autouse=True)
def evidence_key(monkeypatch):
    """Give the EVIDENCE stage a real Ed25519 signer for every test here.

    Without it the loop still runs, but ``EvidenceService.record`` refuses (a
    record must never be stored unsigned), so the EVIDENCE stage would be
    silently skipped. The pipeline test asserts it is *active*, so the key is
    configured the way an operator configures it: a private key and the matching
    keyring entry.
    """
    import base64

    from orchestration.evidence_trust import generate_signer, public_key_b64

    signer = generate_signer("pipeline-key")
    monkeypatch.setenv("NEXUS_EVIDENCE_KEY_ID", "pipeline-key")
    monkeypatch.setenv(
        "NEXUS_EVIDENCE_PRIVATE_KEY",
        base64.urlsafe_b64encode(signer.private_key.private_bytes_raw()).decode())
    monkeypatch.setenv("NEXUS_EVIDENCE_KEYRING", f"pipeline-key:{public_key_b64(signer)}")
    return signer

#: The commit the fake GitHub reports as the branch head. The agent names it and
#: the Git verifier accepts the task only because the two agree.
HEAD = "9a8b7c6d5e4f30211203f4e5d6c7b8a901234567"

#: The PLANNER's answer: a two-task DAG with a real dependency (audit → change).
PLAN = {"tasks": [
    {"id": "audit", "title": "Изучить код", "description": "Прочитать модуль и описать.",
     "acceptance": ["сводка"], "dependencies": []},
    {"id": "change", "title": "Внести изменение", "description": "Реализовать и протестировать.",
     "acceptance": ["тесты проходят"], "dependencies": ["audit"]},
]}


def _until(read, *, timeout=25.0, what="the condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _start_run(studio, *, text="Цель: покрыть модуль тестами"):
    """USER PROMPT → PLANNER → DAG → a run whose tasks are pending.

    Connect the repository (allowlist + link + bind), send one autonomous chat
    turn, and let the planner answer. Returns the project and the run id.
    """
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO, "account_id": studio.account()})[0] == 200
    studio.github.commits = [{"sha": HEAD, "commit": {"message": "work"}}]
    conversation = studio.conversation(project, mode="autonomous")
    studio.answers(json.dumps(PLAN))
    status, reply = studio.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages",
        {"text": text, "mode": "autonomous"})
    assert status == 200, reply
    run_id = reply["messages"][2]["meta"]["run_id"]
    return project, run_id


def _detail(studio, project, run):
    status, detail = studio.api.get(f"/api/v1/projects/{project}/runs/{run}")
    assert status == 200, detail
    return detail


def _conversation_for(studio, key):
    """The Cloud conversation the lab started for one task, once it exists."""
    for cid, conv in list(studio.cloud.cloud.conversations.items()):
        if (conv.get("tags") or {}).get("nexustask") == key:
            return cid
    return None


def _finish(studio, cid, text):
    """The agent's conversation ends, with *text* as its report."""
    studio.cloud.cloud.add_assistant_message(cid, text)
    studio.cloud.cloud.conversations[cid]["execution_status"] = "finished"


def _starts(studio):
    return [c for c in studio.cloud.cloud.calls if c == ("POST", "/api/v1/app-conversations")]


# ------------------------------------------------------------- the whole pipeline
def test_the_full_pipeline_runs_every_stage_in_order(monkeypatch):
    """One request drives every stage, and each stage leaves its own durable mark.

    The assertions are ordered the way the pipeline is: a task cannot start
    before its dependency verified, the run cannot complete before every task
    verified, and the notification cannot exist before the run ended.
    """
    with studio_stack(monkeypatch, lab_inline=True) as studio:
        # --- USER PROMPT → PLANNER → DAG ------------------------------------
        project, run = _start_run(studio)
        # The plan is a real DAG with the dependency the planner declared.
        planned = _until(lambda: (lambda d: d if len(d["tasks"]) == 2 else None)(
            _detail(studio, project, run)), what="the plan to be stored as a DAG")
        by_key = {t["task_key"]: t for t in planned["tasks"]}
        assert set(by_key) == {"audit", "change"}
        assert by_key["change"]["dependencies"] == ["audit"]
        assert by_key["audit"]["acceptance"] == ["сводка"]
        assert planned["run"]["state"] == "running"

        # --- SCHEDULER → OPENHANDS (EXECUTION) ------------------------------
        first = _until(lambda: _conversation_for(studio, "audit"),
                       what="the scheduler to dispatch the first task")
        started = studio.cloud.cloud.start_bodies[first]
        # The scheduler authorized the dispatch and told OpenHands where to work.
        assert (started["selected_repository"], started["selected_branch"]) == (REPO, "main")
        assert "Изучить код" in started["initial_message"]["content"][0]["text"]
        # The dependency gates the DAG: the second task is NOT started yet.
        time.sleep(0.6)
        assert _conversation_for(studio, "change") is None
        assert len(_starts(studio)) == 1

        # --- QA (independent verification) + EVIDENCE -----------------------
        # The agent's report names the branch head, so the Git verifier (the QA
        # stage) accepts the task, and a signed evidence record is written.
        _finish(studio, first, f"Готово. Коммит {HEAD}, тесты прошли.")

        def _audit_verified():
            d = _detail(studio, project, run)
            t = {x["task_key"]: x for x in d["tasks"]}["audit"]
            return t if t["state"] == "completed_verified" else None

        audit = _until(_audit_verified, what="the QA stage to verify the first task")
        assert audit["verification"]["reason"] == "remote branch is at the reported commit"
        assert audit["evidence"][-1]["remote_sha"] == HEAD

        # EVIDENCE stage: a signed, re-verifiable record now exists for the task.
        # The verdict and the evidence record are written in two transactions
        # (record_verification commits, then the record is signed), so under load
        # the verdict can be visible a moment before the record. Wait for it.
        ev = _until(lambda: _evidence_for_task(studio, project, audit["id"]),
                    what="the EVIDENCE stage to write the signed record")
        assert ev["verdict"] == "VERIFIED"
        assert ev["commit_sha"] == HEAD and ev["branch"] == "main"

        # --- the dependent task now starts --------------------------------
        second = _until(lambda: _conversation_for(studio, "change"),
                        what="the dependent task to start after its dependency verified")
        _finish(studio, second, f"Готово. Коммит {HEAD}.")

        # --- ACCEPTANCE: the run completes only when every task verified -----
        done = _until(lambda: (lambda d: d if d["run"]["state"] != "running" else None)(
            _detail(studio, project, run)), what="the run to reach acceptance")
        assert done["run"]["state"] == "completed", done["run"]
        assert {t["task_key"]: t["state"] for t in done["tasks"]} == {
            "audit": "completed_verified", "change": "completed_verified"}
        assert done["run"]["iteration"] == 2
        assert len(_starts(studio)) == 2, "a task was started more than once"

        # --- NOTIFICATION ---------------------------------------------------
        _, notes = studio.api.get(f"/api/v1/projects/{project}/notifications")
        kinds = [n["kind"] for n in notes["notifications"]]
        assert "goal_started" in kinds
        assert "goal_completed" in kinds
        assert "task_completed_verified" in kinds

        # A finished run is left alone: more passes start nothing.
        time.sleep(0.6)
        assert len(_starts(studio)) == 2
        assert studio.app.last_lab_tick.get("errors") == []


def test_a_text_answer_is_never_proof_of_work(monkeypatch):
    """The task text's central rule: "Не считай текстовый ответ OpenHands
    доказательством выполнения."

    A conversation that finishes with a confident report but names no commit is
    not accepted: its dependent task is never started and the run does not
    complete. A run started from the chat is durable, so it does not stop there
    either ("ошибка проверки приводит к повторной работе, а не к ложному
    завершению"): the agent is told what is missing, in the conversation that
    did the work, and the task is at work again. A run with explicit budgets
    still stops at this point, as it always did
    (``tests/test_lab_inline.py::test_a_run_given_a_budget_stops_when_it_is_spent_and_resume_tries_again``).
    """
    with studio_stack(monkeypatch, lab_inline=True) as studio:
        project, run = _start_run(studio)
        first = _until(lambda: _conversation_for(studio, "audit"),
                       what="the first task to start")
        _finish(studio, first, "Всё готово, поверьте на слово.")

        # Not accepted, and said so where the work was done.
        told = _until(lambda: studio.cloud.cloud.messages.get(first),
                      what="the agent to be told what is missing")
        assert len(told) == 1 and "names no commit" in told[0]

        def _back_at_work():
            d = _detail(studio, project, run)
            t = {x["task_key"]: x for x in d["tasks"]}["audit"]
            return d if t["state"] == "running" and t["attempt"] == 2 else None

        detail = _until(_back_at_work, what="the task to be at work again")
        assert detail["run"]["state"] == "running", "the run neither stopped nor completed"
        tasks = {t["task_key"]: t for t in detail["tasks"]}
        assert tasks["audit"]["conversation_id"] == first
        assert tasks["audit"]["verification"]["reason"] == (
            "no verifiable commit reported by the agent")
        # The dependency was not trusted, so nothing downstream ran.
        assert tasks["change"]["state"] == "pending"
        assert len(_starts(studio)) == 1

        # The attempt that was not accepted is still signed into evidence: the
        # record binds the commit the verifier *observed* on the branch, not a
        # claim by the agent, and the verdict in it says it did not verify.
        ev = _until(lambda: _evidence_for_task(studio, project, tasks["audit"]["id"]),
                    what="the EVIDENCE stage to write the signed record")
        assert ev["verdict"] == "VERIFIED"
        assert ev["commit_sha"] == HEAD, "the record must bind the observed branch head"
        assert ev["body"]["detail"]["verdict"] == "completed_unverified"

        # Told what was missing, the agent names the commit; now it counts.
        _finish(studio, first, f"Коммит {HEAD}")
        second = _until(lambda: _conversation_for(studio, "change"),
                        what="the dependent task to start once its dependency verified")
        _finish(studio, second, f"Коммит {HEAD}")
        done = _until(lambda: (lambda d: d if d["run"]["state"] != "running" else None)(
            _detail(studio, project, run)), what="the run to finish")
        assert done["run"]["state"] == "completed"
        assert {t["task_key"]: (t["state"], t["attempt"]) for t in done["tasks"]} == {
            "audit": ("completed_verified", 2), "change": ("completed_verified", 1)}


def test_the_loop_is_advanced_by_the_separate_worker_not_the_server(monkeypatch):
    """NEXUS_LAB_INLINE=0: the server accepts and plans the run but advances
    nothing; the same code the separate ``lab_worker`` process runs does."""
    from orchestration.lab_worker import LabWorker

    monkeypatch.setenv("NEXUS_LAB_INLINE", "0")
    with studio_stack(monkeypatch) as studio:
        assert studio.app.lab_inline is False
        project, run = _start_run(studio)
        time.sleep(1.0)
        detail = _detail(studio, project, run)
        assert detail["run"]["state"] == "running" and detail["run"]["iteration"] == 0
        assert {t["state"] for t in detail["tasks"]} == {"pending"}
        assert _starts(studio) == []

        # The worker's own loop takes the run from here. It is the real class the
        # ``python -m orchestration.lab_worker`` entry point uses.
        worker = LabWorker(studio.app.store, worker_id="pipeline-worker",
                           client=studio.app.client, github=studio.app.github,
                           github_accounts=studio.app.github_accounts,
                           evidence=studio.app.evidence)
        first = _until(lambda: (worker.tick_once(), _conversation_for(studio, "audit"))[1],
                       what="the worker to dispatch the first task")
        _finish(studio, first, f"Коммит {HEAD}")
        second = _until(lambda: (worker.tick_once(), _conversation_for(studio, "change"))[1],
                        what="the worker to dispatch the second task")
        _finish(studio, second, f"Коммит {HEAD}")
        _until(lambda: (worker.tick_once(),
                        _detail(studio, project, run)["run"]["state"] == "completed")[1],
               what="the worker to complete the run")
        assert _detail(studio, project, run)["run"]["state"] == "completed"


def test_the_agent_is_told_every_capability_the_task_requires(monkeypatch):
    """The task text lists what OpenHands must be able to do. The first message
    the agent receives must ask for each of them in so many words, and the
    delivery must carry the task's acceptance criteria and the run identity."""
    from orchestration.nexus_lab import OpenHandsExecutor

    told = OpenHandsExecutor.instructions({
        "task_key": "change", "title": "Внести изменение",
        "description": "Реализовать и протестировать.",
        "repository": REPO, "branch": "main", "run_id": "11111111-1111-1111-1111-111111111111",
        "acceptance": ["тесты проходят"]})
    # get the task, inspect the repository, read/change files, Git, run commands,
    # install allowed deps, run tests, analyze and fix errors, report results.
    for phrase in (
        "Inspect the repository",                       # анализировать репозиторий
        "read and change the files",                    # читать и изменять файлы
        "use Git",                                      # работать с Git
        "install only the dependencies the repository already allows",  # зависимости
        "Run the relevant tests",                       # выполнять тесты
        "find the cause and fix it",                    # анализировать/исправлять ошибки
        f"push it to branch main of {REPO}",            # возвращать результаты
        "committed and not pushed does not count",
        "тесты проходят",                               # acceptance criteria
        "NEXUS run 11111111",                           # continues unfinished work
    ):
        assert phrase in told, f"the agent is not told: {phrase!r}"


# ------------------------------------------------------------------- helpers
def _evidence_for_task(studio, project, task_id):
    """The newest signed evidence record whose subject is *task_id*."""
    status, body = studio.api.get(f"/api/v1/projects/{project}/evidence")
    assert status == 200, body
    rows = [e for e in body["evidence"]
            if e.get("subject_kind") == "task" and str(e.get("subject_id")) == str(task_id)]
    return rows[0] if rows else None


def test_evidence_records_are_re_verifiable_with_the_trust_engine(monkeypatch, evidence_key):
    """The EVIDENCE stage is not decoration: the record it writes re-verifies
    with the real keyring through the public route, and its commit binds."""
    with studio_stack(monkeypatch, lab_inline=True) as studio:
        project, run = _start_run(studio)
        first = _until(lambda: _conversation_for(studio, "audit"),
                       what="the first task to start")
        _finish(studio, first, f"Коммит {HEAD}")
        audit = _until(lambda: (lambda d: (lambda t: t if t["state"] == "completed_verified"
                                           else None)({x["task_key"]: x for x in d["tasks"]}["audit"]))(
            _detail(studio, project, run)), what="the first task to verify")
        ev = _until(lambda: _evidence_for_task(studio, project, audit["id"]),
                    what="the EVIDENCE stage to write the signed record")
        # Re-verify through the route: the signature and the chain must hold.
        status, body = studio.api.post(
            f"/api/v1/projects/{project}/evidence/{ev['id']}/verify",
            {"expected_commit": HEAD})
        assert status == 200, body
        assert body["evidence"]["verdict"] == "VERIFIED", body["evidence"]
