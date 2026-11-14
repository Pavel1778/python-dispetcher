"""Architectural requirement, stated explicitly as a regression contract:

one conversation may hold several independent, concurrent autonomous runs.
This is intentional product behaviour, not a bug - see
``tests/test_browser_e2e.py::test_a_phone_scrolls_the_list_of_runs_to_the_last_one_with_a_finger``,
which already drives four concurrent runs from one conversation through the
real UI and asserts all four stay visible together on the "Агент" tab.

A prior session attempted a conversation-level "only one active run" guard
to close what looked like a gap next to the spec-level idempotency in
``nexus_specs.py``. It was reverted after that e2e test caught it breaking
this exact contract. These tests pin the contract down at the API level so
it cannot regress silently again, no matter which layer a future change
touches:

* an identical resubmitted message never creates a second run (idempotency,
  unchanged and still enforced by ``nexus_specs.py``'s per-text/per-goal
  dedup);
* a distinct new message is free to create its own independent run even
  while another run in the same conversation is still active;
* "Продолжи" resumes the run this conversation most recently started /
  linked, never starts a new one and never touches a different one;
* every run a conversation has started stays individually visible and
  queryable, regardless of how many are concurrently active;
* cancelling one run changes only that run - a second, independent run in
  the same conversation is completely unaffected.
"""
from __future__ import annotations

import json

import pytest

from tests.test_studio_api import DSN, REPO, studio_stack

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run these tests")


def _plan(label: str) -> str:
    return json.dumps({"tasks": [
        {"id": f"t-{label}", "title": f"Задача {label}", "description": "Описание.",
         "acceptance": ["готово"], "dependencies": []},
    ]})


@pytest.fixture
def studio(monkeypatch):
    with studio_stack(monkeypatch) as stack:
        yield stack


def _conversation(stack, project, mode="auto"):
    status, body = stack.api.post(f"/api/v1/projects/{project}/conversations",
                                  {"title": "", "mode": mode})
    assert status == 201, body
    return body["conversation"]["id"]


def _send(stack, project, conversation, text):
    return stack.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages", {"text": text})


def _runs(stack, project) -> list[dict]:
    return stack.api.get(f"/api/v1/projects/{project}/runs?include_hidden=1")[1]["runs"]


def _run_id_from(body) -> str:
    return next(m["meta"]["run_id"] for m in body["messages"] if m["meta"].get("run_id"))


# --------------------------------------------------------------------- 1/5
def test_identical_request_does_not_create_a_duplicate_run(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _conversation(studio, project)
    studio.answers(_plan("A"))

    text = "Реализуй функцию А"
    status1, body1 = _send(studio, project, conversation, text)
    assert status1 == 200, body1
    run_id = _run_id_from(body1)
    assert len(_runs(studio, project)) == 1

    # Same text again, as a client retry after a dropped response would send.
    status2, body2 = _send(studio, project, conversation, text)
    assert status2 == 200, body2
    assert len(_runs(studio, project)) == 1, "an identical retry created a second run"
    ids = [m["meta"].get("run_id") for m in body2["messages"] if m["meta"].get("run_id")]
    assert all(i == run_id for i in ids)


# --------------------------------------------------------------------- 2/5
def test_a_distinct_request_starts_an_independent_run_while_one_is_active(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _conversation(studio, project)
    studio.answers(_plan("A"))
    status1, body1 = _send(studio, project, conversation, "Реализуй функцию А")
    assert status1 == 200, body1
    run_a = _run_id_from(body1)
    runs = _runs(studio, project)
    assert len(runs) == 1
    assert next(r for r in runs if r["id"] == run_a)["state"] == "running"

    # A second, textually distinct order, sent while the first run is still
    # "running": this must succeed and start its own run, not be refused.
    studio.answers(_plan("B"))
    status2, body2 = _send(studio, project, conversation, "Реализуй функцию Б")
    assert status2 == 200, body2
    assert body2["messages"][-1]["meta"].get("mode") == "autonomous"
    assert body2["messages"][-1]["meta"].get("blocked_second_run") is not True
    run_b = _run_id_from(body2)
    assert run_b != run_a

    runs = _runs(studio, project)
    assert len(runs) == 2, "a distinct new order while a run was active did not start its own run"
    states = {r["id"]: r["state"] for r in runs}
    assert states[run_a] == "running"
    assert states[run_b] == "running"


# --------------------------------------------------------------------- 3/5
def test_continue_resumes_the_conversations_own_run_not_a_different_one(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _conversation(studio, project)
    studio.answers(_plan("A"))
    status1, body1 = _send(studio, project, conversation, "Реализуй функцию А")
    run_a = _run_id_from(body1)

    studio.answers(_plan("B"))
    status2, body2 = _send(studio, project, conversation, "Реализуй функцию Б")
    run_b = _run_id_from(body2)
    assert run_b != run_a

    # The conversation's own "current" run is the one it most recently
    # started/linked (run_b). Pause it through the real API first so
    # "Продолжи" has real work to resume rather than reporting "already
    # running" (a raw SQL state flip bypasses the bookkeeping a clean pause
    # does and leaves nothing retryable to resume).
    assert studio.api.post(f"/api/v1/projects/{project}/runs/{run_b}/pause", {})[0] == 200

    status3, body3 = _send(studio, project, conversation, "Продолжи")
    assert status3 == 200, body3
    last = body3["messages"][-1]
    assert last["meta"].get("run_id") == run_b, "continue resumed the wrong run"
    assert last["meta"].get("resumed") is True

    # run_a (not named by "Продолжи") is untouched: still its own state, no
    # new run was created by the continuation.
    runs = {r["id"]: r["state"] for r in _runs(studio, project)}
    assert len(runs) == 2
    assert runs[run_a] == "running"
    assert runs[run_b] == "running"


# --------------------------------------------------------------------- 4/5
def test_multiple_concurrent_runs_remain_individually_visible(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _conversation(studio, project)
    run_ids = []
    for label in ("A", "B", "C"):
        studio.answers(_plan(label))
        status, body = _send(studio, project, conversation, f"Реализуй функцию {label}")
        assert status == 200, body
        run_ids.append(_run_id_from(body))
    assert len(set(run_ids)) == 3, "three distinct orders did not produce three distinct runs"

    runs = _runs(studio, project)
    assert {r["id"] for r in runs} == set(run_ids)
    assert all(r["state"] == "running" for r in runs)

    # Each run is also individually fetchable by id, independent of the others.
    for run_id in run_ids:
        status, body = studio.api.get(f"/api/v1/projects/{project}/runs/{run_id}")
        assert status == 200, body
        assert body["run"]["id"] == run_id


# --------------------------------------------------------------------- 5/5
def test_cancelling_one_run_does_not_affect_an_independent_second_run(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _conversation(studio, project)
    studio.answers(_plan("A"))
    status1, body1 = _send(studio, project, conversation, "Реализуй функцию А")
    run_a = _run_id_from(body1)

    studio.answers(_plan("B"))
    status2, body2 = _send(studio, project, conversation, "Реализуй функцию Б")
    run_b = _run_id_from(body2)
    assert run_b != run_a

    status, body = studio.api.post(f"/api/v1/projects/{project}/runs/{run_a}/cancel", {})
    assert status == 200, body
    assert body["run"]["state"] == "cancelled"

    # run_b, never named by the cancel call, keeps working undisturbed.
    status, body = studio.api.get(f"/api/v1/projects/{project}/runs/{run_b}")
    assert status == 200, body
    assert body["run"]["state"] == "running"

    # A second cancel of the same run is a no-op, never an error, and still
    # never touches run_b.
    status, body = studio.api.post(f"/api/v1/projects/{project}/runs/{run_a}/cancel", {})
    assert status == 200, body
    assert body["run"]["state"] == "cancelled"
    status, body = studio.api.get(f"/api/v1/projects/{project}/runs/{run_b}")
    assert body["run"]["state"] == "running"

