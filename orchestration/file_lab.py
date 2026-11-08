"""File laboratory and development workspaces.

Two related, project-scoped capabilities:

* **File lab** - upload a document, image, source file or archive into a
  project, preview its text, and hand it to an agent. Bytes are stored in
  PostgreSQL (``nexus_files``), never on the server's filesystem, so a restart
  keeps them and a hostile filename cannot touch the host.
* **Workspaces** - a durable, project-scoped file tree an agent or a human can
  inspect and edit. Extracting an archive into a workspace goes through
  :mod:`orchestration.archive_safety`, which rejects traversal, links and
  bombs before a single row is written. The workspace is *data*, not a shell:
  nothing here executes an uploaded file.

Every read and write is authorized against project membership in the same
query that touches the row, so a foreign id is a 404 rather than a leak.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
from typing import Any

from .archive_safety import (
    ArchiveLimits,
    ArchiveUnsafe,
    is_archive_name,
    open_archive,
)
from .cloud_store import NotFound, PostgresStore
from .nexus_projects import _text

#: Extensions we will render as text in the preview pane. Anything else is
#: offered as a download instead of being shown as mojibake.
TEXT_EXTS = (
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl",
    ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".sql",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go",
    ".rs", ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift",
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".dockerfile", ".make", ".mk",
    ".html", ".htm", ".css", ".scss", ".sass", ".less", ".vue", ".svelte",
    ".gitignore", ".gitattributes", ".editorconfig", ".gradle", ".properties",
)

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico")

#: Preview bytes returned inline. A larger file is truncated, and the response
#: says so, so the UI never pretends it showed the whole thing.
MAX_PREVIEW_BYTES = 256 * 1024

_DEFAULT_MAX_UPLOAD = 32 * 1024 * 1024  # 32 MiB

#: A workspace file's path must be a clean relative POSIX path. The database
#: re-checks this, but validating here gives a 400 instead of a 500. The
#: normalization in ``safe_workspace_path`` already removes ``.``/``..`` and
#: empty segments, so this only has to bound the length and forbid a leading
#: slash or a doubled slash.
_SAFE_PATH = re.compile(r"^[^/][^/]*(/[^/]+)*$")


def max_upload_bytes() -> int:
    raw = os.environ.get("NEXUS_MAX_UPLOAD_BYTES", "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return _DEFAULT_MAX_UPLOAD


def is_text_filename(filename: str) -> bool:
    name = (filename or "").lower()
    if name.endswith(TEXT_EXTS):
        return True
    base = posixpath.basename(name)
    return base in ("dockerfile", "makefile", "license", "readme")


def is_image_filename(filename: str) -> bool:
    return (filename or "").lower().endswith(IMAGE_EXTS)


def decode_preview(data: bytes) -> tuple[str, bool]:
    """Return ``(text, truncated)`` for a preview, replacing bad bytes.

    Decoding with ``errors="replace"`` means a file that is not really text
    still renders (with replacement characters) instead of raising; the UI only
    calls this for filenames it believes are text.
    """
    truncated = len(data) > MAX_PREVIEW_BYTES
    chunk = data[:MAX_PREVIEW_BYTES]
    return chunk.decode("utf-8", errors="replace"), truncated


def safe_workspace_path(path: str) -> str:
    """Normalize a workspace path, rejecting anything that could escape it."""
    if not isinstance(path, str):
        raise ValueError("path must be a string")
    candidate = path.replace("\\", "/").strip()
    if not candidate or candidate.startswith("/") or "\x00" in candidate:
        raise ValueError("invalid path")
    parts = [p for p in candidate.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ValueError("invalid path")
    normalized = posixpath.join(*parts)
    if not _SAFE_PATH.match(normalized):
        raise ValueError("invalid path")
    return normalized


class FileLabService:
    """Uploads, previews, archive extraction and workspaces."""

    def __init__(self, store: PostgresStore):
        self.store = store

    # ----------------------------------------------------------- authorization
    @staticmethod
    def _project(cur: Any, principal: str, project_id: str) -> None:
        """Membership check inside the caller's transaction (see ProjectService)."""
        cur.execute(
            "SELECT 1 FROM nexus_projects p JOIN nexus_project_members m "
            "ON m.project_id=p.id WHERE p.id=%s AND m.principal=%s",
            (project_id, principal),
        )
        if cur.fetchone() is None:
            raise NotFound("project not found")

    # ------------------------------------------------------------------ files
    def upload(self, principal: str, project_id: str, *, filename: Any,
               content_type: Any, data: bytes) -> dict:
        filename = _text(filename, "filename", maximum=255)
        # A filename is metadata, not a path: strip any directory part so a
        # caller cannot smuggle separators into the stored name.
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not filename or filename in (".", ".."):
            raise ValueError("filename is invalid")
        if not isinstance(data, bytes):
            raise ValueError("file data is required")
        if len(data) > max_upload_bytes():
            raise ValueError(f"file exceeds the {max_upload_bytes()} byte upload limit")
        content_type = _text(content_type or "application/octet-stream",
                             "content_type", maximum=200, required=False)
        digest = hashlib.sha256(data).hexdigest()
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute(
                "INSERT INTO nexus_files(project_id, filename, content_type, size, "
                "sha256, data, is_archive, uploaded_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING id, project_id, filename, content_type, size, sha256, "
                "is_archive, created_at",
                (project_id, filename, content_type, len(data), digest, data,
                 is_archive_name(filename), principal),
            )
            row = dict(cur.fetchone())
        row["id"] = str(row["id"])
        return row

    def list_files(self, principal: str, project_id: str, *, limit: int = 200) -> list[dict]:
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute(
                "SELECT id, filename, content_type, size, sha256, is_archive, created_at "
                "FROM nexus_files WHERE project_id=%s ORDER BY created_at DESC, id LIMIT %s",
                (project_id, limit),
            )
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r["id"] = str(r["id"])
        return rows

    def get_file(self, principal: str, project_id: str, file_id: str) -> dict:
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute(
                "SELECT id, project_id, filename, content_type, size, sha256, "
                "is_archive, created_at, data FROM nexus_files "
                "WHERE id=%s AND project_id=%s",
                (file_id, project_id),
            )
            row = cur.fetchone()
        if row is None:
            raise NotFound("file not found")
        row = dict(row)
        row["id"] = str(row["id"])
        row["project_id"] = str(row["project_id"])
        return row

    def delete_file(self, principal: str, project_id: str, file_id: str) -> bool:
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute("DELETE FROM nexus_files WHERE id=%s AND project_id=%s",
                        (file_id, project_id))
            return cur.rowcount > 0

    def preview(self, principal: str, project_id: str, file_id: str) -> dict:
        row = self.get_file(principal, project_id, file_id)
        data = bytes(row["data"])
        filename = row["filename"]
        kind = "text" if is_text_filename(filename) else (
            "image" if is_image_filename(filename) else "binary")
        out = {
            "id": row["id"], "filename": filename,
            "content_type": row["content_type"], "size": row["size"],
            "kind": kind,
        }
        if kind == "text":
            text, truncated = decode_preview(data)
            out["text"] = text
            out["truncated"] = truncated
        return out

    # -------------------------------------------------------------- archives
    def extract_into_workspace(self, principal: str, project_id: str, *,
                               name: Any, file_id: str,
                               limits: ArchiveLimits | None = None) -> dict:
        """Create a workspace from an uploaded archive.

        The archive is validated in memory by :mod:`orchestration.archive_safety`
        before any row is written; a rejected archive leaves no workspace behind.
        """
        name = _text(name, "name", maximum=120)
        source = self.get_file(principal, project_id, file_id)
        if not source["is_archive"]:
            raise ValueError("file is not a recognised archive")
        try:
            report = open_archive(bytes(source["data"]), source["filename"], limits)
        except ArchiveUnsafe as e:
            raise ValueError(f"archive rejected: {e}") from e
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute(
                "INSERT INTO nexus_workspaces(project_id, name, source_file_id, created_by) "
                "VALUES (%s,%s,%s,%s) RETURNING id, project_id, name, created_at",
                (project_id, name, file_id, principal),
            )
            workspace = dict(cur.fetchone())
            workspace_id = workspace["id"]
            for member in report.members:
                cur.execute(
                    "INSERT INTO nexus_workspace_files(workspace_id, path, is_dir, size, data) "
                    "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (workspace_id, path) DO NOTHING",
                    (workspace_id, member.path, member.is_dir, len(member.data),
                     None if member.is_dir else member.data),
                )
        workspace["id"] = str(workspace_id)
        workspace["file_count"] = len(report.members)
        workspace["skipped"] = report.skipped
        workspace["total_uncompressed"] = report.total_uncompressed
        return workspace

    # ------------------------------------------------------------ workspaces
    def create_workspace(self, principal: str, project_id: str, *, name: Any) -> dict:
        name = _text(name, "name", maximum=120)
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute(
                "INSERT INTO nexus_workspaces(project_id, name, created_by) "
                "VALUES (%s,%s,%s) RETURNING id, project_id, name, created_at",
                (project_id, name, principal),
            )
            row = dict(cur.fetchone())
        row["id"] = str(row["id"])
        return row

    def list_workspaces(self, principal: str, project_id: str) -> list[dict]:
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute(
                "SELECT w.id, w.name, w.created_at, w.updated_at, "
                "(SELECT count(*) FROM nexus_workspace_files f "
                " WHERE f.workspace_id=w.id AND NOT f.is_dir) AS file_count "
                "FROM nexus_workspaces w WHERE w.project_id=%s "
                "ORDER BY w.created_at DESC, w.id LIMIT 200",
                (project_id,),
            )
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r["id"] = str(r["id"])
        return rows

    def _workspace(self, cur: Any, principal: str, project_id: str,
                   workspace_id: str) -> dict:
        self._project(cur, principal, project_id)
        cur.execute(
            "SELECT id, project_id, name, source_file_id, created_at, updated_at "
            "FROM nexus_workspaces WHERE id=%s AND project_id=%s",
            (workspace_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            raise NotFound("workspace not found")
        return dict(row)

    def delete_workspace(self, principal: str, project_id: str, workspace_id: str) -> bool:
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute("DELETE FROM nexus_workspaces WHERE id=%s AND project_id=%s",
                        (workspace_id, project_id))
            return cur.rowcount > 0

    def list_workspace_files(self, principal: str, project_id: str,
                             workspace_id: str) -> list[dict]:
        with self.store._tx() as cur:
            self._workspace(cur, principal, project_id, workspace_id)
            cur.execute(
                "SELECT path, is_dir, size, (original_data IS NOT NULL) AS edited, "
                "updated_at FROM nexus_workspace_files WHERE workspace_id=%s "
                "ORDER BY path LIMIT 5000",
                (workspace_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_workspace_file(self, principal: str, project_id: str,
                           workspace_id: str, path: str) -> dict:
        path = safe_workspace_path(path)
        with self.store._tx() as cur:
            self._workspace(cur, principal, project_id, workspace_id)
            cur.execute(
                "SELECT path, is_dir, size, data, original_data, updated_at "
                "FROM nexus_workspace_files WHERE workspace_id=%s AND path=%s",
                (workspace_id, path),
            )
            row = cur.fetchone()
        if row is None:
            raise NotFound("file not found")
        row = dict(row)
        data = bytes(row["data"] or b"")
        row["kind"] = "dir" if row["is_dir"] else (
            "text" if is_text_filename(path) else (
                "image" if is_image_filename(path) else "binary"))
        if row["kind"] == "text":
            row["text"], row["truncated"] = decode_preview(data)
        row.pop("data", None)
        row.pop("original_data", None)
        return row

    def put_workspace_file(self, principal: str, project_id: str, workspace_id: str,
                           *, path: Any, text: Any) -> dict:
        path = safe_workspace_path(path)
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        data = text.encode("utf-8")
        if len(data) > max_upload_bytes():
            raise ValueError("file content exceeds the size limit")
        with self.store._tx() as cur:
            self._workspace(cur, principal, project_id, workspace_id)
            # Keep the pre-edit bytes once, so the diff shows the imported
            # original rather than the previous edit.
            cur.execute(
                "INSERT INTO nexus_workspace_files(workspace_id, path, is_dir, size, "
                "data, original_data) VALUES (%s,%s,false,%s,%s,NULL) "
                "ON CONFLICT (workspace_id, path) DO UPDATE SET "
                "  original_data = COALESCE(nexus_workspace_files.original_data, "
                "                            nexus_workspace_files.data), "
                "  data = EXCLUDED.data, size = EXCLUDED.size, updated_at = now() "
                "RETURNING path, size, updated_at",
                (workspace_id, path, len(data), data),
            )
            row = dict(cur.fetchone())
            cur.execute("UPDATE nexus_workspaces SET updated_at=now() WHERE id=%s",
                        (workspace_id,))
        return row

    def delete_workspace_file(self, principal: str, project_id: str,
                              workspace_id: str, path: str) -> bool:
        path = safe_workspace_path(path)
        with self.store._tx() as cur:
            self._workspace(cur, principal, project_id, workspace_id)
            cur.execute(
                "DELETE FROM nexus_workspace_files WHERE workspace_id=%s AND path=%s",
                (workspace_id, path),
            )
            return cur.rowcount > 0

    def workspace_diff(self, principal: str, project_id: str,
                       workspace_id: str) -> list[dict]:
        """Per-file unified diff for every file edited in the workspace.

        A file with no stored original (created in the workspace) diffs against
        the empty string, which is what a reader expects to see.
        """
        import difflib

        with self.store._tx() as cur:
            self._workspace(cur, principal, project_id, workspace_id)
            cur.execute(
                "SELECT path, data, original_data FROM nexus_workspace_files "
                "WHERE workspace_id=%s AND NOT is_dir AND original_data IS NOT NULL "
                "ORDER BY path",
                (workspace_id,),
            )
            rows = cur.fetchall()
        out = []
        for row in rows:
            before = bytes(row["original_data"] or b"").decode("utf-8", errors="replace")
            after = bytes(row["data"] or b"").decode("utf-8", errors="replace")
            if before == after:
                continue
            diff = list(difflib.unified_diff(
                before.splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile=f"a/{row['path']}", tofile=f"b/{row['path']}",
            ))
            out.append({"path": row["path"], "diff": "".join(diff),
                        "changed": True})
        return out
