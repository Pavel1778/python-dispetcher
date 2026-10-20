"""End-to-end: a real Sr delegates to a real Jr on the local agent-server.

Not collected by pytest (``python_files = test_*.py``). It exercises the whole
mechanism against a live LLM agent, not a fake:

1. a workflow is created in PostgreSQL;
2. Sr is started on the runtime with the ``sr_`` client tools attached;
3. Sr's model really calls ``sr_create_task`` (observed as a real
   ``ActionEvent``), and the bridge turns it into a durable Jr task;
4. the Jr dispatcher starts a real Jr conversation for that task and runs it to
   completion;
5. the task reaches ``completed`` with a non-empty answer.

It creates only its own workflow, conversations and tasks, and deletes the
conversations it created when it is done.

Run::

    SUPABASE_DB_URL="postgresql://postgres@127.0.0.1:5432/ohp_test" \
        python tests/e2e_local_sr_jr.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore, TaskState  # noqa: E402
from orchestration.local_runtime import build_local_stack  # noqa: E402

MIGRATIONS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"
)

#: A goal whose expected answer is written by the *Jr*, so seeing it proves the
#: delegation actually ran end-to-end rather than Sr answering on its own.
JR_GOAL = "Reply with exactly: JR-DELEGATION-OK"
SR_PROMPT = (
    "You are the senior (Sr) agent for this workflow. Delegate every unit of "
    "work to junior (Jr) tasks using the provided tools; do not attempt the work "
    "yourself. Call sr_create_task for each unit, then stop. The result is "
    "delivered back to you as a message.\n\nWorkflow goal:\n{goal}"
)


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
    client, dispatcher, sr = build_local_stack(store, worker_id="e2e-local")
    created: list[str] = []

    goal = f'Delegate this unit: "{JR_GOAL}" (run {uuid.uuid4().hex[:6]})'
    workflow = store.create_workflow(goal)
    print("workflow:", workflow)

    # Start Sr through the shared runtime: this attaches the sr_ client tools,
    # generates the delegating instruction and binds the conversation durably.
    sr_id = sr.ensure_sr(workflow)
    created.append(sr_id)
    print("Sr conversation:", sr_id)
    print("Sr status:", client.get_conversation(sr_id).execution_status)

    # Let Sr's model call sr_create_task; the bridge executes it.
    jr_task_id = None
    for i in range(40):
        report = sr.tick()
        if report.handled:
            print("sr handled:", report.handled)
        kids = store.list_tasks(workflow_id=workflow)
        if kids:
            jr_task_id = kids[0].id
            break
        if i % 5 == 0:
            print(f"  [{i}] sr={client.get_conversation(sr_id).execution_status}")
        time.sleep(3)

    if not jr_task_id:
        print("NO JR TASK CREATED - Sr events:")
        for e in client.list_events(sr_id):
            kind = e.get("kind")
            if kind == "ActionEvent":
                print("  ACTION", e.get("tool_name"), json.dumps(e.get("action"))[:200])
            elif kind == "MessageEvent":
                content = (e.get("llm_message") or {}).get("content") or []
                texts = [c.get("text", "") for c in content if isinstance(c, dict)]
                if any(texts):
                    print("  MSG", e.get("source"), " ".join(texts)[:200])
            elif "Error" in kind or "error" in str(e.get("key", "")):
                print("  ", kind, json.dumps(e, default=str)[:300])
        print("  events count:", client.event_count(sr_id))
        print("  final response:", repr(client.final_response(sr_id))[:400])
        _cleanup(store, client, created, [])
        return 1

    print("Jr task:", jr_task_id)

    # Run the Jr to completion.
    jr_conv_id = None
    for i in range(40):
        dispatcher.tick()
        task = store.get_task(jr_task_id)
        if task.state in (TaskState.COMPLETED, TaskState.DEAD_LETTER,
                          TaskState.BLOCKED, TaskState.FAILED):
            jr_conv_id = (store.conversation_for(jr_task_id) or {}).get(
                "external_conversation_id"
            )
            print(f"Jr finished after {i} ticks: {task.state.value}")
            break
        time.sleep(3)

    task = store.get_task(jr_task_id)
    print("Jr final state:", task.state.value)
    print("Jr result:", repr(task.result)[:200])
    ok = task.state is TaskState.COMPLETED and bool(task.result)

    _cleanup(store, client, created, [jr_conv_id] if jr_conv_id else [])
    store.close()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _cleanup(store: PostgresStore, client, conversations: list[str],
             extra: list[str]) -> None:
    for cid in [*conversations, *extra]:
        if not cid:
            continue
        try:
            client.delete_conversation(cid)
            print("deleted conversation:", cid)
        except Exception as e:  # noqa: BLE001
            print("delete failed:", cid, type(e).__name__)


if __name__ == "__main__":
    raise SystemExit(main())
