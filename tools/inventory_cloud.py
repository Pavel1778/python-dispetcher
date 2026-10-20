"""Read-only inventory of OpenHands Cloud conversations.

Lists conversations with title, creation time, tags, sandbox and event counts,
so orphaned or duplicate sessions created by the dispatcher can be identified.
Nothing is deleted or modified here.

Usage:
    python3 tools/inventory_cloud.py [--limit N]

Prints a JSON report on stdout. Never prints the API key.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient, CloudError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="read-only Cloud conversation inventory")
    ap.add_argument("--limit", type=int, default=50, help="page size for search")
    ap.add_argument("--max-pages", type=int, default=20)
    args = ap.parse_args()

    client = CloudClient()
    rows: list[dict] = []
    page_id = None
    try:
        for _ in range(args.max_pages):
            items, page_id = client.search_conversations(
                limit=args.limit, page_id=page_id
            )
            for c in items:
                rows.append(
                    {
                        "id": c.id,
                        "title": c.title,
                        "created_at": c.raw.get("created_at"),
                        "execution_status": c.execution_status,
                        "sandbox_id": c.sandbox_id,
                        "sandbox_status": c.sandbox_status,
                        "selected_repository": c.selected_repository,
                        "selected_branch": c.selected_branch,
                        "tags": c.tags,
                        "sub_conversation_ids": c.sub_conversation_ids,
                    }
                )
            if not page_id:
                break
    except CloudError as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 2

    # Event counts are one extra call per conversation; only do it for a small
    # set, to stay read-only and cheap.
    if len(rows) <= 60:
        for row in rows:
            try:
                row["event_count"] = len(client.list_events(row["id"]))
            except CloudError as e:
                row["event_count"] = None
                row["event_error"] = type(e).__name__

    # Classify by the tags the dispatcher itself writes.
    for row in rows:
        tags = row.get("tags") or {}
        if "orchestratorsr" in tags:
            row["role"] = "Sr"
            row["owner_key"] = tags["orchestratorsr"]
        elif "orchestratortaskid" in tags:
            row["role"] = "Jr"
            row["owner_key"] = tags["orchestratortaskid"]
        else:
            row["role"] = "unknown"
            row["owner_key"] = None

    report = {
        "total": len(rows),
        "by_role": {
            r: sum(1 for x in rows if x["role"] == r)
            for r in ("Sr", "Jr", "unknown")
        },
        "conversations": rows,
    }
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
