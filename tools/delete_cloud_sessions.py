"""Delete specific OpenHands Cloud conversations, by explicit id only.

There is deliberately no pattern, tag or "delete all" mode. A mass delete of a
shared account is not something a script should be able to do from a typo, and
the account may hold conversations this tool knows nothing about. Every id must
be named on the command line, and nothing happens without ``--confirm``.

Safety properties:

* Refuses to run without ``--confirm``; prints the plan instead.
* Only deletes ids given with ``--id`` (or ``--ids-file``, one per line).
* Prints each id, its title and its tags before deleting, so the operator can
  see what is about to go.
* Never prints or logs the API key.

Usage:
    python3 tools/delete_cloud_sessions.py --id <id> [--id <id> ...] [--confirm]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient, CloudError  # noqa: E402


def _describe(client: CloudClient, conversation_id: str) -> dict:
    try:
        item = client.get_conversation(conversation_id)
    except CloudError as e:
        return {"id": conversation_id, "error": f"{type(e).__name__}: {e}"}
    return {
        "id": conversation_id,
        "title": item.title,
        "created_at": item.raw.get("created_at"),
        "execution_status": item.execution_status,
        "tags": item.tags,
        "sandbox_id": item.sandbox_id,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="delete named Cloud conversations")
    ap.add_argument("--id", action="append", default=[],
                    help="conversation id to delete (repeatable)")
    ap.add_argument("--ids-file", help="file with one conversation id per line")
    ap.add_argument("--confirm", action="store_true",
                    help="actually delete; without this only the plan is printed")
    args = ap.parse_args()

    ids = list(args.id)
    if args.ids_file:
        with open(args.ids_file, encoding="utf-8") as fh:
            ids += [line.strip() for line in fh
                    if line.strip() and not line.startswith("#")]
    # Deduplicate while keeping order, so a repeated --id is not deleted twice.
    seen: set[str] = set()
    ids = [i for i in ids if not (i in seen or seen.add(i))]

    if not ids:
        print(json.dumps({"error": "no conversation ids given; nothing to do"}))
        return 2

    client = CloudClient()
    plan = [_describe(client, cid) for cid in ids]
    print(json.dumps({"mode": "confirm" if args.confirm else "dry-run",
                      "count": len(plan), "targets": plan},
                     indent=2, default=str))

    if not args.confirm:
        print("\nDry run. Re-run with --confirm to delete the ids above.")
        return 0

    results = []
    for cid in ids:
        try:
            ok = client.delete_conversation(cid)
        except CloudError as e:
            results.append({"id": cid, "deleted": False,
                            "error": f"{type(e).__name__}: {e}"})
            continue
        results.append({"id": cid, "deleted": ok})
    print(json.dumps({"deleted": results}, indent=2, default=str))
    return 0 if all(r.get("deleted") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
