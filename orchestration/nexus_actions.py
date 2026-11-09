"""Guarded actions that require explicit human confirmation.

Some operations must never happen because a model or a single click asked for
them: creating a public repository, or flipping an existing private repository
to public. This module implements a two-step handshake:

1. ``propose`` records an intent (who, what, the exact payload) and returns a
   one-time confirmation token. **Nothing is executed.**
2. ``confirm`` checks the token against its stored hash, marks the action
   confirmed and returns the payload so the caller can execute it.

The token is generated with :mod:`secrets` and only its SHA-256 is stored, so a
database read cannot yield a usable token. Proposals expire, and confirming an
already-decided action is refused rather than re-run. Nothing in this module
talks to GitHub; execution stays in the caller so the confirmation boundary is
obvious and testable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from .cloud_store import NotFound, PostgresStore
from .nexus_projects import _text

#: How long a proposal stays confirmable.
DEFAULT_TTL_SECONDS = 900


class ActionStateError(ValueError):
    """The action is not in a state that allows the requested transition."""


class ConfirmationMismatch(ValueError):
    """The supplied confirmation token does not match the proposal."""


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PendingActions:
    """Durable proposals awaiting a human decision."""

    def __init__(self, store: PostgresStore, *, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.store = store
        self.ttl_seconds = ttl_seconds

    def propose(self, actor: str, action: Any, payload: Any, *,
                project_id: str | None = None) -> dict:
        action = _text(action, "action", maximum=64)
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)
        with self.store._tx() as cur:
            cur.execute(
                "INSERT INTO nexus_pending_actions(project_id, actor, action, payload, "
                "token_hash, expires_at) VALUES (%s,%s,%s,%s,%s,%s) "
                "RETURNING id, action, payload, status, created_at, expires_at",
                (project_id, actor, action, json.dumps(payload), _hash_token(token), expires),
            )
            row = dict(cur.fetchone())
        row["id"] = str(row["id"])
        # The token is returned exactly once, here. It is never stored in clear.
        row["confirmation_token"] = token
        return row

    def _load(self, cur: Any, actor: str, action_id: str) -> dict:
        cur.execute(
            "SELECT id, project_id, actor, action, payload, token_hash, status, "
            "result, created_at, expires_at FROM nexus_pending_actions "
            "WHERE id=%s AND actor=%s FOR UPDATE",
            (action_id, actor),
        )
        row = cur.fetchone()
        if row is None:
            raise NotFound("action not found")
        return dict(row)

    @staticmethod
    def _expire_if_due(row: dict) -> bool:
        expires = row["expires_at"]
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires <= datetime.now(timezone.utc)

    def confirm(self, actor: str, action_id: str, token: str) -> dict:
        if not token:
            raise ConfirmationMismatch("confirmation token is required")
        with self.store._tx() as cur:
            row = self._load(cur, actor, action_id)
            if row["status"] == "confirmed":
                raise ActionStateError("action is already confirmed")
            if row["status"] != "pending":
                raise ActionStateError(f"action is {row['status']}")
            if self._expire_if_due(row):
                cur.execute("UPDATE nexus_pending_actions SET status='expired', "
                            "decided_at=now() WHERE id=%s", (action_id,))
                raise ActionStateError("action has expired")
            if not hmac.compare_digest(row["token_hash"], _hash_token(token)):
                raise ConfirmationMismatch("confirmation token does not match")
            cur.execute(
                "UPDATE nexus_pending_actions SET status='confirmed', decided_at=now() "
                "WHERE id=%s RETURNING id, action, payload, status, decided_at",
                (action_id,),
            )
            decided = dict(cur.fetchone())
        decided["id"] = str(decided["id"])
        return decided

    def cancel(self, actor: str, action_id: str) -> dict:
        with self.store._tx() as cur:
            row = self._load(cur, actor, action_id)
            if row["status"] != "pending":
                raise ActionStateError(f"action is {row['status']}")
            cur.execute("UPDATE nexus_pending_actions SET status='cancelled', "
                        "decided_at=now() WHERE id=%s RETURNING id, status, decided_at",
                        (action_id,))
            out = dict(cur.fetchone())
        out["id"] = str(out["id"])
        return out

    def record_result(self, actor: str, action_id: str, result: Any) -> dict:
        with self.store._tx() as cur:
            row = self._load(cur, actor, action_id)
            if row["status"] != "confirmed":
                raise ActionStateError("action must be confirmed before a result is recorded")
            cur.execute("UPDATE nexus_pending_actions SET result=%s WHERE id=%s "
                        "RETURNING id, status, result",
                        (json.dumps(result), action_id))
            out = dict(cur.fetchone())
        out["id"] = str(out["id"])
        return out

    def get(self, actor: str, action_id: str) -> dict:
        with self.store._tx() as cur:
            row = self._load(cur, actor, action_id)
        row.pop("token_hash", None)
        row["id"] = str(row["id"])
        return row

    def list_pending(self, actor: str) -> list[dict]:
        with self.store._tx() as cur:
            cur.execute(
                "SELECT id, project_id, action, payload, status, created_at, expires_at "
                "FROM nexus_pending_actions WHERE actor=%s AND status='pending' "
                "ORDER BY created_at DESC, id LIMIT 200",
                (actor,),
            )
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r["id"] = str(r["id"])
            if r.get("project_id") is not None:
                r["project_id"] = str(r["project_id"])
        return rows
