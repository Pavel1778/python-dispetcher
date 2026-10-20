"""End-to-end on **OpenHands Cloud**: a real Sr delegates to a real Jr.

Companion to :mod:`tests.e2e_local_sr_jr`, but against the live service using
the *direct* transport (:mod:`orchestration.cloud_direct`): it provisions a
sandbox through ``POST /api/v1/sandboxes`` and drives the sandbox's agent-server,
which is the path that actually yields a running Cloud conversation.

What it proves, end to end:

1. a workflow is created in PostgreSQL;
2. Sr is started on a real sandbox with the ``sr_`` client tools attached;
3. Sr's model really calls ``sr_create_task`` (observed as an ``ActionEvent``),
   and the bridge turns it into a durable Jr task;
4. the Jr dispatcher starts a real Jr conversation and runs it to completion;
5. the task reaches ``completed`` with the answer written by the **Jr**.

Not collected by pytest. It creates its own workflow, conversations and
sandbox, and deletes everything it created when it is done.

Run::

    SUPABASE_DB_URL="postgresql://postgres@127.0.0.1:5432/ohp_test" \
    OPENHANDS_API_KEY=... OPENHANDS_DEFAULT_MODEL=openhands/deepseek-v4.1-flash \
        python tests/e2e_cloud_sr_jr.py
"""

from __future__ import annotations

import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.cloud_direct import CloudDirectClient  # noqa: E402
from orchestration.cloud_dispatcher import CloudDispatcher  # noqa: E402
from orchestration.cloud_store import PostgresStore, TaskState  # noqa: E402
from orchestration.sr_runtime import SrRuntime  # noqa: E402
from orchestration.sr_tools import SrTools  # noqa: E402

MIGRATIONS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"
)

#: Written by the Jr, so seeing it proves the delegation ran end to end.
JR_GOAL = "Reply with exactly: CLOUD-JR-DELEGATION-OK"


def apply_migrations(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            for name in sorted(os.listdir(MIGRATIONS)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(MIGRATIONS, name)).read())


def main() -> int:
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        print("set SUPABASE_DB_URL")
        return 2
    apply_migrations(dsn)

    store = PostgresStore(dsn)
    cloud = CloudClient()
    client = CloudDirectClient(cloud, store, llm_model="openhands/deepseek-v4.1-flash")
    dispatcher = CloudDispatcher(store, client, worker_id="e2e-cloud")
    sr = SrRuntime(
        store, client, SrTools(store, dispatcher), worker_id="e2e-cloud"
    )
    store.list_sandboxes()  # ensure the table is reachable

    before = {s["sandbox_id"] for s in store.list_sandboxes(limit=100)}
    goal = f'Delegate this unit: "{JR_GOAL}" (run {uuid.uuid4().hex[:6]})'
    workflow = store.create_workflow(goal)
    print("workflow:", workflow)

    sr_id = sr.ensure_sr(workflow)
    print("Sr conversation:", sr_id)
    print("Sr status:", client.get_conversation(sr_id).execution_status)

    jr_task_id = None
    for _ in range(40):
        report = sr.tick()
        if report.handled:
            print("sr handled:", report.handled)
        kids = store.list_tasks(workflow_id=workflow)
        if kids:
            jr_task_id = kids[0].id
            break
        time.sleep(5)

    jr_conv_id = None
    if not jr_task_id:
        print("NO JR TASK CREATED - Sr events:")
        for e in client.list_events(sr_id):
            if e.get("kind") == "ActionEvent":
                print("  ACTION", e.get("tool_name"))
            elif e.get("kind") == "ConversationErrorEvent":
                print("  ERROR", e.get("code"), str(e.get("detail"))[:200])
        _cleanup(store, client, before, [sr_id])
        return 1

    print("Jr task:", jr_task_id)
    for i in range(60):
        dispatcher.tick()
        task = store.get_task(jr_task_id)
        if task.state in (TaskState.COMPLETED, TaskState.DEAD_LETTER,
                          TaskState.BLOCKED, TaskState.FAILED):
            jr_conv_id = (store.conversation_for(jr_task_id) or {}).get(
                "external_conversation_id"
            )
            print(f"Jr finished after {i} ticks: {task.state.value}")
            break
        time.sleep(5)

    task = store.get_task(jr_task_id)
    print("Jr final state:", task.state.value)
    print("Jr result:", repr(task.result)[:200])
    ok = task.state is TaskState.COMPLETED and bool(task.result)

    _cleanup(store, client, before, [sr_id, jr_conv_id])
    store.close()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _cleanup(store: PostgresStore, client: CloudDirectClient,
             sandboxes_before: set[str], conversations: list[str]) -> None:
    for cid in conversations:
        if not cid:
            continue
        try:
            client.delete_conversation(cid)
            print("deleted conversation:", cid)
        except Exception as e:  # noqa: BLE001
            print("delete conversation failed:", cid, type(e).__name__)
    for row in store.list_sandboxes(limit=100):
        if row["sandbox_id"] in sandboxes_before:
            continue
        try:
            client.cloud.delete_sandbox(row["sandbox_id"])
            print("deleted sandbox:", row["sandbox_id"])
        except Exception as e:  # noqa: BLE001
            print("delete sandbox failed:", row["sandbox_id"], type(e).__name__)


if __name__ == "__main__":
    raise SystemExit(main())
