"""Classify each live Cloud conversation as user-owned or dispatcher-created.

Read-only. Produces the evidence needed to decide what may be deleted, without
deleting anything.

A conversation is dispatcher-created when it carries the tags the dispatcher
writes (``orchestratorsr`` / ``orchestratortaskid``), or when its own event
stream shows a dispatcher persona (the Sr instruction) -- which is what an
untagged Sr would look like. Everything else is treated as the user's and is
never a deletion candidate.

Usage:
    python3 tools/classify_cloud_sessions.py [--out FILE]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient, CloudError  # noqa: E402

SR_MARK = "You are the senior (Sr) agent for this workflow"
TASK_MARK = "You are a junior (Jr) worker"


def _first_user_text(events: list[dict]) -> str:
    for e in events:
        if e.get("kind") != "MessageEvent":
            continue
        msg = e.get("llm_message") or {}
        if msg.get("role") != "user":
            continue
        return "".join(
            c.get("text", "") for c in (msg.get("content") or [])
            if isinstance(c, dict)
        )
    return ""


def _goals(events: list[dict]) -> list[str]:
    goals = []
    for e in events:
        if e.get("kind") != "ActionEvent":
            continue
        if e.get("tool_name") != "sr_create_task":
            continue
        action = e.get("action") or {}
        if isinstance(action, dict) and action.get("goal"):
            goals.append(action["goal"])
    return goals


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/classify.json")
    args = ap.parse_args()

    client = CloudClient()
    items, _ = client.search_conversations(limit=100)
    rows = []
    for c in items:
        events = []
        try:
            events = client.list_events(c.id)
        except CloudError:
            pass
        tags = c.tags or {}
        first = _first_user_text(events)
        tagged = "orchestratorsr" in tags or "orchestratortaskid" in tags
        persona = SR_MARK in first or TASK_MARK in first
        rows.append({
            "id": c.id,
            "title": c.title,
            "created_at": c.raw.get("created_at"),
            "status": c.execution_status,
            "event_count": len(events),
            "tags": tags,
            "tagged_by_dispatcher": tagged,
            "dispatcher_persona_in_events": persona,
            "dispatcher_created": tagged or persona,
            "role": ("Sr" if "orchestratorsr" in tags
                     else "Jr" if "orchestratortaskid" in tags
                     else "untagged"),
            "owner_key": tags.get("orchestratorsr") or tags.get("orchestratortaskid"),
            "jr_goals_created": _goals(events),
            "first_user_text": first[:300],
        })

    rows.sort(key=lambda r: str(r["created_at"]))
    report = {
        "total": len(rows),
        "dispatcher_created": sum(1 for r in rows if r["dispatcher_created"]),
        "user_owned": sum(1 for r in rows if not r["dispatcher_created"]),
        "conversations": rows,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)

    for r in rows:
        flag = "DISPATCHER" if r["dispatcher_created"] else "user      "
        print(f"{flag} {r['created_at']} {r['role']:8} {r['id']} "
              f"ev={r['event_count']:5} {str(r['title'])[:50]!r}")
    print(f"\ntotal={report['total']} dispatcher={report['dispatcher_created']} "
          f"user={report['user_owned']} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
