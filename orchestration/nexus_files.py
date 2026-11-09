"""Bounded opaque file storage, scoped to a project and never unpacked."""
from __future__ import annotations

import hashlib
import unicodedata
from typing import Any

from .cloud_store import NotFound, PostgresStore
from .nexus_projects import ProjectService

MAX_FILE_SIZE = 5 * 1024 * 1024
MAX_PROJECT_BYTES = 20 * 1024 * 1024
MAX_PROJECT_FILES = 100


def validate_filename(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 255:
        raise ValueError("invalid file name")
    if value in (".", "..") or any(c in "/\\" or unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("file name must not contain paths or control characters")
    return value


class FileService:
    def __init__(self, store: PostgresStore):
        self.store = store

    def upload(self, principal: str, project_id: str, name: Any, data: bytes) -> dict:
        name = validate_filename(name)
        if len(data) > MAX_FILE_SIZE:
            raise ValueError("file exceeds 5 MiB")
        digest = hashlib.sha256(data).hexdigest()
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            # Serialize quotas for concurrent uploads to the same project.
            cur.execute("SELECT id FROM nexus_projects WHERE id=%s FOR UPDATE", (project_id,))
            cur.execute("SELECT count(*) AS n, COALESCE(sum(size),0) AS bytes "
                        "FROM nexus_files WHERE project_id=%s", (project_id,))
            used = cur.fetchone()
            if used["n"] >= MAX_PROJECT_FILES or used["bytes"] + len(data) > MAX_PROJECT_BYTES:
                raise ValueError("project file quota exceeded")
            cur.execute("INSERT INTO nexus_files(project_id,name,size,sha256,content,created_by) "
                        "VALUES (%s,%s,%s,%s,%s,%s) "
                        "RETURNING id, project_id, name, size, sha256, created_by, created_at",
                        (project_id, name, len(data), digest, data, principal))
            result = dict(cur.fetchone())
            ProjectService._event(cur, project_id, principal, "file.uploaded", result["id"])
            return result

    def list(self, principal: str, project_id: str) -> list[dict]:
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("SELECT id, project_id, name, size, sha256, created_by, created_at "
                        "FROM nexus_files WHERE project_id=%s AND name IS NOT NULL "
                        "ORDER BY created_at DESC, id LIMIT %s",
                        (project_id, MAX_PROJECT_FILES))
            return [dict(row) for row in cur.fetchall()]

    def is_opaque(self, principal: str, project_id: str, file_id: str) -> bool:
        """Whether a file row was written by this (opaque) service.

        The unified ``nexus_files`` table holds rows from two upload designs.
        An opaque row carries ``name``; a File Lab row carries ``filename``.
        The GET route uses this to decide between a raw attachment and a JSON
        preview without changing either contract.
        """
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("SELECT (name IS NOT NULL) AS opaque FROM nexus_files "
                        "WHERE project_id=%s AND id=%s", (project_id, file_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("file not found")
            return bool(row["opaque"])

    def download(self, principal: str, project_id: str, file_id: str) -> dict:
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("SELECT name, content FROM nexus_files WHERE project_id=%s AND id=%s",
                        (project_id, file_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("file not found")
            return dict(row)

    def delete(self, principal: str, project_id: str, file_id: str) -> None:
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("DELETE FROM nexus_files f WHERE f.project_id=%s AND f.id=%s "
                        "AND EXISTS (SELECT 1 FROM nexus_project_members m WHERE m.project_id=f.project_id "
                        "AND m.principal=%s AND m.role='owner') RETURNING f.id",
                        (project_id, file_id, principal))
            if cur.fetchone() is None:
                raise NotFound("file not found")
            ProjectService._event(cur, project_id, principal, "file.deleted", file_id)
