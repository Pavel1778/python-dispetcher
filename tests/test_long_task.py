"""A task text of any size starts an autonomous run, and reaches the agent whole.

An autonomous start used to fail with ``text exceeds 20000 characters``: the
goal row was the only place the text lived and it holds 20 000. These tests
drive the whole path over HTTP - chat box or REST, to the first message an
agent receives - on the real server and real PostgreSQL, with the direct
OpenHands transport against two socket servers (the fake Cloud that provisions
a sandbox and the fake agent-server inside it), the fake GitHub and a planning
model that reads its prompt (``tests/long_task_kit.py``).

What "works" means here is narrow on purpose: the bytes the agent has are the
bytes that were sent. Every size is checked by SHA-256 at three places - what
the server stored, what the plan accounts for, what the agent was given.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.long_task_kit import (  # noqa: E402
    Lab,
    agent_bytes,
    sha256,
    specification,
)
from tests.test_studio_api import DSN, REPO, studio_stack  # noqa: E402

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run long-task tests")

SIZES = [19_999, 20_000, 20_001, 50_000, 100_000, 200_000, 500_000, 1_000_000]


@pytest.fixture
def lab(monkeypatch):
    with studio_stack(monkeypatch, transport="direct", lab_inline=True) as studio:
        yield Lab(studio, repo=REPO)


@pytest.mark.parametrize("chars", SIZES)
def test_a_text_of_this_size_starts_one_run_and_the_agent_has_all_of_it(lab, chars):
    text = specification(chars)
    assert len(text) == chars
    digest = sha256(text)

    status, reply = lab.send(text)
    assert status == 200, reply
    spec_id = reply["messages"][0]["meta"]["spec"]["id"]

    # Stored: byte for byte what was sent.
    assert lab.stored(spec_id) == text.encode("utf-8")
    spec = lab.spec(spec_id)
    assert (spec["char_count"], spec["sha256"]) == (chars, digest)

    # One goal, one run - announced in the conversation, at once or when the
    # last part of the plan is ready.
    run_id = lab.wait_run()
    assert [g["spec_id"] for g in lab.goals()] == [spec_id]
    assert [r["id"] for r in lab.runs()] == [run_id]

    # The plan accounts for every piece of the text.
    spec = lab.spec(spec_id)
    assert spec["state"] == "planned" and spec["run_id"] == run_id
    covered = spec["coverage"]
    assert covered["pieces"] >= spec["requirement_count"]
    assert (covered["in_tasks"] + covered["declared_context"] + covered["implicit_context"]
            + covered["in_verification_tasks"]) == covered["pieces"]

    # The agent of the first task has the whole original.
    detail = lab.run(run_id)
    first = detail["tasks"][0]
    conversation = lab.agent(first["task_key"], run_id)
    mode, data = agent_bytes(conversation, digest)
    assert mode == ("inline" if chars <= 20_000 else "file")
    assert data is not None, "the agent was started without the text"
    assert len(data) == len(text.encode("utf-8"))
    assert sha256(data.decode("utf-8")) == digest
    assert data == text.encode("utf-8")
