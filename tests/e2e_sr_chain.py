"""Manual end-to-end Sr -> client tool -> real Jr, against live OpenHands Cloud.

Not collected by pytest (``python_files = test_*.py``). It creates its own
temporary Sr conversation, waits for the live agent to call ``create_task``,
runs the bridge, then the Jr dispatcher, and finally deletes the conversations
it created and cancels its tasks. It never touches other conversations.

Run:

    SUPABASE_DB_URL="postgresql://..." OPENHANDS_API_KEY="..." \
    /workspace/project/4119041ca5254315a7b6e95fdc85ae8a/scratch/sdk-main/.venv/bin/python \
      tests/e2e_sr_chain.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.cloud_dispatcher import CloudDispatcher  # noqa: E402
from orchestration.cloud_store import PostgresStore, TaskState  # noqa: E402
from orchestration.sr_runtime import SrRuntime  # noqa: E402
from orchestration.sr_tools import SrTools  # noqa: E402

MIGRATIONS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"
)

PROMPT = (
    "You are the Sr agent. Delegate the single unit of work below by calling the "
    "sr_create_task tool exactly once, then stop.\n"
    "Unit: 'Reply with exactly: JR-UNIT-OK'.\n"
    "Call sr_create_task with goal='Reply with exactly: JR-UNIT-OK' and no repository."
)


def main() -> int:
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        print("set SUPABASE_DB_URL")
        return 2

    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(open(os.path.join(MIGRATIONS, "0001_init.sql")).read())
            cur.execute(open(os.path.join(MIGRATIONS, "0002_sr_runtime.sql")).read())
            cur.execute(open(os.path.join(MIGRATIONS, "0003_tool_call_recovery.sql")).read())

    store = PostgresStore(dsn)
    client = CloudClient()
    dispatcher = CloudDispatcher(
        store, client, worker_id="e2e-sr", start_poll_interval=3.0,
        start_poll_attempts=40,
    )
    runtime = SrRuntime(
        store, client, SrTools(store, dispatcher), start_poll_interval=3.0,
        start_poll_attempts=40,
    )

    workflow = store.create_workflow("E2E Sr->Jr chain")

    # Start Sr with the real client tools attached, using our own prompt.
    start = client.start_conversation(
        initial_message=PROMPT,
        title="Sr.E2E chain (delete me)",
        tags={"orchestratorsr": workflow.replace("-", "")},
        client_tools=SrTools.tool_schemas(),
    )
    store.bind_sr(workflow, start_task_id=start.id, status="starting")
    cid = runtime._await_start(start)
    store.bind_sr(workflow, conversation_id=cid, status="running")
    print("Sr conversation:", cid)

    created: list[str] = []
    jr_task_id = None
    for _ in range(60):
        report = runtime.tick()
        if report.handled:
            print("handled:", report.handled)
            kids = store.list_tasks(workflow_id=workflow)
            if kids:
                jr_task_id = kids[0].id
                break
        time.sleep(3)

    if not jr_task_id:
        print("NO JR CREATED - dumping Sr events")
        for e in client.list_events(cid, limit=100):
            if e.get("kind") in ("ActionEvent", "MessageEvent"):
                print(" ", e.get("kind"), e.get("tool_name"),
                      (e.get("action") or {}).get("kind"))
        client.delete_conversation(cid)
        store.close()
        return 1

    print("Jr task:", jr_task_id)
    # Dispatch the Jr and let it run to completion.
    dispatcher.tick()
    jr_conv = store.conversation_for(jr_task_id)
    print("Jr conversation:", jr_conv and jr_conv["external_conversation_id"])
    if jr_conv and jr_conv.get("external_conversation_id"):
        created.append(jr_conv["external_conversation_id"])
        for _ in range(30):
            dispatcher.tick()
            st = store.get_task(jr_task_id).state
            if st in (TaskState.COMPLETED, TaskState.DEAD_LETTER,
                      TaskState.BLOCKED, TaskState.FAILED):
                break
            time.sleep(3)
    print("Jr final state:", store.get_task(jr_task_id).state.value)
    print("Jr result:", (store.get_task(jr_task_id).result or "")[:120])

    # Cleanup: only the conversations this run created.
    for c in [cid, *created]:
        try:
            print("delete:", c, client.delete_conversation(c))
        except Exception as e:  # noqa: BLE001
            print("delete failed:", c, e)
    for t in store.list_tasks(workflow_id=workflow):
        try:
            SrTools(store, dispatcher).cancel_task(t.id)
        except Exception:  # noqa: BLE001
            pass
    store.close()

    items, _ = client.search_conversations(limit=50)
    print("cloud conversations remaining:", len(items))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
