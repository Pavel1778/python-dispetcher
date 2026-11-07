"""NEXUS project foundation, intentionally separate from legacy execution.

Authorization is part of every query, not an ID-only preflight. The existing
single-user authentication supplies the principal; no user-controlled project
or principal is trusted. Draft goals do not start workflows or agents.
"""
from __future__ import annotations

from typing import Any

from .cloud_store import NotFound, PostgresStore


class IdempotencyConflict(ValueError):
    """The same project's request key was reused with a different goal."""


def _text(value: Any, field: str, *, maximum: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    result = value.strip()
    if "\x00" in result:
        raise ValueError(f"{field} contains an invalid character")
    if required and not result:
        raise ValueError(f"{field} is required")
    if len(result) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return result


def principal_for_session(username: str | None) -> str:
    """A bearer acts as the configured owner; a session uses its username.

    Without a configured owner, the legacy machine token has its own namespace.
    The caller never gets to supply this identity in an HTTP request.
    """
    return f"user:{username}" if username else "machine:default"


class ProjectService:
    def __init__(self, store: PostgresStore):
        self.store = store

    def create(self, principal: str, name: Any, description: Any = "") -> dict:
        name = _text(name, "name", maximum=120)
        description = _text(description, "description", maximum=4000, required=False)
        with self.store._tx() as cur:
            cur.execute("INSERT INTO nexus_projects(name, description) VALUES (%s, %s) "
                        "RETURNING id, name, description, created_at, updated_at", (name, description))
            project = dict(cur.fetchone())
            cur.execute("INSERT INTO nexus_project_members(project_id, principal, role) "
                        "VALUES (%s, %s, 'owner')", (project["id"], principal))
            self._event(cur, project["id"], principal, "project.created", project["id"])
        return project

    def list(self, principal: str) -> list[dict]:
        with self.store._tx() as cur:
            cur.execute("SELECT p.id, p.name, p.description, p.created_at, p.updated_at "
                        "FROM nexus_projects p JOIN nexus_project_members m ON m.project_id=p.id "
                        "WHERE m.principal=%s ORDER BY p.created_at DESC, p.id", (principal,))
            return [dict(row) for row in cur.fetchall()]

    def get(self, principal: str, project_id: str) -> dict:
        with self.store._tx() as cur:
            return self._project(cur, principal, project_id)

    @staticmethod
    def _project(cur: Any, principal: str, project_id: str) -> dict:
        cur.execute("SELECT p.id, p.name, p.description, p.created_at, p.updated_at "
                    "FROM nexus_projects p JOIN nexus_project_members m ON m.project_id=p.id "
                    "WHERE p.id=%s AND m.principal=%s FOR KEY SHARE OF m", (project_id, principal))
        row = cur.fetchone()
        if row is None:
            raise NotFound("project not found")
        return dict(row)

    def update(self, principal: str, project_id: str, changes: dict) -> dict:
        if not changes or set(changes) - {"name", "description"}:
            raise ValueError("provide name and/or description")
        name = _text(changes["name"], "name", maximum=120) if "name" in changes else None
        description = (_text(changes["description"], "description", maximum=4000, required=False)
                       if "description" in changes else None)
        with self.store._tx() as cur:
            cur.execute("UPDATE nexus_projects p SET name=COALESCE(%s, p.name), "
                        "description=COALESCE(%s, p.description), updated_at=now() "
                        "WHERE p.id=%s AND EXISTS (SELECT 1 FROM nexus_project_members m "
                        "WHERE m.project_id=p.id AND m.principal=%s AND m.role='owner') "
                        "RETURNING p.id, p.name, p.description, p.created_at, p.updated_at",
                        (name, description, project_id, principal))
            row = cur.fetchone()
            if row is None:
                raise NotFound("project not found")
            self._event(cur, project_id, principal, "project.updated", project_id)
            return dict(row)

    def create_goal(self, principal: str, project_id: str, text: Any,
                    key: str | None = None) -> tuple[dict, bool]:
        text = _text(text, "text", maximum=20000)
        if key is not None:
            key = _text(key, "Idempotency-Key", maximum=200)
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute("INSERT INTO nexus_goals(project_id, text, created_by, idempotency_key) "
                        "VALUES (%s,%s,%s,%s) ON CONFLICT (project_id, idempotency_key) "
                        "DO NOTHING RETURNING id, project_id, text, state, created_by, created_at",
                        (project_id, text, principal, key))
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT id, project_id, text, state, created_by, created_at "
                            "FROM nexus_goals WHERE project_id=%s AND idempotency_key=%s",
                            (project_id, key))
                row = cur.fetchone()
                if row["text"] != text or row["created_by"] != principal:
                    raise IdempotencyConflict("idempotency key already used for a different request")
                return dict(row), False
            self._event(cur, project_id, principal, "goal.created", row["id"])
            return dict(row), True

    def list_goals(self, principal: str, project_id: str) -> list[dict]:
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute("SELECT id, project_id, text, state, created_by, created_at "
                        "FROM nexus_goals WHERE project_id=%s ORDER BY created_at DESC, id LIMIT 200",
                        (project_id,))
            return [dict(row) for row in cur.fetchall()]

    def events(self, principal: str, project_id: str) -> list[dict]:
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute("SELECT seq, project_id, actor, kind, resource_id, created_at "
                        "FROM nexus_project_events WHERE project_id=%s ORDER BY seq DESC LIMIT 200",
                        (project_id,))
            return [dict(row) for row in cur.fetchall()]

    @staticmethod
    def _event(cur: Any, project_id: Any, principal: str, kind: str, resource_id: Any) -> None:
        cur.execute("INSERT INTO nexus_project_events(project_id, actor, kind, resource_id) "
                    "VALUES (%s,%s,%s,%s)", (project_id, principal, kind, resource_id))
