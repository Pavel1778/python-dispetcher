"""Bounded end-to-end check against the real runtime (v1.50.1).

Creates ONE dedicated test conversation with a client-supplied UUID in an
isolated /tmp workspace, sends a trivial task, polls briefly, then deletes only
that conversation. The three existing working conversations are never touched.
"""

import os
import sys
import time
import uuid

sys.path.insert(0, "/workspace/platform")
from orchestration.openhands_client import (  # noqa: E402
    OpenHandsClient,
    Outcome,
    task_conversation_id,
)

BASE = "http://127.0.0.1:60000"
KEY = os.environ["SESSION_API_KEY"]
client = OpenHandsClient(BASE, api_key=KEY, timeout=30.0)

print("health:", client.health())

test_task = f"phase3-e2e-{uuid.uuid4().hex[:8]}"
cid = task_conversation_id(test_task, namespace="openhands-orchestrator-e2e")
workdir = f"/tmp/oh-phase3-e2e/{cid}"
os.makedirs(workdir, exist_ok=True)
print("test conversation id:", cid)
print("test workspace:", workdir)

view, created = client.create_conversation(
    conversation_id=cid,
    working_dir=workdir,
    title=f"phase3-e2e {test_task}",
    max_iterations=8,
    agent_settings=client.agent_settings(),
)
print("created:", created, "| status:", view.raw_status)

view2, created2 = client.create_conversation(
    conversation_id=cid,
    working_dir=workdir,
    title="dup-check",
    max_iterations=8,
    agent_settings=client.agent_settings(),
)
print("re-create created:", created2, "(must be False)")
print("IDEMPOTENT:", created2 is False and view2.conversation_id == cid)

try:
    out = client.send_task(cid, "Reply with exactly: PHASE3-E2E-OK", run=True)
    print("send_task started:", out.started)
except Exception as e:  # noqa: BLE001
    print("send_task error:", type(e).__name__, e)

final = None
for i in range(24):
    time.sleep(5)
    try:
        v = client.get_conversation(cid)
    except Exception as e:  # noqa: BLE001
        print(f"[{i}] poll error:", type(e).__name__)
        continue
    print(f"[{i}] status={v.raw_status}")
    if v.outcome is Outcome.FINISHED:
        final = client.final_response(cid)
        print("FINAL RESPONSE:", repr(final)[:300])
        break
    if v.outcome is Outcome.FAILED:
        print("conversation failed")
        break

print("events:", client.event_count(cid))
print("RESULT final_present:", final is not None)

try:
    import urllib.request

    req = urllib.request.Request(f"{BASE}/api/conversations/{cid}", method="DELETE")
    req.add_header("X-Session-API-Key", KEY)
    with urllib.request.urlopen(req, timeout=30) as r:
        print("cleanup delete status:", r.status)
except Exception as e:  # noqa: BLE001
    print("cleanup error:", type(e).__name__, e)
