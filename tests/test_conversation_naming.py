"""A conversation must end up named, because create cannot name it.

Neither API accepts a name at **creation** time, verified against both live
servers:

* the local agent-server's ``StartConversationRequest`` has no ``title`` field,
  so ``POST /api/conversations`` answers ``201`` and the conversation stays
  untitled;
* OpenHands Cloud accepts ``title`` but the conversation that appears carries an
  auto-generated name derived from the first user message (``autotitle``
  defaults to true), so a Sr conversation was named
  ``"You are the senior (Sr) agent for this workflow..."``.

The name therefore has to be applied with ``PATCH`` once the conversation
exists. These tests fail if that step is dropped, and they also guard the
test doubles: a fake that honoured ``title`` on create would make the defect
invisible again, which is exactly how it survived this long.

They run the real dispatcher, the real ``SrRuntime``, real SQL against
PostgreSQL and a real HTTP stand-in for Cloud.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient, CloudUnavailable  # noqa: E402
from orchestration.cloud_dispatcher import CloudDispatcher  # noqa: E402
from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.naming import conversation_title  # noqa: E402
from orchestration.sr_runtime import SrRuntime  # noqa: E402
from orchestration.sr_tools import SrTools  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run naming tests"
)

_MIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"
)


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DROP TABLE IF EXISTS sr_tool_calls, task_events, conversations, "
                "tasks, repositories, workflows CASCADE"
            )
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


@pytest.fixture(autouse=True)
def _schema():
    _reset_schema()
    yield


@pytest.fixture
def store():
    s = PostgresStore(DSN)
    yield s
    s.close()


@pytest.fixture
def cloud():
    with FakeCloudServer() as server:
        yield server


@pytest.fixture
def client(cloud):
    return CloudClient(cloud.base_url, api_key="test-key", timeout=5.0)


@pytest.fixture
def dispatcher(store, client):
    return CloudDispatcher(store, client, worker_id="w1", start_poll_interval=0.01)


@pytest.fixture
def runtime(store, client, dispatcher):
    tools = SrTools(store, dispatcher)
    return SrRuntime(store, client, tools, start_poll_interval=0.01)


def _title(cloud, cid):
    return cloud.cloud.conversations[cid]["title"]


# ------------------------------------------------------------------- helper
def test_title_is_readable_and_carries_a_reference():
    title = conversation_title("Sr", "audit the parser", ref="fede8a2f-1234-5678")
    assert title == "Sr.audit the parser [fede8a2f]"
    # The reference is what tells two runs of the same goal apart.
    other = conversation_title("Sr", "audit the parser", ref="a53d3017-9999")
    assert title != other


def test_title_collapses_newlines_and_bounds_length():
    title = conversation_title("Jr", "line one\nline two", ref="x" * 40)
    assert "\n" not in title
    assert "line one line two" in title
    assert len(title) <= 100


def test_empty_goal_still_produces_a_usable_name():
    assert conversation_title("Jr", "   ", ref="abcdef12") == "Jr.workflow [abcdef12]"


# --------------------------------------------------------- the defect itself
def test_create_ignores_title_so_patch_is_required(cloud, client):
    """Pin the API behaviour the fix depends on.

    ``start_conversation`` still accepts a ``title`` (callers may pass one), but
    the conversation that comes back must **not** carry it: the live server
    derives its own name from the first message. If a future stand-in starts
    honouring it, this fails and points at why the real defect could hide again.
    """
    start = client.start_conversation(
        initial_message="hello", title="НЕ ДОЛЖНО ПОПАСТЬ"
    )
    current = start
    for _ in range(50):
        if current.ready:
            break
        assert not current.failed, current.detail
        time.sleep(0.02)
        current = client.get_start_task(current.id)
    cid = current.app_conversation_id
    assert cid
    assert _title(cloud, cid) != "НЕ ДОЛЖНО ПОПАСТЬ"
    assert _title(cloud, cid).startswith("Conversation ")


def test_sr_conversation_is_named_after_it_exists(store, cloud, runtime):
    wf = store.create_workflow("build a thing")
    cid = runtime.ensure_sr(wf)
    assert _title(cloud, cid) == f"Sr.build a thing [{wf.replace('-', '')[:8]}]"


def test_jr_conversation_is_named_after_it_exists(store, cloud, dispatcher):
    wf = store.create_workflow("wf")
    task = store.submit_task("implement the parser", workflow_id=wf)
    dispatcher.tick()
    cid = store.conversation_for(task.id)["external_conversation_id"]
    assert _title(cloud, cid) == (
        f"Jr.implement the parser [{task.id.replace('-', '')[:8]}]"
    )


def test_two_tasks_with_one_goal_are_distinguishable(store, cloud, dispatcher):
    """The reported confusion: identical goals produced identical names."""
    wf = store.create_workflow("wf")
    first = store.submit_task("same goal", workflow_id=wf)
    second = store.submit_task("same goal", workflow_id=wf)
    dispatcher.tick()

    one = store.conversation_for(first.id)["external_conversation_id"]
    two = store.conversation_for(second.id)["external_conversation_id"]
    assert one != two
    assert _title(cloud, one) != _title(cloud, two)


def test_naming_failure_does_not_fail_the_work(store, cloud, client, dispatcher):
    """A rename that errors must not cost the conversation.

    The work is real and already started; losing a name is cosmetic. The task
    must still reach RUNNING.
    """
    wf = store.create_workflow("wf")
    task = store.submit_task("do the work", workflow_id=wf)

    real = client.update_conversation

    def boom(*args, **kwargs):
        raise CloudUnavailable("rename endpoint exploded")

    client.update_conversation = boom  # type: ignore[assignment]
    try:
        report = dispatcher.tick()
    finally:
        client.update_conversation = real  # type: ignore[assignment]

    conv = store.conversation_for(task.id)
    assert conv["external_conversation_id"] in cloud.cloud.conversations
    assert task.id in report.started[0]["task"]
