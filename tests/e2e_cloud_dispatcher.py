"""Manual end-to-end against real OpenHands Cloud.

Not collected by pytest (``python_files = test_*.py``). It creates one real
conversation, drives it through the dispatcher, verifies the Cloud status, then
deletes the conversation and cancels the task. It never touches other
conversations.

Run:

    SUPABASE_DB_URL="postgresql://..." OPENHANDS_API_KEY="..." \
    /workspace/project/4119041ca5254315a7b6e95fdc85ae8a/scratch/sdk-main/.venv/bin/python \
      tests/e2e_cloud_dispatcher.py

Requires a reachable PostgreSQL and a valid Cloud key.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.cloud_dispatcher import CloudDispatcher  # noqa: E402
from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.sr_tools import SrTools  # noqa: E402

MIGRATION = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "migrations", "0001_init.sql",
)


def main() -> int:
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        print("set SUPABASE_DB_URL")
        return 2

    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(open(MIGRATION).read())

    store = PostgresStore(dsn)
    client = CloudClient()
    if not client.health():
        print("cloud not reachable")
        return 2

    dispatcher = CloudDispatcher(
        store, client, worker_id="e2e", start_poll_interval=3.0,
        start_poll_attempts=40,
    )
    tools = SrTools(store, dispatcher)

    workflow = store.create_workflow("e2e cloud run")
    task_id = tools.create_task(
        "Reply with exactly: ORCH-E2E-OK", workflow_id=workflow, max_attempts=2
    ).data["task_id"]
    print("task:", task_id)

    report = dispatcher.tick()
    print("tick:", {k: v for k, v in report.as_dict().items() if v})

    task = store.get_task(task_id)
    conv = store.conversation_for(task_id)
    print("state:", task.state.value, "conversation:", conv and conv["external_conversation_id"])

    if conv and conv.get("external_conversation_id"):
        view = client.get_conversation(conv["external_conversation_id"])
        print("cloud execution_status:", view.execution_status, "|", view.title)
        # Give the agent a moment, then check the answer path.
        for _ in range(10):
            view = client.get_conversation(conv["external_conversation_id"])
            if view.execution_status == "finished":
                break
            time.sleep(3)
        dispatcher.tick()
        print("after advance:", store.get_task(task_id).state.value)

    # Clean up: delete the conversation we created, then cancel the task.
    if conv and conv.get("external_conversation_id"):
        print("delete conversation:", client.delete_conversation(conv["external_conversation_id"]))
    tools.cancel_task(task_id)
    store.close()

    items, _ = client.search_conversations(limit=50)
    print("cloud conversations remaining:", len(items))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
