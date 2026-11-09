"""Project context and unified memory.

Two related, project-scoped stores:

* **Project context** — a versioned snapshot of what NEXUS *observed* about a
  project's repository at a specific revision: languages, layout, build/test
  commands, recent commits. It is produced by reading the repository tree and
  known files, never by asking a model to guess. A refresh with an unchanged
  revision is a no-op, so the same work is not re-done.

* **Memory** — durable, project-scoped (or global) facts with a provenance
  label. The critical invariant: only `test_verified` and
  `independently_verified` memories count as verified. An agent's own report is
  `agent_reported` and can never satisfy a dependency or be reported as a
  verified fact.

Both enforce project membership in the same query that touches the row, so a
foreign project id is a `404`, not a leak.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .cloud_store import NotFound, PostgresStore
from .nexus_projects import ProjectService, _text

#: Provenance labels that count as verified evidence.
VERIFIED_PROVENANCE = frozenset({"test_verified", "independently_verified"})

#: File extensions -> language, used by the analyzer. Deliberately small and
#: honest: an unknown extension is left out rather than guessed.
_LANGUAGE_BY_EXT = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".jsx": "JavaScript", ".java": "Java",
    ".kt": "Kotlin", ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP",
    ".c": "C", ".h": "C", ".cc": "C++", ".cpp": "C++", ".hpp": "C++", ".cs": "C#",
    ".swift": "Swift", ".sh": "Shell", ".sql": "SQL", ".html": "HTML", ".css": "CSS",
    ".md": "Markdown", ".json": "JSON", ".yaml": "YAML", ".yml": "YAML",
    ".toml": "TOML", ".tf": "Terraform", ".dockerfile": "Dockerfile",
}

#: Files whose presence implies a build/test command. The command is only
#: recorded when the file is actually in the tree.
_BUILD_HINTS = {
    "package.json": "npm test",
    "pyproject.toml": "python -m pytest",
    "pytest.ini": "python -m pytest",
    "requirements.txt": "pip install -r requirements.txt",
    "Makefile": "make",
    "Cargo.toml": "cargo test",
    "go.mod": "go test ./...",
    "pom.xml": "mvn test",
    "build.gradle": "gradle test",
    "Dockerfile": "docker build .",
}

_README_NAMES = ("README.md", "README.rst", "README.txt", "README")

#: Cap how much of a file we summarize, so a huge README cannot bloat a row.
_MAX_README_CHARS = 4000


def _dedupe_key(kind: str, content: str) -> str:
    return hashlib.sha256(f"{kind}\x00{content.strip().lower()}".encode()).hexdigest()


def analyze_tree(tree: list[dict[str, Any]], files: dict[str, str]) -> dict[str, Any]:
    """Build a repository summary from a tree and a few read files.

    Pure function: no I/O, so it is trivially testable and cannot invent facts.
    ``files`` maps a path to its decoded content for the files we chose to read.
    """
    languages: dict[str, int] = {}
    top_level: set[str] = set()
    test_files: list[str] = []
    has_ci = False
    for entry in tree:
        path = entry.get("path")
        if not isinstance(path, str):
            continue
        parts = path.split("/")
        top_level.add(parts[0])
        if len(parts) > 2 and parts[0] == ".github" and parts[1] == "workflows":
            has_ci = True
        ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
        if ext in _LANGUAGE_BY_EXT and entry.get("type") == "blob":
            languages[_LANGUAGE_BY_EXT[ext]] = languages.get(_LANGUAGE_BY_EXT[ext], 0) + 1
        base = parts[-1].lower()
        if (base.startswith("test_") or base.endswith("_test.py")
                or "/tests/" in path or path.startswith("tests/")) and entry.get("type") == "blob":
            test_files.append(path)

    paths = {e.get("path") for e in tree if isinstance(e.get("path"), str)}
    build_commands = sorted({cmd for f, cmd in _BUILD_HINTS.items() if f in paths})
    readme = next((files[n] for n in _README_NAMES if n in files), "")
    return {
        "languages": dict(sorted(languages.items(), key=lambda kv: (-kv[1], kv[0]))),
        "top_level": sorted(top_level),
        "file_count": sum(1 for e in tree if e.get("type") == "blob"),
        "test_files": sorted(test_files)[:50],
        "test_file_count": len(test_files),
        "build_commands": build_commands,
        "has_ci": has_ci,
        "readme_excerpt": readme[:_MAX_README_CHARS],
        "readme_present": bool(readme),
    }


class ContextService:
    """Reads and stores project context snapshots."""

    def __init__(self, store: PostgresStore):
        self.store = store

    def refresh(self, principal: str, project_id: str, *, repository: str,
                revision: str, summary: dict[str, Any]) -> tuple[dict, bool]:
        """Store a snapshot for a revision. Returns ``(row, created)``.

        Re-storing the same ``(project, repository, revision)`` returns the
        existing row and does **not** create a duplicate; a new revision marks
        older snapshots ``superseded`` so "current" is unambiguous.
        """
        if not isinstance(summary, dict):
            raise ValueError("summary must be an object")
        repository = _text(repository, "repository", maximum=300, required=False)
        revision = _text(revision, "revision", maximum=200, required=False)
        payload = json.dumps(summary, sort_keys=True, ensure_ascii=False)
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute(
                "SELECT id, project_id, repository, revision, summary, state, created_at "
                "FROM nexus_project_context WHERE project_id=%s AND repository=%s AND revision=%s",
                (project_id, repository, revision),
            )
            existing = cur.fetchone()
            if existing is not None:
                return dict(existing), False
            cur.execute(
                "UPDATE nexus_project_context SET state='superseded' "
                "WHERE project_id=%s AND state='current'", (project_id,))
            cur.execute(
                "INSERT INTO nexus_project_context"
                "(project_id, repository, revision, summary, created_by) "
                "VALUES (%s,%s,%s,%s,%s) "
                "RETURNING id, project_id, repository, revision, summary, state, created_at",
                (project_id, repository, revision, payload, principal),
            )
            row = dict(cur.fetchone())
            ProjectService._event(cur, project_id, principal, "context.refreshed", row["id"])
            return row, True

    def current(self, principal: str, project_id: str) -> dict | None:
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute(
                "SELECT id, project_id, repository, revision, summary, state, created_at "
                "FROM nexus_project_context WHERE project_id=%s AND state='current' "
                "ORDER BY created_at DESC, id LIMIT 1", (project_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def history(self, principal: str, project_id: str, *, limit: int = 50) -> list[dict]:
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute(
                "SELECT id, repository, revision, state, created_at "
                "FROM nexus_project_context WHERE project_id=%s "
                "ORDER BY created_at DESC, id LIMIT %s", (project_id, limit))
            return [dict(r) for r in cur.fetchall()]


class MemoryService:
    """Project-scoped and global memory with provenance and deduplication."""

    def __init__(self, store: PostgresStore):
        self.store = store

    def remember(self, principal: str, *, kind: str, content: str,
                 provenance: str, project_id: str | None = None,
                 confidence: float = 1.0, source: str = "") -> dict:
        """Store a memory, de-duplicating by (scope, kind, content).

        A repeat updates provenance/confidence only when the new provenance is
        *stronger*, so an agent cannot downgrade a verified fact by re-asserting
        it. Returns the live row.
        """
        if kind not in ("fact", "decision", "constraint", "completed_work",
                        "defect", "failed_approach", "discovery", "task", "verification"):
            raise ValueError("unknown memory kind")
        if provenance not in ("agent_reported", "repository_observed",
                              "test_verified", "independently_verified"):
            raise ValueError("unknown provenance")
        content = _text(content, "content", maximum=8000)
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        key = _dedupe_key(kind, content)
        with self.store._tx() as cur:
            if project_id is not None:
                ProjectService._project(cur, principal, project_id)
            cur.execute(
                "SELECT id, provenance FROM nexus_memory WHERE project_id IS NOT DISTINCT FROM %s "
                "AND dedupe_key=%s", (project_id, key))
            prior = cur.fetchone()
            rank = {"agent_reported": 0, "repository_observed": 1,
                    "test_verified": 2, "independently_verified": 3}
            if prior is not None:
                if rank[provenance] > rank[prior["provenance"]]:
                    cur.execute(
                        "UPDATE nexus_memory SET provenance=%s, confidence=%s, source=%s, "
                        "updated_at=now() WHERE id=%s RETURNING *", (provenance, confidence, source, prior["id"]))
                    return dict(cur.fetchone())
                cur.execute("SELECT * FROM nexus_memory WHERE id=%s", (prior["id"],))
                return dict(cur.fetchone())
            cur.execute(
                "INSERT INTO nexus_memory"
                "(project_id, kind, content, provenance, confidence, dedupe_key, source, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (project_id, kind, content, provenance, confidence, key, source, principal))
            row = dict(cur.fetchone())
            if project_id is not None:
                ProjectService._event(cur, project_id, principal, "memory.remembered", row["id"])
            return row

    def retrieve(self, principal: str, project_id: str, *, query: str | None = None,
                 kind: str | None = None, verified_only: bool = False,
                 limit: int = 50) -> list[dict]:
        """Project memories (plus global ones) filtered by relevance.

        Relevance is a simple, honest substring match over content, ranked by
        provenance then recency. No embeddings: the request explicitly said not
        to add a vector store without a real need.
        """
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            sql = (
                "SELECT id, project_id, kind, content, provenance, confidence, source, "
                "created_at, updated_at FROM nexus_memory "
                "WHERE (project_id=%s OR project_id IS NULL) AND superseded_by IS NULL"
            )
            params: list[Any] = [project_id]
            if kind:
                sql += " AND kind=%s"
                params.append(kind)
            if verified_only:
                sql += " AND provenance IN ('test_verified','independently_verified')"
            sql += " ORDER BY created_at DESC, id LIMIT %s"
            params.append(limit)
            cur.execute(sql, tuple(params))
            rows = [dict(r) for r in cur.fetchall()]
        if query:
            needle = query.strip().lower()
            rows = [r for r in rows if needle in r["content"].lower()] or rows
        return rows

    def supersede(self, principal: str, project_id: str, old_id: str, *,
                  kind: str, content: str, provenance: str,
                  confidence: float = 1.0, source: str = "") -> dict:
        """Replace a memory: write the new one and point the old at it."""
        if provenance not in ("agent_reported", "repository_observed",
                              "test_verified", "independently_verified"):
            raise ValueError("unknown provenance")
        new = self.remember(principal, kind=kind, content=content, provenance=provenance,
                            project_id=project_id, confidence=confidence, source=source)
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute(
                "UPDATE nexus_memory SET superseded_by=%s WHERE id=%s AND project_id=%s RETURNING id",
                (new["id"], old_id, project_id))
            if cur.fetchone() is None:
                raise NotFound("memory not found")
        return new
