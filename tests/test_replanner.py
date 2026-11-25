"""The LLM-driven replanner: bounded, honest, and signed into evidence.

The pure part pins the prompt and the safety rules (bounded, never raises,
redacts secrets, strips a fence). The integration part drives a **real**
durable run through the **real** scheduler and store on PostgreSQL, with the
small scripted executor/verifier the durable-runs suite already uses: a task
fails verification, the replanner is asked once, the correction is delivered
to the next attempt, and an ``AUTO_CORRECTED`` record is signed by the real
Evidence Trust Engine. After three corrections, the fourth failure gets none.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.evidence_trust import (  # noqa: E402
    EvidenceTrustEngine, Keyring, generate_signer, public_key_b64,
)
from orchestration.nexus_evidence import EvidenceService  # noqa: E402
from orchestration.nexus_lab import TaskReport, Verification  # noqa: E402
from orchestration.replanner import Correction, Replanner  # noqa: E402
from tests.test_durable_runs import (  # noqa: E402, F401
    Conversations, Verdicts, env as durable_env, scheduler, start, tasks_of,
)

DSN = os.environ.get("TEST_SUPABASE_DB_URL")


@pytest.fixture
def env(durable_env):  # noqa: F811 - re-exported under this module's name
    """The durable-runs fixture (real PostgreSQL), under this module's name."""
    return durable_env


def _report(text: str = "I changed it locally") -> TaskReport:
    return TaskReport(state="finished", final_text=text)


def _verdict(reason: str = "no verifiable commit reported by the agent") -> Verification:
    return Verification("completed_unverified", reason, {"remote_sha": "abc123"}, retry="restart")


# ------------------------------------------------------------------- pure part
def test_the_prompt_names_the_failure_the_repository_and_the_report():
    planner = Replanner(lambda prompt, system: "do better")
    prompt = planner.build_prompt(
        {"task_key": "t1", "repository": "acme/app", "branch": "main"},
        _verdict(), _report("I changed it locally"), attempt=1)
    assert "no verifiable commit reported by the agent" in prompt
    assert "acme/app" in prompt
    assert "main" in prompt
    assert "I changed it locally" in prompt
    assert "Correction 1 of 3" in prompt


def test_a_correction_is_bounded_by_the_attempt_ceiling():
    planner = Replanner(lambda prompt, system: "try again", max_attempts=3)
    task = {"task_key": "t1"}
    assert isinstance(planner.correct(task, _verdict(), _report(), attempt=3), Correction)
    assert planner.correct(task, _verdict(), _report(), attempt=4) is None


def test_a_provider_failure_yields_no_correction_and_does_not_raise():
    def boom(prompt, system):
        raise RuntimeError("provider down")

    planner = Replanner(boom)
    assert planner.correct({"task_key": "t1"}, _verdict(), _report(), attempt=1) is None


def test_an_empty_answer_yields_no_correction():
    planner = Replanner(lambda prompt, system: "   \n  ")
    assert planner.correct({"task_key": "t1"}, _verdict(), _report(), attempt=1) is None


def test_a_markdown_fence_is_stripped_and_the_note_is_clipped():
    planner = Replanner(lambda prompt, system: "```python\nPush the branch.\n```")
    correction = planner.correct({"task_key": "t1"}, _verdict(), _report(), attempt=1)
    assert correction is not None
    assert correction.note == "Push the branch."


def test_secrets_in_the_report_never_reach_the_prompt():
    planner = Replanner(lambda prompt, system: "ok")
    prompt = planner.build_prompt(
        {"task_key": "t1"}, _verdict(),
        _report("token ghp_abcdefghijklmnopqrstuvwxyz0123456789 leaked"), attempt=1)
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in prompt
    assert "«redacted»" in prompt


def test_the_stored_attempt_count_is_read_from_the_task():
    planner = Replanner(lambda prompt, system: "again", max_attempts=2)
    task = {"task_key": "t1", "verification": {"auto_corrections": 2}}
    assert planner.correct(task, _verdict(), _report()) is None


# --------------------------------------------------------------- real scheduler
pytestmark_db = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run the scheduler integration")


def _evidence(store):
    signer = generate_signer("k1")
    keyring = Keyring()
    keyring.add("k1", public_key_b64(signer))
    return EvidenceService(store, EvidenceTrustEngine(keyring, signer))


class RecordingConversations(Conversations):
    """The durable-runs executor, recording the note each start is given."""

    def __init__(self):
        super().__init__()
        self.notes: list[str] = []

    def dispatch(self, task):
        self.notes.append(task.get("resume_note") or "")
        return super().dispatch(task)


def _auto_corrected_rows(store) -> list[dict]:
    with store._tx() as cur:
        cur.execute("SELECT body FROM nexus_evidence WHERE created_by='lab:replanner' "
                    "ORDER BY seq")
        return [row["body"] for row in cur.fetchall()]


@pytestmark_db
def test_a_failed_task_gets_a_correction_delivered_and_signed(env):
    calls: list[str] = []

    def text(prompt, system):
        calls.append(prompt)
        return "Check the head of the branch, push, and name the new SHA."

    executor = RecordingConversations()
    verifier = Verdicts({"t1": [_verdict()]})
    sched = scheduler(env, executor, verifier,
                      replanner=Replanner(text, max_attempts=3),
                      evidence=_evidence(env["store"]))
    run_id = start(env, [{"task_key": "t1", "title": "do it"}],
                   max_attempts_per_task=10)

    sched.tick(run_id)                       # claim + dispatch
    executor.finish(executor.of("t1")[-1], "I changed it locally")
    sched.tick(run_id)                       # poll -> verify -> retry

    assert len(calls) == 1
    assert "no verifiable commit" in calls[0]
    task = tasks_of(env, run_id)["t1"]
    assert task["verification"]["auto_corrections"] == 1
    assert "push" in task["verification"]["auto_correction"]["note"].lower()
    # The correction reached the next start's instruction, not only the record.
    assert "Автокоррекция 1/3" in executor.notes[1]
    rows = _auto_corrected_rows(env["store"])
    assert len(rows) == 1
    assert rows[0]["detail"]["status"] == "AUTO_CORRECTED"
    assert rows[0]["detail"]["auto_corrected"] is True


@pytestmark_db
def test_at_most_three_corrections_are_asked_for(env):
    calls: list[int] = []

    def text(prompt, system):
        calls.append(1)
        return "try a different approach"

    executor = Conversations()
    verifier = Verdicts({"t1": [_verdict()]})
    sched = scheduler(env, executor, verifier,
                      replanner=Replanner(text, max_attempts=3),
                      evidence=_evidence(env["store"]))
    run_id = start(env, [{"task_key": "t1", "title": "do it"}],
                   max_attempts_per_task=10)

    for _ in range(4):
        sched.tick(run_id)                   # claim + dispatch
        executor.finish(executor.of("t1")[-1], "still wrong")
        sched.tick(run_id)                   # poll -> verify -> retry

    assert len(calls) == 3
    task = tasks_of(env, run_id)["t1"]
    assert task["verification"]["auto_corrections"] == 3
    assert len(_auto_corrected_rows(env["store"])) == 3


@pytestmark_db
def test_a_replanner_outage_does_not_stop_the_retry(env):
    def boom(prompt, system):
        raise RuntimeError("no provider")

    executor = Conversations()
    verifier = Verdicts({"t1": [_verdict()]})
    sched = scheduler(env, executor, verifier,
                      replanner=Replanner(boom),
                      evidence=_evidence(env["store"]))
    run_id = start(env, [{"task_key": "t1", "title": "do it"}],
                   max_attempts_per_task=10)

    sched.tick(run_id)
    executor.finish(executor.of("t1")[-1], "I changed it locally")
    sched.tick(run_id)

    task = tasks_of(env, run_id)["t1"]
    # The retry happened: a second conversation was started for the task. (The
    # same tick that retries may claim it again, so the state is not "pending".)
    assert len(executor.of("t1")) == 2
    assert "auto_corrections" not in (task["verification"] or {})
    assert _auto_corrected_rows(env["store"]) == []


@pytestmark_db
def test_without_a_replanner_the_deterministic_note_is_used(env):
    executor = Conversations()
    verifier = Verdicts({"t1": [_verdict()]})
    sched = scheduler(env, executor, verifier, evidence=_evidence(env["store"]))
    run_id = start(env, [{"task_key": "t1", "title": "do it"}],
                   max_attempts_per_task=10)

    sched.tick(run_id)
    executor.finish(executor.of("t1")[-1], "I changed it locally")
    sched.tick(run_id)

    task = tasks_of(env, run_id)["t1"]
    assert len(executor.of("t1")) == 2
    assert "auto_corrections" not in (task["verification"] or {})
    assert _auto_corrected_rows(env["store"]) == []
