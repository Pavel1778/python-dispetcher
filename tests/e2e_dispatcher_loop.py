"""Full dispatcher loop against the real runtime.

Proves the whole chain end to end: a task submitted to SQLite is picked up by
the dispatcher, given to a real conversation, tracked, and — because this
environment has no working LLM key — correctly dead-lettered as a
non-retryable auth failure rather than retried forever.

Only the conversations this run creates are deleted at the end.
"""

import os
import sys
import urllib.request

sys.path.insert(0, "/workspace/platform")
from orchestration.control_plane import ControlPlane, TaskState  # noqa: E402
from orchestration.dispatcher import Dispatcher  # noqa: E402
from orchestration.openhands_client import OpenHandsClient  # noqa: E402

BASE = "http://127.0.0.1:60000"
KEY = os.environ["SESSION_API_KEY"]
DB = "/tmp/oh-phase3-e2e/cp.db"

cp = ControlPlane(DB)
client = OpenHandsClient(BASE, api_key=KEY, timeout=30.0)
d = Dispatcher(
    cp,
    client,
    worker_id="e2e-worker",
    workspace_root="/tmp/oh-phase3-e2e",
    lease_seconds=120.0,
)

cp.submit_task(
    "e2e-task-1",
    "Reply with exactly: PHASE3-E2E-OK",
    max_attempts=5,
)

print("--- tick 1 (dispatch) ---")
r1 = d.tick()
print("dispatched:", r1.dispatched)
t = cp.get_task("e2e-task-1")
print("state:", t.state.value, "| conversation:", t.conversation_id)
cid = t.conversation_id

print("--- ticks 2..12 (track) ---")
import time

for i in range(2, 13):
    time.sleep(6)
    r = d.tick()
    t = cp.get_task("e2e-task-1")
    print(
        f"tick {i}: state={t.state.value} attempt={t.attempt} "
        f"completed={r.completed} failed={r.failed} requeued={r.requeued}"
    )
    if t.state in (TaskState.COMPLETED, TaskState.DEAD_LETTER, TaskState.BLOCKED):
        break

final = cp.get_task("e2e-task-1")
print("FINAL state:", final.state.value, "| attempt:", final.attempt)
print("evidence:", final.evidence)

# Clean up only the conversation this run created.
if cid:
    req = urllib.request.Request(f"{BASE}/api/conversations/{cid}", method="DELETE")
    req.add_header("X-Session-API-Key", KEY)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print("cleanup conversation:", r.status)
    except Exception as e:  # noqa: BLE001
        print("cleanup conversation error:", type(e).__name__)

cp.close()
