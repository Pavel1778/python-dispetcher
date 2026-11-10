"""Migration application with integrity, ordering and effect verification.

The previous runner (``PostgresStore.apply_migrations``) executed each file and
recorded ``schema_migrations(filename, checksum)`` in the same transaction. That
is atomic per file, which is the part it got right, and it had three defects the
audit reproduced:

1. **A recorded success whose effect is absent was invisible.** If the SQL ran
   ``CREATE TABLE IF NOT EXISTS`` against a table that already existed in a
   different shape, the row was recorded and the schema silently stayed wrong.
   Nothing compared the recorded history to what the database must contain.
2. **A changed migration re-executed silently.** A file whose checksum differed
   was re-run under its old name, so the database's history said "0007 applied"
   while the body that ran was a different one. Production file edits are now a
   refusal with the file named, not a re-run.
3. **Ordering and identity were unchecked.** A migration step that was never run
   (a partially-provisioned environment) was indistinguishable from one that had
   been, and two files sharing a number — which is how the duplicate ``0011`` in
   the PR17 line came about — were only caught by luck of sort order.

This module keeps the good part (one transaction per file: SQL and its
``schema_migrations`` row commit together or not at all) and adds:

* a reviewed manifest (``migrations/0000_manifest.json``) of what the tree must
  contain, including per-file checksums and the schema effects each file must
  produce;
* pre-flight validation — duplicate numbers, gaps in the applied history,
  checksum drift, unlisted files, missing predecessors — all reported before any
  SQL runs;
* post-conditions — after a file runs, the tables, columns, indexes and RLS
  flags it claims to create must exist, or the transaction is rolled back and
  the file is *not* recorded. Indexes matter: task-level workspace exclusivity
  rests on ``uq_tasks_workspace_active`` (migration 0015), and a runner that only
  checked tables would report a database as healthy while the invariant it
  depends on was absent.

A failure is never recorded as success, and the exception names the exact file.

Note on nested transactions: several migrations (``0002``, ``0003``, ``0004``,
``0008``, ``0009``, ``0010``, ``0011``) contain ``BEGIN; ... COMMIT;`` themselves.
Inside an already-open transaction PostgreSQL treats those as savepoint pushes and
releases, so the outer transaction still decides the outcome. The runner does not
rewrite reviewed SQL to remove them; ``test_migration_runner.py`` asserts that a
failure part-way through such a file still leaves nothing behind.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MANIFEST_NAME = "0000_manifest.json"

#: ``0001_init.sql`` — four digits, a name, ``.sql``. Anything else is refused
#: rather than sorted somewhere surprising.
FILENAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class MigrationError(Exception):
    """A migration problem that must stop the run.

    ``filename`` is the file the caller has to look at, or ``None`` when the
    problem is with the history as a whole (a missing predecessor, a foreign
    schema). ``detail`` carries the machine-readable specifics for tests and for
    the operator log.
    """

    def __init__(self, message: str, *, filename: str | None = None,
                 kind: str = "migration_error", detail: dict[str, Any] | None = None):
        self.filename = filename
        self.kind = kind
        self.detail = detail or {}
        prefix = f"{filename}: " if filename else ""
        super().__init__(prefix + message)


@dataclass
class MigrationFile:
    """One migration on disk, with the effects the manifest says it must have."""

    name: str
    number: int
    path: Path
    checksum: str
    columns: list[tuple[str, str]] = field(default_factory=list)
    tables: dict[str, list[str]] = field(default_factory=dict)
    indexes: list[str] = field(default_factory=list)
    rls: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        """Whether this file declares anything the runner can check."""
        return bool(self.columns or self.tables or self.indexes or self.rls)


@dataclass
class AppliedMigration:
    filename: str
    checksum: str


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def default_directory() -> Path:
    return Path(__file__).resolve().parent.parent / "migrations"


def load_manifest(base: Path) -> dict[str, Any]:
    """Read the reviewed manifest, or fail loudly.

    A missing manifest is not "no verification required": an unverified run is
    exactly the state this module exists to end, so the runner refuses instead of
    degrading to the old behaviour.
    """
    path = base / MANIFEST_NAME
    if not path.is_file():
        raise MigrationError(
            f"manifest {MANIFEST_NAME} is missing; refusing to migrate without "
            "a reviewed statement of the expected files and their effects",
            filename=MANIFEST_NAME, kind="manifest_missing",
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MigrationError(
            f"manifest {MANIFEST_NAME} is not valid JSON: {exc}",
            filename=MANIFEST_NAME, kind="manifest_invalid",
        ) from exc


def discover(base: Path, manifest: dict[str, Any]) -> list[MigrationFile]:
    """Every migration the manifest lists, validated as a set.

    Checks, in this order, because each depends on the previous one being sane:

    * every ``*.sql`` on disk has a valid name and is listed in the manifest
      (an unlisted file is an unreviewed migration);
    * every manifest entry exists on disk (a lost step);
    * numbers are unique (the duplicate-``0011`` failure) and ascending.
    """
    listed: dict[str, Any] = manifest.get("files") or {}
    on_disk = {p.name: p for p in base.glob("*.sql")}

    unlisted = sorted(set(on_disk) - set(listed))
    if unlisted:
        raise MigrationError(
            "migration files present but not in the manifest: "
            + ", ".join(unlisted)
            + f"; add them to {MANIFEST_NAME} (with their checksums) before applying",
            filename=unlisted[0], kind="unlisted_file", detail={"files": unlisted},
        )
    missing = sorted(set(listed) - set(on_disk))
    if missing:
        raise MigrationError(
            "manifest lists migrations that are missing from the tree: "
            + ", ".join(missing),
            filename=missing[0], kind="missing_file", detail={"files": missing},
        )

    by_number: dict[int, list[str]] = {}
    files: list[MigrationFile] = []
    for name in sorted(on_disk):
        match = FILENAME_RE.match(name)
        if not match:
            raise MigrationError(
                "migration filename must be NNNN_name.sql",
                filename=name, kind="bad_filename",
            )
        number = int(match.group(1))
        by_number.setdefault(number, []).append(name)
        text = on_disk[name].read_text(encoding="utf-8")
        files.append(MigrationFile(
            name=name,
            number=number,
            path=on_disk[name],
            checksum=sha256_text(text),
            columns=[tuple(c) for c in (manifest.get("columns") or {}).get(name, [])],
            tables={
                table: list(cols)
                for table, cols in (manifest.get("table_columns") or {}).get(name, [])
            },
            indexes=list((manifest.get("indexes") or {}).get(name, [])),
            rls=list((manifest.get("rls") or {}).get(name, [])),
        ))

    duplicates = {n: names for n, names in by_number.items() if len(names) > 1}
    if duplicates:
        first = min(duplicates)
        raise MigrationError(
            "duplicate migration number "
            f"{first:04d}: {', '.join(sorted(duplicates[first]))}"
            "; numbers identify a step for the life of a database and cannot be reused",
            filename=sorted(duplicates[first])[1], kind="duplicate_number",
            detail={"numbers": {f"{n:04d}": names for n, names in duplicates.items()}},
        )
    files.sort(key=lambda f: f.number)
    return files


def validate_history(files: list[MigrationFile],
                     applied: list[AppliedMigration],
                     manifest: dict[str, Any]) -> None:
    """Compare what the database recorded against what the tree says.

    Raises on the first problem; the message names the file the operator has to
    look at. Nothing here writes anything, so a refused run leaves the database
    exactly as it was.
    """
    listed_checksums: dict[str, str] = manifest.get("files") or {}
    order = {f.name: i for i, f in enumerate(files)}
    seen: set[str] = set()

    for row in sorted(applied, key=lambda r: order.get(r.filename, len(order))):
        if row.filename not in order:
            raise MigrationError(
                "database records a migration that is not in this tree "
                f"({row.filename}); refusing to continue against an unknown history",
                filename=row.filename, kind="unknown_history",
            )
        position = order[row.filename]
        # Every earlier migration must be recorded too: a gap means a step was
        # skipped and the schema below it is not what later files assume.
        gaps = [f.name for f in files[:position] if f.name not in seen]
        if gaps:
            raise MigrationError(
                "applied history skips " + ", ".join(gaps)
                + f" but has {row.filename}; a later migration cannot be trusted "
                "on top of a missing predecessor",
                filename=gaps[0], kind="missing_predecessor",
                detail={"missing": gaps},
            )
        if row.checksum != listed_checksums.get(row.filename):
            if row.checksum != next(
                f.checksum for f in files if f.name == row.filename
            ):
                raise MigrationError(
                    "database applied a different body for this migration than the "
                    "manifest records; the file changed after it was applied. "
                    "Ship a new migration instead of editing an applied one.",
                    filename=row.filename, kind="checksum_changed",
                    detail={"recorded": row.checksum,
                            "manifest": listed_checksums.get(row.filename)},
                )
        seen.add(row.filename)

    gaps = [f.name for f in files if f.name not in seen and f.number < _highest(applied, order)]
    if gaps:
        raise MigrationError(
            "migrations missing from the applied history: " + ", ".join(gaps),
            filename=gaps[0], kind="missing_predecessor", detail={"missing": gaps},
        )


def _highest(applied: list[AppliedMigration], order: dict[str, int]) -> int:
    return max((order.get(r.filename, 0) for r in applied), default=-1)


def verify_effects(cur: Any, migration: MigrationFile) -> None:
    """Require the schema the migration claims to create. Called inside its tx.

    Raises ``MigrationError`` so the caller rolls the file back: a migration whose
    SQL "succeeded" without producing the effect is a failure, not a success.
    """
    for table, columns in migration.tables.items():
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (table,),
        )
        present = {r["column_name"] for r in cur.fetchall()}
        if not present:
            raise MigrationError(
                f"declared table {table} does not exist after the migration ran",
                filename=migration.name, kind="effect_missing",
                detail={"table": table, "missing_columns": columns},
            )
        absent = [c for c in columns if c not in present]
        if absent:
            raise MigrationError(
                f"table {table} is missing column(s) {', '.join(absent)} after the "
                "migration ran",
                filename=migration.name, kind="effect_missing",
                detail={"table": table, "missing_columns": absent},
            )
    for index in migration.indexes:
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() "
            "AND indexname = %s",
            (index,),
        )
        if cur.fetchone() is None:
            raise MigrationError(
                f"declared index {index} does not exist after the migration ran",
                filename=migration.name, kind="effect_missing",
                detail={"index": index},
            )
    for table in migration.rls:
        cur.execute(
            "SELECT relrowsecurity FROM pg_class WHERE relname = %s "
            "AND relnamespace = current_schema()::regnamespace",
            (table,),
        )
        row = cur.fetchone()
        if row is None or not row["relrowsecurity"]:
            raise MigrationError(
                f"row level security is not enabled on {table} after the "
                "migration ran",
                filename=migration.name, kind="effect_missing",
                detail={"table": table},
            )
    for table, column in migration.columns:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s "
            "AND column_name = %s",
            (table, column),
        )
        if cur.fetchone() is None:
            raise MigrationError(
                f"declared column {table}.{column} does not exist after the "
                "migration ran",
                filename=migration.name, kind="effect_missing",
                detail={"table": table, "column": column},
            )
