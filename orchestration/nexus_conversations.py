"""Persistent conversations: history, messages, attachments and agent sessions.

A conversation is scoped to one project, so it inherits that project's bound
repository and branch. Every query authorizes by membership: a non-member gets
"not found", never a window into another project's chat.

The chat is real, not simulated. A message is stored, then the assistant turn is
produced by :mod:`orchestration.nexus_chat` and appended as its own message; a
tool event is a message with ``role='tool'`` so the UI can render it apart from
the assistant's prose. Nothing here fabricates a response: if the assistant
cannot answer, the failure is recorded as an ``error`` message and the
conversation state becomes ``error``.
"""
from __future__ import annotations

from typing import Any

from .cloud_store import NotFound, PostgresStore
from .nexus_projects import _text
from .nexus_specs import MESSAGE_CHARS

#: The modes a conversation may be set to. ``auto`` is the default: the Intent
#: Router decides per message (see ``orchestration/nexus_intent.py``), so a
#: question in an ``auto`` conversation is answered instead of starting a run.
_MODES = ("ask", "analysis", "plan", "code", "autonomous", "auto")


class ConversationService:
    """Create, list, search, rename, archive and read conversations."""

    def __init__(self, store: PostgresStore):
        self.store = store

    # ------------------------------------------------------------- lifecycle
    def create(self, principal: str, project_id: str, *,
               title: str = "", mode: str = "auto") -> dict:
        mode = self._mode(mode)
        title = _text(title, "title", maximum=200, required=False)
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute(
                "INSERT INTO nexus_conversations(project_id, title, mode, created_by) "
                "VALUES (%s,%s,%s,%s) "
                "RETURNING id, project_id, title, mode, state, archived, run_id, "
                "  created_at, updated_at",
                (project_id, title, mode, principal))
            row = cur.fetchone()
            self._event(cur, project_id, principal, "conversation.created", row["id"])
            return dict(row)

    def list(self, principal: str, project_id: str, *,
             include_archived: bool = False, query: str | None = None) -> list[dict]:
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            sql = ("SELECT id, project_id, title, mode, state, archived, run_id, "
                   "created_at, updated_at FROM nexus_conversations "
                   "WHERE project_id=%s")
            params: list[Any] = [project_id]
            if not include_archived:
                sql += " AND archived=false"
            if query:
                sql += " AND lower(title) LIKE %s"
                params.append("%" + query.strip().lower() + "%")
            sql += " ORDER BY updated_at DESC, id LIMIT 200"
            cur.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    def get(self, principal: str, project_id: str, conversation_id: str) -> dict:
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute(
                "SELECT c.id, c.project_id, c.title, c.mode, c.state, c.archived, "
                "  c.run_id, c.created_at, c.updated_at, "
                "  p.repository_id, p.selected_branch, r.full_name AS repository "
                "FROM nexus_conversations c "
                "JOIN nexus_projects p ON p.id = c.project_id "
                "LEFT JOIN repositories r ON r.id = p.repository_id "
                "WHERE c.id=%s AND c.project_id=%s",
                (conversation_id, project_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("conversation not found")
            return dict(row)

    def rename(self, principal: str, project_id: str, conversation_id: str,
               title: str) -> dict:
        title = _text(title, "title", maximum=200)
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute(
                "UPDATE nexus_conversations SET title=%s, updated_at=now() "
                "WHERE id=%s AND project_id=%s "
                "RETURNING id, project_id, title, mode, state, archived, run_id, "
                "  created_at, updated_at",
                (title, conversation_id, project_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("conversation not found")
            return dict(row)

    def set_archived(self, principal: str, project_id: str, conversation_id: str,
                     archived: bool) -> dict:
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute(
                "UPDATE nexus_conversations SET archived=%s, updated_at=now() "
                "WHERE id=%s AND project_id=%s "
                "RETURNING id, project_id, title, mode, state, archived, run_id, "
                "  created_at, updated_at",
                (bool(archived), conversation_id, project_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("conversation not found")
            return dict(row)

    def set_mode(self, principal: str, project_id: str, conversation_id: str,
                 mode: str) -> dict:
        mode = self._mode(mode)
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute(
                "UPDATE nexus_conversations SET mode=%s, updated_at=now() "
                "WHERE id=%s AND project_id=%s "
                "RETURNING id, project_id, title, mode, state, archived, run_id, "
                "  created_at, updated_at",
                (mode, conversation_id, project_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("conversation not found")
            return dict(row)

    def linked_run(self, principal: str, project_id: str, conversation_id: str) -> dict | None:
        """The run this conversation started, if any, or ``None``.

        Used by the Intent Router: "продолжи" is an order only when there is
        real work to continue. Membership is checked, so a non-member gets
        "not found" (a raised ``NotFound``) rather than a window into the chat.
        """
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute(
                "SELECT r.id, r.state FROM nexus_conversations c "
                "JOIN nexus_runs r ON r.id = c.run_id "
                "WHERE c.id=%s AND c.project_id=%s", (conversation_id, project_id))
            row = cur.fetchone()
        return {"id": str(row["id"]), "state": row["state"]} if row else None

    def link_run(self, principal: str, project_id: str, conversation_id: str,
                 run_id: str | None) -> dict:
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute(
                "UPDATE nexus_conversations SET run_id=%s, updated_at=now() "
                "WHERE id=%s AND project_id=%s "
                "RETURNING id, project_id, title, mode, state, archived, run_id, "
                "  created_at, updated_at",
                (run_id, conversation_id, project_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("conversation not found")
            return dict(row)

    # ---------------------------------------------------------------- messages
    def add_message(self, principal: str, project_id: str, conversation_id: str, *,
                    role: str, content: str, meta: dict | None = None) -> dict:
        if role not in ("user", "assistant", "tool", "system"):
            raise ValueError("role must be user, assistant, tool or system")
        content = _text(content, "content", maximum=MESSAGE_CHARS, required=False)
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute("SELECT id FROM nexus_conversations WHERE id=%s AND project_id=%s",
                        (conversation_id, project_id))
            if cur.fetchone() is None:
                raise NotFound("conversation not found")
            return self.append_internal(cur, conversation_id, role=role, content=content,
                                        meta=meta)

    @staticmethod
    def append_internal(cur: Any, conversation_id: str, *, role: str, content: str,
                        meta: dict | None = None, run_id: str | None = None,
                        state: str | None = None) -> dict:
        """Append a message inside the caller's transaction. No membership check.

        For work that finishes after the request that asked for it has returned
        (a plan built in the background): the message is written in the same
        transaction as the thing it announces, so the chat never says a run
        started that did not, and never stays silent about one that did.
        *run_id* links the conversation to a run; *state* sets its state.

        A transcript is read in ``created_at`` order, and ``now()`` is one
        instant for a whole transaction, so two messages appended together
        would be listed in either order. Each message is therefore stamped
        later than the newest one the conversation already has.
        """
        cur.execute(
            "INSERT INTO nexus_conversation_messages(conversation_id, role, content, meta, "
            "                                        created_at) "
            "VALUES (%s,%s,%s,%s, GREATEST(clock_timestamp(), COALESCE("
            "  (SELECT max(created_at) FROM nexus_conversation_messages "
            "   WHERE conversation_id=%s), '-infinity'::timestamptz) "
            "  + interval '1 microsecond')) "
            "RETURNING id, conversation_id, role, content, meta, created_at",
            (conversation_id, role, content, _json(meta or {}), conversation_id))
        row = dict(cur.fetchone())
        cur.execute("UPDATE nexus_conversations SET updated_at=now(), "
                    "run_id=COALESCE(%s, run_id), state=COALESCE(%s, state) WHERE id=%s",
                    (run_id, state, conversation_id))
        return row

    def post_internal(self, conversation_id: str, messages: list[dict], *,
                      state: str | None = None) -> None:
        """Append system-originated *messages* to a conversation, if it still exists."""
        with self.store._tx() as cur:
            cur.execute("SELECT 1 FROM nexus_conversations WHERE id=%s", (conversation_id,))
            if cur.fetchone() is None:
                return
            for message in messages:
                self.append_internal(cur, conversation_id, role=message["role"],
                                     content=message["content"], meta=message.get("meta"),
                                     state=state)

    def list_messages(self, principal: str, project_id: str, conversation_id: str,
                      *, limit: int = 500) -> list[dict]:
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute("SELECT id FROM nexus_conversations WHERE id=%s AND project_id=%s",
                        (conversation_id, project_id))
            if cur.fetchone() is None:
                raise NotFound("conversation not found")
            cur.execute(
                "SELECT m.id, m.conversation_id, m.role, m.content, m.meta, m.created_at, "
                "  a.file_id, a.kind AS attachment_kind, a.filename AS attachment_filename, "
                "  a.content_type AS attachment_content_type, a.size AS attachment_size "
                "FROM nexus_conversation_messages m "
                "LEFT JOIN nexus_conversation_attachments a ON a.message_id = m.id "
                "WHERE m.conversation_id=%s ORDER BY m.created_at, m.id LIMIT %s",
                (conversation_id, limit))
            out: list[dict] = []
            by_id: dict[str, dict] = {}
            for row in cur.fetchall():
                mid = str(row["id"])
                msg = by_id.get(mid)
                if msg is None:
                    msg = {"id": mid, "role": row["role"], "content": row["content"],
                           "meta": row["meta"], "created_at": row["created_at"],
                           "attachments": []}
                    by_id[mid] = msg
                    out.append(msg)
                if row["file_id"] is not None:
                    msg["attachments"].append({
                        "file_id": str(row["file_id"]), "kind": row["attachment_kind"],
                        "filename": row["attachment_filename"],
                        "content_type": row["attachment_content_type"],
                        "size": row["attachment_size"]})
            return out

    def set_state(self, principal: str, project_id: str, conversation_id: str,
                  state: str) -> None:
        if state not in ("idle", "thinking", "error"):
            raise ValueError("state must be idle, thinking or error")
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute("UPDATE nexus_conversations SET state=%s, updated_at=now() "
                        "WHERE id=%s AND project_id=%s", (state, conversation_id, project_id))

    # ------------------------------------------------------------- attachments
    def attach(self, principal: str, project_id: str, conversation_id: str, *,
               file_id: str | None, message_id: str | None, kind: str,
               filename: str, content_type: str, size: int) -> dict:
        if kind not in ("file", "image", "archive", "voice"):
            raise ValueError("kind must be file, image, archive or voice")
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute("SELECT id FROM nexus_conversations WHERE id=%s AND project_id=%s",
                        (conversation_id, project_id))
            if cur.fetchone() is None:
                raise NotFound("conversation not found")
            cur.execute(
                "INSERT INTO nexus_conversation_attachments"
                "(conversation_id, message_id, file_id, kind, filename, content_type, size) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING id, conversation_id, message_id, file_id, kind, filename, "
                "  content_type, size, created_at",
                (conversation_id, message_id, file_id, kind, filename[:255],
                 content_type[:255], int(size)))
            return dict(cur.fetchone())

    # ------------------------------------------------------------------ agents
    def start_agent_session(self, principal: str, project_id: str, *,
                            conversation_id: str | None,
                            openhands_account_id: str | None,
                            external_conversation_id: str | None,
                            repository: str = "", branch: str = "") -> dict:
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute(
                "INSERT INTO nexus_agent_sessions"
                "(project_id, conversation_id, openhands_account_id, "
                " external_conversation_id, state, repository, branch) "
                "VALUES (%s,%s,%s,%s,'starting',%s,%s) "
                "RETURNING id, project_id, conversation_id, openhands_account_id, "
                "  external_conversation_id, run_id, run_task_id, state, "
                "  current_activity, repository, branch, last_error, created_at, updated_at",
                (project_id, conversation_id, openhands_account_id,
                 external_conversation_id, repository, branch))
            return self._session_view(cur.fetchone())

    def update_agent_session(self, session_id: str, *, state: str | None = None,
                             external_conversation_id: str | None = None,
                             current_activity: str | None = None,
                             last_error: str | None = None) -> dict:
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_agent_sessions SET "
                "  state=COALESCE(%s, state), "
                "  external_conversation_id=COALESCE(%s, external_conversation_id), "
                "  current_activity=COALESCE(%s, current_activity), "
                "  last_error=%s, updated_at=now() "
                "WHERE id=%s "
                "RETURNING id, project_id, conversation_id, openhands_account_id, "
                "  external_conversation_id, run_id, run_task_id, state, "
                "  current_activity, repository, branch, last_error, created_at, updated_at",
                (state, external_conversation_id, current_activity, last_error, session_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("agent session not found")
            return self._session_view(row)

    def list_agent_sessions(self, principal: str, project_id: str, *,
                            conversation_id: str | None = None) -> list[dict]:
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            sql = ("SELECT s.id, s.project_id, s.conversation_id, s.openhands_account_id, "
                   "  s.external_conversation_id, s.run_id, s.run_task_id, s.state, "
                   "  s.current_activity, s.repository, s.branch, s.last_error, "
                   "  s.created_at, s.updated_at, a.label AS account_label "
                   "FROM nexus_agent_sessions s "
                   "LEFT JOIN nexus_openhands_accounts a ON a.id = s.openhands_account_id "
                   "WHERE s.project_id=%s")
            params: list[Any] = [project_id]
            if conversation_id is not None:
                sql += " AND s.conversation_id=%s"
                params.append(conversation_id)
            sql += " ORDER BY s.created_at DESC, s.id LIMIT 200"
            cur.execute(sql, tuple(params))
            return [self._session_view(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _session_view(row: Any) -> dict:
        out = dict(row)
        for key in ("id", "project_id", "conversation_id", "openhands_account_id",
                    "run_id", "run_task_id"):
            if out.get(key) is not None:
                out[key] = str(out[key])
        return out

    @staticmethod
    def check_mode(mode: Any) -> str:
        """*mode* if it is one a conversation can be in, else ``ValueError``."""
        if mode not in _MODES:
            raise ValueError("mode must be ask, code, plan or autonomous")
        return mode

    _mode = check_mode

    @staticmethod
    def _project(cur: Any, principal: str, project_id: str) -> None:
        cur.execute("SELECT 1 FROM nexus_project_members "
                    "WHERE project_id=%s AND principal=%s", (project_id, principal))
        if cur.fetchone() is None:
            raise NotFound("project not found")

    @staticmethod
    def _event(cur: Any, project_id: Any, principal: str, kind: str, resource_id: Any) -> None:
        cur.execute("INSERT INTO nexus_project_events(project_id, actor, kind, resource_id) "
                    "VALUES (%s,%s,%s,%s)", (project_id, principal, kind, resource_id))


def _json(value: Any) -> str:
    import json

    return json.dumps(value, default=str)
