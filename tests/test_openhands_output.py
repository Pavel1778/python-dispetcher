"""The OpenHands output contract, pinned against the real event shapes.

These cases were written from conversations read off the **live** agent-server
(v1.60.1, 2026-11-15), not from a guess. What they hold:

* a conversation that did not reach the terminal state ``finished`` is never
  reported as a success, even when the transcript carries an agent message or a
  ``finish`` action (a live conversation can sit in ``error`` *and* carry a long
  final message);
* the answer is read the way the SDK reads it: a ``finish`` tool's
  ``action.message`` is the answer, and so is a later assistant message;
* a long conversation's answer is still found (the end is what a bounded read
  misses, not the beginning);
* an empty or absent ``execution_status`` is "still at work", never a verdict.

No database and no mock: a real local HTTP server (``tests/fake_cloud.py``)
speaks the Cloud surface, and the client under test is the real one.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.nexus_lab import OpenHandsExecutor  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402


def _client(cloud):
    return CloudClient(cloud.base_url, api_key="k", timeout=10.0)


def _executor(client):
    return OpenHandsExecutor(client, poll_interval=0.0)


def test_a_finish_tool_message_is_the_answer():
    """The answer of an agent that ends by calling ``finish``.

    Live shape: ``ActionEvent`` with ``tool_name='finish'`` and the text in
    ``action.message``. It is not an ``llm_message``; reading only messages
    would lose every such answer.
    """
    with FakeCloudServer() as cloud:
        cid = cloud.cloud.make_conversation(title="t", execution_status="finished")
        cloud.cloud.add_finish(cid, "Готово. Коммит 0123abc.")
        report = _executor(_client(cloud)).poll({"id": "t", "task_key": "t"}, cid)
        assert report.state == "finished"
        assert report.final_text == "Готово. Коммит 0123abc."


def test_an_assistant_message_is_the_answer_when_no_finish_is_called():
    with FakeCloudServer() as cloud:
        cid = cloud.cloud.make_conversation(title="t", execution_status="finished")
        cloud.cloud.add_assistant_message(cid, "All work is complete. Here is the final report.")
        report = _executor(_client(cloud)).poll({"id": "t", "task_key": "t"}, cid)
        assert report.state == "finished"
        assert report.final_text == "All work is complete. Here is the final report."


def test_a_conversation_that_did_not_finish_is_never_a_success():
    """``error`` is not ``finished`` even when the transcript carries an answer.

    Reproduced live: a conversation sits in ``error`` (the account's
    ``security_analyzer`` is rejected) while ``/agent_final_response`` still
    returns a 5 098-character report. The verdict is the state, not the text.
    """
    for state in ("error", "stuck"):
        with FakeCloudServer() as cloud:
            cid = cloud.cloud.make_conversation(title="t", execution_status=state)
            cloud.cloud.add_assistant_message(cid, "A long report the agent wrote anyway.")
            cloud.cloud.add_finish(cid, "И ещё одно сообщение.")
            report = _executor(_client(cloud)).poll({"id": "t", "task_key": "t"}, cid)
            assert report.state == state, "the state is the verdict, not the text"
            assert report.final_text == "", "a non-finished conversation has no accepted answer"


def test_an_absent_status_is_still_at_work_not_a_verdict():
    """An empty ``execution_status`` is asked again, never settled.

    The live service leaves the field empty while it cannot ask the sandbox.
    Settling on that would end a task seconds after it began.
    """
    with FakeCloudServer() as cloud:
        cid = cloud.cloud.make_conversation(title="t", execution_status=None)
        report = _executor(_client(cloud)).poll({"id": "t", "task_key": "t"}, cid)
        assert report.state == "running"
        assert report.final_text == ""


def test_the_answer_is_found_at_the_end_of_a_long_conversation():
    """A conversation longer than the event page cap still yields its answer.

    Live conversations run to thousands of events (one measured 3 790). Read
    oldest-first and bounded, the end is exactly the part never reached.
    """
    with FakeCloudServer() as cloud:
        cid = cloud.cloud.make_conversation(title="t", execution_status="finished")
        for _ in range(1_950):
            cloud.cloud.events[cid].append(
                {"kind": "ObservationEvent", "tool_name": "terminal",
                 "observation": {"kind": "TerminalObservation"}})
        cloud.cloud.add_finish(cid, "Готово. Коммит deadbee.")
        report = _executor(_client(cloud)).poll({"id": "t", "task_key": "t"}, cid)
        assert report.state == "finished"
        assert report.final_text == "Готово. Коммит deadbee."


def test_a_conversation_that_vanished_is_missing_not_finished():
    with FakeCloudServer() as cloud:
        report = _executor(_client(cloud)).poll(
            {"id": "t", "task_key": "t"}, "11111111-1111-1111-1111-111111111111")
        assert report.state == "missing"
        assert report.final_text == ""
