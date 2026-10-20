"""PostgreSQL (Supabase) task store for the Sr/Jr dispatcher.

Mirrors the semantics of :mod:`orchestration.control_plane` (the SQLite store)
but targets Supabase PostgreSQL, which is the durable store when the dispatcher
runs outside a single machine.

The parts that matter for correctness:

* **Atomic claim.** ``claim_task`` uses ``SELECT ... FOR UPDATE SKIP LOCKED``
  inside a transaction, so two dispatchers can run concurrently and never take
  the same task. This is what replaces the single-process lock the SQLite store
  relies on.
* **Leases.** A claim writes ``lease_expires``. A crashed dispatcher's lease
  expires and the task is reclaimed, bounded by ``max_attempts``.
* **Idempotent submit.** ``idempotency_key`` is UNIQUE; the loser of a race
  reads the winner's row instead of inserting a duplicate.
* **Idempotent dispatch.** ``conversations.task_id`` is UNIQUE, so a restart
  reuses the recorded conversation instead of starting a second one.

The connection string is read from ``SUPABASE_DB_URL`` (or ``DATABASE_URL``)
and never logged. The service-role credential must stay server-side.
"""

from __future__ import annotations

import json
import hashlib
import os
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:  # psycopg3
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - surfaced with a clear message below
    psycopg = None  # type: ignore
    dict_row = None  # type: ignore


class StoreError(RuntimeError):
    """Base class for store failures."""


class NotFound(StoreError):
    """The requested row does not exist."""


class InvalidTransition(StoreError):
    """The state machine does not allow this move."""


class LeaseConflict(StoreError):
    """Another owner holds a live lease on this task."""


class TaskState(str, Enum):
    """Same lifecycle as the SQLite store, so both stores are interchangeable."""

    QUEUED = "queued"
    ASSIGNED = "assigned"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECOVERING = "recovering"
    DEAD_LETTER = "dead_letter"


_ALLOWED: dict[TaskState, frozenset[TaskState]] = {
    TaskState.QUEUED: frozenset({TaskState.ASSIGNED, TaskState.CANCELLED, TaskState.BLOCKED}),
    TaskState.ASSIGNED: frozenset(
        {
            TaskState.RUNNING,
            TaskState.QUEUED,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.RECOVERING,
            TaskState.BLOCKED,
            TaskState.VERIFYING,
        }
    ),
    TaskState.RUNNING: frozenset(
        {
            TaskState.VERIFYING,
            TaskState.FAILED,
            TaskState.BLOCKED,
            TaskState.CANCELLED,
            TaskState.RECOVERING,
            TaskState.DEAD_LETTER,
        }
    ),
    TaskState.VERIFYING: frozenset(
        {TaskState.COMPLETED, TaskState.FAILED, TaskState.RUNNING, TaskState.BLOCKED, TaskState.DEAD_LETTER}
    ),
    TaskState.RECOVERING: frozenset(
        {
            TaskState.RUNNING,
            TaskState.QUEUED,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.BLOCKED,
            TaskState.VERIFYING,
            TaskState.COMPLETED,
            TaskState.DEAD_LETTER,
        }
    ),
    TaskState.BLOCKED: frozenset({TaskState.QUEUED, TaskState.CANCELLED}),
    TaskState.FAILED: frozenset({TaskState.QUEUED, TaskState.DEAD_LETTER, TaskState.CANCELLED}),
    TaskState.DEAD_LETTER: frozenset({TaskState.QUEUED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}

ACTIVE_STATES = frozenset(
    {
        TaskState.QUEUED,
        TaskState.ASSIGNED,
        TaskState.RUNNING,
        TaskState.VERIFYING,
        TaskState.RECOVERING,
        TaskState.BLOCKED,
        TaskState.FAILED,
    }
)
TERMINAL_STATES = frozenset({TaskState.COMPLETED, TaskState.CANCELLED})


@dataclass
class Task:
    id: str
    state: TaskState
    goal: str
    workflow_id: str | None
    parent_task_id: str | None
    owner: str | None
    lease_expires: float | None
    attempt: int
    max_attempts: int
    priority: int
    dependencies: list[str]
    requires_approval: bool
    approved: bool | None
    evidence: list[dict]
    result: str | None
    conversation_id: str | None
    selected_repository: str | None
    selected_branch: str | None
    updated_at: float

    @property
    def lease_live(self) -> bool:
        return self.lease_expires is not None and self.lease_expires > time.time()


def _new_id() -> str:
    return str(uuid.uuid4())


def _legal_path(frm: TaskState, to: TaskState) -> list[TaskState] | None:
    """Shortest legal sequence of transitions from *frm* to *to*.

    Some states cannot reach another directly (``assigned`` cannot jump to
    ``dead_letter`` or ``queued``). Callers that must force an outcome use this
    to walk a legal route instead of raising. Returns the steps *after* the
    current state, or None when no route exists.
    """
    if to in _ALLOWED[frm]:
        return [to]
    queue: list[tuple[TaskState, list[TaskState]]] = [(frm, [frm])]
    seen = {frm}
    while queue:
        node, path = queue.pop(0)
        for nxt in _ALLOWED[node]:
            if nxt is to:
                return path[1:] + [to]
            if nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, path + [nxt]))
    return None


class PostgresStore:
    """Durable task store on PostgreSQL / Supabase."""

    def __init__(self, dsn: str | None = None, *, connect_timeout: float = 10.0,
                 auto_migrate: bool = True):
        if psycopg is None:  # pragma: no cover
            raise StoreError("psycopg is required: pip install 'psycopg[binary]'")
        self.dsn = dsn or os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
        if not self.dsn:
            raise StoreError("SUPABASE_DB_URL is not set")
        self._connect_timeout = connect_timeout
        # A single connection is enough for one dispatcher process; the atomic
        # claim still protects against *other* processes. Callers needing more
        # concurrency can open one store per worker.
        #
        # Supabase's transaction pooler (PgBouncer, port 6543) hands a server
        # connection to a different client between statements, so a server-side
        # prepared statement can be missing on the next execute. Disabling
        # psycopg's automatic preparation keeps every statement unparameterised
        # on the server. The default (5) prepares a statement after its fifth
        # execution, which fails behind the pooler with
        # "prepared statement ... does not exist".
        pooler = os.environ.get("DATABASE_TRANSACTION_POOLER", "").lower() in ("1", "true", "yes")
        connect_kwargs: dict[str, Any] = {
            "row_factory": dict_row,
            "autocommit": False,
            "connect_timeout": connect_timeout,
        }
        if pooler:
            connect_kwargs["prepare_threshold"] = None
        self._conn = psycopg.connect(self.dsn, **connect_kwargs)
        # psycopg connections are not thread-safe, but the web server answers
        # each request on its own thread while sharing this one connection.
        # Without serializing access, two threads interleave on one transaction
        # and the loser sees "current transaction is aborted".
        self._lock = threading.RLock()
        # Render's free plan has no shell to run ``psql`` in, so the schema must
        # be applied by the process itself. The migrations are idempotent, and
        # ``AUTO_MIGRATE=false`` opts out for environments that apply them out of
        # band (a release step, a DBA).
        if auto_migrate and os.environ.get("AUTO_MIGRATE", "true").lower() not in ("0", "false", "no"):
            self.apply_migrations()

    # ------------------------------------------------------------ migrations
    def apply_migrations(self, directory: str | None = None) -> list[str]:
        """Apply every ``*.sql`` migration in order, skipping applied ones.

        Returns the names of the files actually run. A file whose checksum was
        already recorded is skipped, so a restart does not re-execute it; the
        SQL is idempotent regardless, so a partially-applied run still converges.

        Each file runs through ``_tx``, so a file that fails is rolled back and
        the original SQL error reaches the caller instead of leaving the shared
        connection aborted.
        """
        base = Path(directory) if directory else Path(__file__).resolve().parent.parent / "migrations"
        files = sorted(base.glob("*.sql"))
        if not files:
            return []
        with self._tx() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "  filename text PRIMARY KEY,"
                "  checksum text NOT NULL,"
                "  applied_at timestamptz NOT NULL DEFAULT now())"
            )
        applied: list[str] = []
        for path in files:
            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            with self._tx() as cur:
                cur.execute("SELECT checksum FROM schema_migrations WHERE filename = %s",
                            (path.name,))
                row = cur.fetchone()
                if row is not None and row["checksum"] == checksum:
                    continue
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations(filename, checksum) VALUES (%s, %s) "
                    "ON CONFLICT (filename) DO UPDATE SET checksum = EXCLUDED.checksum,"
                    " applied_at = now()",
                    (path.name, checksum),
                )
            applied.append(path.name)
        return applied

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    #: Columns the code reads or writes, per table. Used to detect a database
    #: that has the right table names but the wrong shape - typically one created
    #: by the other implementation of this dispatcher. Only the columns a foreign
    #: schema is likely to be missing are listed; the goal is a clear message,
    #: not a copy of the DDL.
    REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
        "workflows": ("id", "goal", "state", "created_at", "updated_at",
                      "sr_conversation_id", "sr_attempt", "sr_last_error",
                      "sr_next_attempt_at", "idempotency_key",
                      "sr_start_lease_expires"),
        "repositories": ("id", "full_name", "default_branch", "allowed_branches",
                         "enabled"),
        "tasks": ("id", "workflow_id", "goal", "state", "priority", "owner",
                  "lease_expires", "attempt", "max_attempts", "dependencies"),
        "conversations": ("id", "task_id", "external_conversation_id", "status"),
        "task_events": ("seq", "at", "kind", "task_id", "workflow_id"),
        "sr_tool_calls": ("event_id", "workflow_id", "name", "args", "claimed_at",
                          "attempts", "delivered_at"),
        "sessions": ("token_hash", "username", "csrf_token", "expires_at"),
        "login_attempts": ("key", "failures", "locked_until"),
    }

    def verify_schema(self) -> list[str]:
        """Return the tables the store needs that the database is missing.

        A missing table is a real root cause of ``UndefinedTable``, which in turn
        produced the reported ``InFailedSqlTransaction`` cascade. Checking at
        startup turns that into one clear message instead of a 500 on the first
        request (a forgotten migration is the usual cause).

        A table that exists with the *wrong columns* is reported the same way:
        ``CREATE TABLE IF NOT EXISTS`` silently does nothing when the table is
        already there, so a database created by the other implementation keeps
        its own shape and fails later with "column ... does not exist". Such a
        table is returned as ``table(column)`` so the name that matters is
        visible. This method only reads; it never alters the schema.
        """
        required = (
            "workflows", "tasks", "conversations", "repositories",
            "task_events", "sr_tool_calls", "sessions", "login_attempts",
        )
        with self._tx() as cur:
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema()"
            )
            columns: dict[str, set[str]] = {}
            for row in cur.fetchall():
                columns.setdefault(row["table_name"], set()).add(row["column_name"])

        missing: list[str] = []
        for table in required:
            if table not in columns:
                missing.append(table)
                continue
            for column in self.REQUIRED_COLUMNS.get(table, ()):
                if column not in columns[table]:
                    missing.append(f"{table}({column})")
        return missing

    # ---------------------------------------------------------------- infra
    @contextmanager
    def _tx(self) -> Iterator[Any]:
        """Yield a cursor inside a transaction, committing or rolling back.

        The lock makes the shared connection safe under the threaded server.
        The rollback on entry recovers from a transaction that was already
        aborted (e.g. by a previous failure) instead of propagating
        ``InFailedSqlTransaction`` into an unrelated caller.
        """
        with self._lock:
            conn = self._conn
            if conn.info.transaction_status == psycopg.pq.TransactionStatus.INERROR:
                conn.rollback()
            try:
                with conn.cursor() as cur:
                    yield cur
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _event(
        self,
        cur: Any,
        kind: str,
        *,
        task_id: str | None = None,
        workflow_id: str | None = None,
        session_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        cur.execute(
            "INSERT INTO task_events(kind, task_id, workflow_id, session_id, detail) "
            "VALUES (%s,%s,%s,%s,%s)",
            (kind, task_id, workflow_id, session_id, json.dumps(detail or {})),
        )

    # ------------------------------------------------------------ workflows
    def create_workflow(self, goal: str, *, workflow_id: str | None = None,
                        metadata: dict[str, Any] | None = None,
                        idempotency_key: str | None = None) -> str:
        """Create a workflow, or return the one a previous call already made.

        When *idempotency_key* is given the insert is guarded by the UNIQUE
        index on ``workflows.idempotency_key``: a retried request (a client
        timeout, a double submit, a proxy retry) reuses the existing workflow
        instead of minting a second one, and therefore reuses its Sr
        conversation instead of starting another. The check is in the database,
        so it holds even when two requests arrive at the same instant.

        Returns the workflow id. Use :meth:`create_workflow_once` when the
        caller needs to know whether it created the row.
        """
        wid, _ = self.create_workflow_once(
            goal, workflow_id=workflow_id, metadata=metadata,
            idempotency_key=idempotency_key,
        )
        return wid

    def create_workflow_once(self, goal: str, *, workflow_id: str | None = None,
                             metadata: dict[str, Any] | None = None,
                             idempotency_key: str | None = None) -> tuple[str, bool]:
        """Create-or-reuse a workflow, returning ``(workflow_id, created)``.

        ``created`` is False when an existing workflow was reused, which is the
        signal that the caller must not start a second Sr conversation.
        """
        wid = workflow_id or _new_id()
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO workflows(id, goal, metadata, idempotency_key)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING
                """,
                (wid, goal, json.dumps(metadata or {}), idempotency_key),
            )
            created = cur.rowcount > 0
            if not created:
                # Same id already present: treat as reuse.
                return wid, False
            self._event(cur, "workflow_created", workflow_id=wid, detail={"goal": goal})
        return wid, True

    def workflow_by_idempotency(self, key: str) -> dict[str, Any] | None:
        """The workflow a previous call created for *key*, if any."""
        with self._tx() as cur:
            cur.execute(
                "SELECT id, goal, state, sr_conversation_id, sr_status "
                "FROM workflows WHERE idempotency_key=%s",
                (key,),
            )
            row = cur.fetchone()
        if row is not None:
            row["id"] = str(row["id"])
        return row

    def create_workflow_idempotent(self, goal: str, key: str, *,
                                   metadata: dict[str, Any] | None = None
                                   ) -> tuple[str, bool]:
        """Atomically create-or-reuse a workflow for *key*.

        Unlike :meth:`create_workflow_once` this does not rely on a pre-generated
        id: two concurrent requests with the same key race on the UNIQUE index,
        the loser reads the winner's row, and exactly one workflow results. That
        is the property a retried HTTP request needs.
        """
        wid = _new_id()
        try:
            with self._tx() as cur:
                cur.execute(
                    """
                    INSERT INTO workflows(id, goal, metadata, idempotency_key)
                    VALUES (%s,%s,%s,%s)
                    """,
                    (wid, goal, json.dumps(metadata or {}), key),
                )
                self._event(cur, "workflow_created", workflow_id=wid,
                            detail={"goal": goal, "idempotency_key": key})
            return wid, True
        except psycopg.errors.UniqueViolation:
            existing = self.workflow_by_idempotency(key)
            if existing is None:
                raise
            return existing["id"], False

    # --------------------------------------------------------- repositories
    def add_repository(self, full_name: str, *, provider: str = "github",
                       default_branch: str = "main",
                       allowed_branches: list[str] | None = None,
                       repo_id: str | None = None) -> str:
        rid = repo_id or _new_id()
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO repositories(id, provider, full_name, default_branch,
                                         allowed_branches)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (full_name) DO UPDATE
                  SET allowed_branches = EXCLUDED.allowed_branches,
                      default_branch   = EXCLUDED.default_branch,
                      provider         = EXCLUDED.provider
                RETURNING id
                """,
                (rid, provider, full_name, default_branch,
                 allowed_branches or []),
            )
            rid = cur.fetchone()["id"]
        return rid

    def remove_repository(self, full_name: str) -> bool:
        """Delete a repository from the allowlist. Returns whether a row went."""
        with self._tx() as cur:
            cur.execute(
                "DELETE FROM repositories WHERE full_name=%s", (full_name,)
            )
            return cur.rowcount > 0

    def sync_repositories(self, repos: list[dict[str, Any]]) -> dict[str, Any]:
        """Upsert discovered repositories in one transaction.

        Existing rows keep their ``allowed_branches`` and ``enabled`` flag: a
        sync must not silently re-enable a repository an operator turned off,
        nor drop a branch restriction they set. Only the fields GitHub is
        authoritative about (default branch, provider) are refreshed.
        """
        added = updated = 0
        with self._tx() as cur:
            for repo in repos:
                full_name = repo["full_name"]
                cur.execute(
                    "SELECT id, default_branch FROM repositories WHERE full_name=%s",
                    (full_name,),
                )
                existing = cur.fetchone()
                if existing is None:
                    cur.execute(
                        "INSERT INTO repositories(id, provider, full_name, "
                        "default_branch, allowed_branches) VALUES (%s,%s,%s,%s,%s)",
                        (_new_id(), repo.get("provider", "github"), full_name,
                         repo.get("default_branch") or "main", []),
                    )
                    added += 1
                    continue
                if existing["default_branch"] != repo.get("default_branch"):
                    cur.execute(
                        "UPDATE repositories SET default_branch=%s, provider=%s "
                        "WHERE full_name=%s",
                        (repo.get("default_branch") or "main",
                         repo.get("provider", "github"), full_name),
                    )
                    updated += 1
        return {"added": added, "updated": updated, "total": len(repos)}

    def allowed_repository(self, full_name: str) -> dict[str, Any] | None:
        with self._tx() as cur:
            cur.execute(
                "SELECT * FROM repositories WHERE full_name=%s AND enabled",
                (full_name,),
            )
            row = cur.fetchone()
        return row

    # ------------------------------------------------------------------ tasks
    def submit_task(
        self,
        goal: str,
        *,
        task_id: str | None = None,
        workflow_id: str | None = None,
        parent_task_id: str | None = None,
        request: str = "",
        acceptance: str = "",
        priority: int = 0,
        dependencies: list[str] | None = None,
        max_attempts: int = 3,
        idempotency_key: str | None = None,
        requires_approval: bool = False,
        selected_repository: str | None = None,
        selected_branch: str | None = None,
    ) -> Task:
        tid = task_id or _new_id()
        try:
            with self._tx() as cur:
                cur.execute(
                    """
                    INSERT INTO tasks(id, workflow_id, parent_task_id, goal, request,
                                      acceptance, state, priority, dependencies,
                                      max_attempts, idempotency_key, requires_approval,
                                      selected_repository, selected_branch)
                    VALUES (%s,%s,%s,%s,%s,%s,'queued',%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        tid, workflow_id, parent_task_id, goal, request, acceptance,
                        priority, json.dumps(dependencies or []), max_attempts,
                        idempotency_key, requires_approval,
                        selected_repository, selected_branch,
                    ),
                )
                self._event(cur, "task_submitted", task_id=tid,
                            workflow_id=workflow_id, detail={"goal": goal})
        except psycopg.errors.UniqueViolation:
            if idempotency_key is None:
                raise
            existing = self._by_idempotency(idempotency_key)
            if existing is None:
                raise
            return existing
        return self.get_task(tid)

    def force_transition(self, task_id: str, to: TaskState, *, owner: str | None = None,
                         result: str | None = None,
                         evidence: list[dict] | None = None) -> Task:
        """Move a task to *to*, walking a legal path if no direct edge exists.

        Recovery and requeue paths need to force an outcome (dead-letter, back
        to queue) from states that have no direct edge. The store still refuses
        to invent an impossible move: if no legal route exists it raises.
        """
        current = self.get_task(task_id).state
        path = _legal_path(current, to)
        if path is None:
            raise InvalidTransition(f"no legal path {current.value} -> {to.value}")
        task: Task | None = None
        for step in path:
            task = self.transition(task_id, step, owner=owner, result=result,
                                   evidence=evidence if step is path[-1] else None)
        assert task is not None
        return task

    def _by_idempotency(self, key: str) -> Task | None:
        with self._tx() as cur:
            cur.execute("SELECT * FROM tasks WHERE idempotency_key=%s", (key,))
            row = cur.fetchone()
        return _row_to_task(row) if row else None

    def get_task(self, task_id: str) -> Task:
        with self._tx() as cur:
            cur.execute("SELECT * FROM tasks WHERE id=%s", (task_id,))
            row = cur.fetchone()
        if row is None:
            raise NotFound(f"task {task_id}")
        return _row_to_task(row)

    def try_get_task(self, task_id: str) -> Task | None:
        try:
            return self.get_task(task_id)
        except NotFound:
            return None

    def list_tasks(self, *, workflow_id: str | None = None,
                   parent_task_id: str | None = None,
                   state: TaskState | None = None,
                   owner: str | None = None) -> list[Task]:
        sql = "SELECT * FROM tasks"
        where: list[str] = []
        params: list[Any] = []
        if workflow_id is not None:
            where.append("workflow_id=%s"); params.append(workflow_id)
        if parent_task_id is not None:
            where.append("parent_task_id=%s"); params.append(parent_task_id)
        if state is not None:
            where.append("state=%s"); params.append(state.value)
        if owner is not None:
            where.append("owner=%s"); params.append(owner)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY priority DESC, created_at"
        with self._tx() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        return [_row_to_task(r) for r in rows]

    def claim_task(self, owner: str, *, lease_seconds: float = 300.0,
                   task_id: str | None = None) -> Task | None:
        """Atomically claim one ready task for *owner*.

        ``FOR UPDATE SKIP LOCKED`` means two dispatchers running at the same
        time never claim the same row: the second skips a row the first has
        locked. Returns None when nothing is ready.

        **One task per repository+branch at a time.** Two conversations editing
        the same checkout would clobber each other's commits, and the loser's
        work would vanish with no error. A candidate whose repository and branch
        are already held by another non-terminal task is therefore skipped (or,
        for an explicit *task_id*, refused), so the second task waits its turn.
        Tasks with no repository are analysis-only and never contend.
        """
        with self._tx() as cur:
            if task_id is not None:
                cur.execute(
                    "SELECT * FROM tasks WHERE id=%s FOR UPDATE", (task_id,)
                )
                row = cur.fetchone()
                if row is None:
                    return None
                task = _row_to_task(row)
                if task.owner is not None and task.lease_live and task.owner != owner:
                    raise LeaseConflict(f"task {task.id} leased by {task.owner}")
                unmet = self._unmet_dependencies(cur, task)
                if unmet:
                    raise InvalidTransition(f"unmet dependencies: {unmet}")
                if task.requires_approval and task.approved is not True:
                    raise InvalidTransition("approval required")
                holder = self._workspace_holder(cur, task)
                if holder is not None:
                    raise InvalidTransition(
                        f"workspace {task.selected_repository}"
                        f"@{task.selected_branch or 'default'} is held by {holder}"
                    )
            else:
                # Scan candidates in order, skipping any whose workspace is busy,
                # so one contended repository does not stall the whole queue.
                tried: list[str] = []
                while True:
                    cur.execute(
                        """
                        SELECT * FROM tasks
                         WHERE state IN ('queued','recovering','failed')
                           AND (lease_expires IS NULL OR lease_expires < now())
                           AND id::text <> ALL(%s)
                         ORDER BY priority DESC, created_at
                         FOR UPDATE SKIP LOCKED
                         LIMIT 1
                        """,
                        (tried,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return None
                    task = _row_to_task(row)
                    if task.owner is not None and task.lease_live and task.owner != owner:
                        raise LeaseConflict(f"task {task.id} leased by {task.owner}")
                    unmet = self._unmet_dependencies(cur, task)
                    if unmet:
                        raise InvalidTransition(f"unmet dependencies: {unmet}")
                    if task.requires_approval and task.approved is not True:
                        raise InvalidTransition("approval required")
                    if self._workspace_holder(cur, task) is None:
                        break
                    tried.append(task.id)
            cur.execute(
                "UPDATE tasks SET state='assigned', owner=%s, "
                "lease_expires=now() + make_interval(secs => %s), updated_at=now() "
                "WHERE id=%s",
                (owner, lease_seconds, task.id),
            )
            self._event(cur, "task_claimed", task_id=task.id, session_id=owner,
                        detail={"lease_seconds": lease_seconds})
        return self.get_task(task.id)

    def _workspace_holder(self, cur: Any, task: Task) -> str | None:
        """The id of a task already occupying *task*'s workspace.

        ``None`` means the workspace is free (or the task needs none). Called
        inside the claiming transaction so the answer cannot go stale between the
        check and the claim.

        Only states that mean "a conversation may be editing this checkout right
        now" count: a merely ``queued`` sibling holds nothing, so two queued
        tasks on one repository do not block each other before either starts.
        """
        if not task.selected_repository:
            return None
        cur.execute(
            """
            SELECT id FROM tasks
             WHERE id <> %s
               AND selected_repository = %s
               AND selected_branch IS NOT DISTINCT FROM %s
               AND state IN ('assigned','running','verifying','blocked','recovering')
             LIMIT 1
            """,
            (task.id, task.selected_repository, task.selected_branch),
        )
        row = cur.fetchone()
        return str(row["id"]) if row is not None else None

    def _unmet_dependencies(self, cur: Any, task: Task) -> list[str]:
        unmet: list[str] = []
        for dep in task.dependencies:
            cur.execute("SELECT state FROM tasks WHERE id=%s", (dep,))
            row = cur.fetchone()
            if row is None or row["state"] != TaskState.COMPLETED.value:
                unmet.append(dep)
        return unmet

    def transition(self, task_id: str, to: TaskState, *, owner: str | None = None,
                   result: str | None = None,
                   evidence: list[dict] | None = None) -> Task:
        with self._tx() as cur:
            cur.execute("SELECT * FROM tasks WHERE id=%s FOR UPDATE", (task_id,))
            row = cur.fetchone()
            if row is None:
                raise NotFound(f"task {task_id}")
            task = _row_to_task(row)
            if to not in _ALLOWED[task.state]:
                raise InvalidTransition(f"{task.state.value} -> {to.value}")
            if owner is not None and task.owner is not None and task.owner != owner:
                raise LeaseConflict(f"task {task_id} owned by {task.owner}")

            new_owner = task.owner
            clear_lease = False
            attempt = task.attempt
            if to is TaskState.QUEUED:
                new_owner = None; clear_lease = True
            if to is TaskState.FAILED:
                attempt = task.attempt + 1; new_owner = None; clear_lease = True
            if to is TaskState.DEAD_LETTER:
                new_owner = None; clear_lease = True

            new_evidence = list(task.evidence)
            if evidence:
                new_evidence.extend(evidence)

            cur.execute(
                """
                UPDATE tasks SET state=%s, owner=%s,
                       lease_expires = CASE WHEN %s THEN NULL ELSE lease_expires END,
                       attempt=%s, result=COALESCE(%s, result),
                       evidence=%s, updated_at=now()
                 WHERE id=%s
                """,
                (to.value, new_owner, clear_lease, attempt, result,
                 json.dumps(new_evidence), task_id),
            )
            self._event(cur, "task_transition", task_id=task_id, session_id=new_owner,
                        detail={"from": task.state.value, "to": to.value})
        return self.get_task(task_id)

    def bind_conversation(self, task_id: str, external_conversation_id: str | None,
                          *, start_task_id: str | None = None,
                          status: str = "starting", url: str | None = None,
                          sandbox_id: str | None = None) -> None:
        """Record the conversation for a task, or update it in place.

        ``conversations.task_id`` is UNIQUE, so a restart updates the same row
        instead of creating a second conversation.
        """
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO conversations(id, task_id, external_conversation_id,
                                          external_start_task_id, status, url, sandbox_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (task_id) DO UPDATE
                   SET external_conversation_id = COALESCE(EXCLUDED.external_conversation_id,
                                                           conversations.external_conversation_id),
                       external_start_task_id   = COALESCE(EXCLUDED.external_start_task_id,
                                                           conversations.external_start_task_id),
                       status = EXCLUDED.status,
                       url    = COALESCE(EXCLUDED.url, conversations.url),
                       sandbox_id = COALESCE(EXCLUDED.sandbox_id, conversations.sandbox_id),
                       updated_at = now()
                """,
                (_new_id(), task_id, external_conversation_id, start_task_id, status, url, sandbox_id),
            )
            self._event(cur, "conversation_bound", task_id=task_id,
                        detail={"external_conversation_id": external_conversation_id,
                                "start_task_id": start_task_id})

    def conversation_for(self, task_id: str) -> dict[str, Any] | None:
        with self._tx() as cur:
            cur.execute("SELECT * FROM conversations WHERE task_id=%s", (task_id,))
            row = cur.fetchone()
        return row

    def set_result(self, task_id: str, result: str) -> Task:
        with self._tx() as cur:
            cur.execute(
                "UPDATE tasks SET result=%s, updated_at=now() WHERE id=%s",
                (result, task_id),
            )
            if cur.rowcount == 0:
                raise NotFound(f"task {task_id}")
        return self.get_task(task_id)

    def approve(self, task_id: str, *, approved: bool) -> Task:
        with self._tx() as cur:
            cur.execute(
                "UPDATE tasks SET approved=%s, updated_at=now() WHERE id=%s",
                (approved, task_id),
            )
            if cur.rowcount == 0:
                raise NotFound(f"task {task_id}")
            self._event(cur, "task_approval", task_id=task_id,
                        detail={"approved": approved})
        return self.get_task(task_id)

    def renew_lease(self, task_id: str, owner: str, *,
                    lease_seconds: float = 300.0) -> Task:
        with self._tx() as cur:
            cur.execute("SELECT * FROM tasks WHERE id=%s FOR UPDATE", (task_id,))
            row = cur.fetchone()
            if row is None:
                raise NotFound(f"task {task_id}")
            if row["owner"] != owner:
                raise LeaseConflict(f"task {task_id} not owned by {owner}")
            if row["state"] not in (TaskState.ASSIGNED.value, TaskState.RUNNING.value):
                raise InvalidTransition(f"cannot renew in state {row['state']}")
            cur.execute(
                "UPDATE tasks SET lease_expires=now() + make_interval(secs => %s), "
                "updated_at=now() WHERE id=%s",
                (lease_seconds, task_id),
            )
        return self.get_task(task_id)

    # -------------------------------------------------------------- recovery
    def expired_leases(self) -> list[Task]:
        with self._tx() as cur:
            cur.execute(
                """
                SELECT * FROM tasks
                 WHERE state IN ('assigned','running','recovering')
                   AND (lease_expires IS NULL OR lease_expires < now())
                """
            )
            rows = cur.fetchall()
        return [_row_to_task(r) for r in rows]

    def reclaim_expired(self, *, max_attempts: int | None = None) -> list[dict]:
        actions: list[dict] = []
        for task in self.expired_leases():
            if task.state is TaskState.RECOVERING:
                continue
            limit = task.max_attempts if max_attempts is None else max_attempts
            if task.attempt >= limit:
                self.force_transition(task.id, TaskState.DEAD_LETTER)
                actions.append({"task": task.id, "action": "dead_letter"})
            else:
                self.force_transition(task.id, TaskState.RECOVERING)
                actions.append({"task": task.id, "action": "recovering"})
        return actions

    def reconcile(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "reclaimed": self.reclaim_expired(),
            "blocked_on_dependencies": [],
            "awaiting_approval": [],
            "dead_letter": [],
            "resumable": [],
        }
        for task in self.list_tasks():
            if task.state in TERMINAL_STATES:
                continue
            if task.state is TaskState.QUEUED:
                unmet = [
                    d for d in task.dependencies
                    if (t := self.try_get_task(d)) is None
                    or t.state is not TaskState.COMPLETED
                ]
                if unmet:
                    report["blocked_on_dependencies"].append(task.id)
                elif task.requires_approval and task.approved is not True:
                    report["awaiting_approval"].append(task.id)
                else:
                    report["resumable"].append(task.id)
            if task.state is TaskState.DEAD_LETTER:
                report["dead_letter"].append(task.id)
        return report

    def events(self, *, task_id: str | None = None,
               workflow_id: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM task_events"
        where: list[str] = []
        params: list[Any] = []
        if task_id is not None:
            where.append("task_id=%s"); params.append(task_id)
        if workflow_id is not None:
            where.append("workflow_id=%s"); params.append(workflow_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq DESC LIMIT %s"
        params.append(limit)
        with self._tx() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        return rows

    # ------------------------------------------------------------- Sr runtime
    def bind_sr(
        self,
        workflow_id: str,
        *,
        conversation_id: str | None = None,
        start_task_id: str | None = None,
        status: str | None = None,
    ) -> None:
        """Record the Sr conversation for a workflow (one Sr per workflow)."""
        with self._tx() as cur:
            cur.execute(
                """
                UPDATE workflows
                   SET sr_conversation_id = COALESCE(%s, sr_conversation_id),
                       sr_start_task_id   = COALESCE(%s, sr_start_task_id),
                       sr_status          = COALESCE(%s, sr_status),
                       sr_start_owner     = NULL,
                       sr_start_lease_expires = NULL,
                       updated_at = now()
                 WHERE id = %s
                """,
                (conversation_id, start_task_id, status, workflow_id),
            )
            if cur.rowcount == 0:
                raise NotFound(f"workflow {workflow_id}")
            self._event(cur, "sr_bound", workflow_id=workflow_id,
                        detail={"conversation_id": conversation_id,
                                "start_task_id": start_task_id, "status": status})

    def sr_for(self, workflow_id: str) -> dict[str, Any] | None:
        with self._tx() as cur:
            cur.execute(
                "SELECT sr_conversation_id, sr_start_task_id, sr_status, goal, state, "
                "sr_attempt, sr_last_error, sr_next_attempt_at "
                "FROM workflows WHERE id=%s",
                (workflow_id,),
            )
            row = cur.fetchone()
        return row

    def pending_sr_starts(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """Workflows that have no Sr yet and are due for another start attempt.

        A workflow whose Sr start failed transiently (429, 5xx, network) is left
        in ``queued`` with no conversation. Nothing else revisits it, so the loop
        scans for these. ``sr_next_attempt_at IS NULL`` means "never attempted";
        a future timestamp is a backoff that has not elapsed.

        A workflow whose ``sr_start_lease_expires`` is still in the future is
        being started by another worker right now, so it is excluded: without
        that, two concurrent ticks would both start Sr and create two
        conversations for one workflow.

        ``blocked`` is excluded too. That is the state ``record_sr_failure``
        writes when it gives up, and it leaves ``sr_next_attempt_at`` NULL, so
        without this the scan would re-select a permanently failed workflow
        forever and keep trying to start conversations for it. A blocked
        workflow is waiting for a human.
        """
        with self._tx() as cur:
            cur.execute(
                """
                SELECT id, goal, sr_attempt, sr_last_error
                  FROM workflows
                 WHERE sr_conversation_id IS NULL
                   AND state NOT IN ('completed', 'cancelled', 'failed',
                                     'blocked')
                   AND (sr_next_attempt_at IS NULL OR sr_next_attempt_at <= now())
                   AND (sr_start_lease_expires IS NULL
                        OR sr_start_lease_expires < now())
                 ORDER BY created_at
                 LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        for row in rows:
            row["id"] = str(row["id"])
        return rows

    def claim_sr_start(self, workflow_id: str, owner: str, *,
                       lease_seconds: float = 120.0) -> bool:
        """Atomically reserve the right to start Sr for *workflow_id*.

        Returns True when this caller may start the conversation. ``FOR UPDATE
        SKIP LOCKED`` plus the lease means two concurrent ticks cannot both win:
        the second sees a live lease and returns False, so exactly one Sr
        conversation is created per workflow. The lease expires, so a crash
        between claiming and starting does not strand the workflow.
        """
        with self._tx() as cur:
            cur.execute(
                """
                SELECT id FROM workflows
                 WHERE id=%s
                   AND sr_conversation_id IS NULL
                   AND (sr_start_lease_expires IS NULL
                        OR sr_start_lease_expires < now())
                 FOR UPDATE SKIP LOCKED
                """,
                (workflow_id,),
            )
            if cur.fetchone() is None:
                return False
            cur.execute(
                "UPDATE workflows SET sr_start_owner=%s, "
                "sr_start_lease_expires = now() + make_interval(secs => %s), "
                "updated_at = now() WHERE id=%s",
                (owner, lease_seconds, workflow_id),
            )
        return True

    def release_sr_start(self, workflow_id: str) -> None:
        """Drop the start lease so another worker may retry immediately."""
        with self._tx() as cur:
            cur.execute(
                "UPDATE workflows SET sr_start_owner=NULL, "
                "sr_start_lease_expires=NULL, updated_at=now() WHERE id=%s",
                (workflow_id,),
            )

    def record_sr_failure(
        self,
        workflow_id: str,
        *,
        error: str,
        attempt: int,
        backoff_seconds: float,
        give_up: bool,
    ) -> None:
        """Record a failed Sr start and schedule the next attempt.

        ``attempt`` is the number just used. When ``give_up`` is set the workflow
        is moved to ``blocked`` with the error as evidence: a human must look,
        because retrying forever would hide a permanent fault (a bad key, a
        repository that no longer exists).
        """
        from datetime import datetime, timedelta, timezone

        next_at = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)
        with self._tx() as cur:
            cur.execute(
                """
                UPDATE workflows
                   SET sr_attempt = %s,
                       sr_last_error = %s,
                       sr_next_attempt_at = %s,
                       sr_status = %s,
                       updated_at = now()
                 WHERE id = %s
                """,
                (attempt, error[:2000], None if give_up else next_at,
                 "failed" if give_up else "retrying", workflow_id),
            )
            if cur.rowcount == 0:
                raise NotFound(f"workflow {workflow_id}")
            self._event(cur, "sr_start_failed", workflow_id=workflow_id,
                        detail={"attempt": attempt, "error": error[:500],
                                "give_up": give_up})
            if give_up:
                cur.execute(
                    "UPDATE workflows SET state='blocked', updated_at=now() "
                    "WHERE id=%s AND state NOT IN ('completed','cancelled')",
                    (workflow_id,),
                )
                self._event(cur, "workflow_state", workflow_id=workflow_id,
                            detail={"state": "blocked",
                                    "reason": "Sr could not be started"})

    def mark_sr_started(self, workflow_id: str) -> None:
        """Clear the retry state once an Sr conversation exists."""
        with self._tx() as cur:
            cur.execute(
                """
                UPDATE workflows
                   SET sr_attempt = 0, sr_last_error = NULL,
                       sr_next_attempt_at = NULL, updated_at = now()
                 WHERE id = %s
                """,
                (workflow_id,),
            )
            if cur.rowcount == 0:
                raise NotFound(f"workflow {workflow_id}")

    def list_workflows(self) -> list[dict[str, Any]]:
        with self._tx() as cur:
            cur.execute(
                "SELECT id, goal, state, sr_conversation_id, sr_status, created_at "
                "FROM workflows ORDER BY created_at"
            )
            rows = cur.fetchall()
        # ``id`` comes back as a uuid.UUID; callers compare it with string ids
        # (e.g. the workflow-scope check), so normalize it to str here.
        for row in rows:
            row["id"] = str(row["id"])
        return rows

    def list_repositories(self, *, enabled_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT id, provider, full_name, default_branch, allowed_branches, enabled FROM repositories"
        if enabled_only:
            sql += " WHERE enabled"
        sql += " ORDER BY full_name"
        with self._tx() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return rows

    def claim_tool_call(
        self,
        event_id: str,
        *,
        workflow_id: str | None,
        name: str,
        args: dict[str, Any],
        lease_seconds: float = 120.0,
        max_attempts: int = 5,
    ) -> bool:
        """Atomically claim a client-tool call for execution.

        Returns True when this caller may run the tool, False when another poll
        already handled the same Cloud event id.

        This is a *lease*, not a one-shot flag. The ``event_id`` primary key makes
        the common case execute once; if the process crashes after the claim and
        before :meth:`finish_tool_call`, the row is left unfinished and its
        ``claimed_at`` goes stale, so a later poll re-claims it (bounded by
        ``max_attempts``). That turns permanent event loss into at-least-once
        execution, which is why the operations it calls must be idempotent.
        """
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO sr_tool_calls(event_id, workflow_id, name, args,
                                          claimed_at, attempts)
                VALUES (%s,%s,%s,%s, now(), 1)
                ON CONFLICT (event_id) DO UPDATE
                   SET claimed_at = now(),
                       attempts   = sr_tool_calls.attempts + 1
                 WHERE sr_tool_calls.finished_at IS NULL
                   AND sr_tool_calls.claimed_at < now() - make_interval(secs => %s)
                   AND sr_tool_calls.attempts < %s
                RETURNING event_id
                """,
                (event_id, workflow_id, name, json.dumps(args),
                 lease_seconds, max_attempts),
            )
            row = cur.fetchone()
            if row is None:
                return False
            self._event(cur, "sr_tool_call_claimed", workflow_id=workflow_id,
                        detail={"event_id": event_id, "name": name})
        return True

    def tool_call(self, event_id: str) -> dict[str, Any] | None:
        with self._tx() as cur:
            cur.execute("SELECT * FROM sr_tool_calls WHERE event_id=%s", (event_id,))
            row = cur.fetchone()
        return row

    def finish_tool_call(
        self,
        event_id: str,
        *,
        result: dict[str, Any],
        is_error: bool = False,
        task_id: str | None = None,
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                """
                UPDATE sr_tool_calls
                   SET result=%s, is_error=%s, task_id=COALESCE(%s, task_id),
                       finished_at=now()
                 WHERE event_id=%s
                """,
                (json.dumps(result, default=str), is_error, task_id, event_id),
            )
            self._event(cur, "sr_tool_call_finished", task_id=task_id,
                        detail={"event_id": event_id, "is_error": is_error})

    def mark_delivered(self, event_id: str) -> None:
        """Record that the result reached the Sr conversation."""
        with self._tx() as cur:
            cur.execute(
                "UPDATE sr_tool_calls SET delivered_at=now() WHERE event_id=%s",
                (event_id,),
            )
            self._event(cur, "sr_tool_call_delivered",
                        detail={"event_id": event_id})

    def pending_deliveries(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Finished calls whose result was never delivered to Sr.

        A crash between ``finish_tool_call`` and ``send_message`` would otherwise
        strand the result: Sr waits forever for a reply it will never get. These
        rows are re-delivered on the next tick.
        """
        with self._tx() as cur:
            cur.execute(
                """
                SELECT c.event_id, c.workflow_id, c.name, c.result, c.is_error,
                       w.sr_conversation_id
                  FROM sr_tool_calls c
                  JOIN workflows w ON w.id = c.workflow_id
                 WHERE c.finished_at IS NOT NULL
                   AND c.delivered_at IS NULL
                   AND w.sr_conversation_id IS NOT NULL
                 ORDER BY c.finished_at
                 LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return rows

    def tool_calls(self, *, workflow_id: str, limit: int = 100) -> list[dict]:
        with self._tx() as cur:
            cur.execute(
                "SELECT * FROM sr_tool_calls WHERE workflow_id=%s "
                "ORDER BY created_at DESC LIMIT %s",
                (workflow_id, limit),
            )
            rows = cur.fetchall()
        return rows

    def count_child_tasks(self, workflow_id: str) -> int:
        with self._tx() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM tasks WHERE workflow_id=%s", (workflow_id,)
            )
            row = cur.fetchone()
        return int(row["n"]) if row else 0

    def has_pending_tool_calls(self, workflow_id: str) -> bool:
        """True when a tool call is claimed-but-unfinished or finished-but-undelivered.

        The workflow verifier uses this to avoid completing a workflow while a Sr
        call is still in flight or its result has not reached Sr yet.
        """
        with self._tx() as cur:
            cur.execute(
                "SELECT 1 FROM sr_tool_calls WHERE workflow_id=%s "
                "AND (finished_at IS NULL OR delivered_at IS NULL) LIMIT 1",
                (workflow_id,),
            )
            row = cur.fetchone()
        return row is not None

    # -------------------------------------------------------- workflow state
    def workflow_progress(self, workflow_id: str) -> dict[str, int]:
        """Counts by category, from durable task state only."""
        with self._tx() as cur:
            cur.execute(
                "SELECT state, count(*) AS n FROM tasks WHERE workflow_id=%s "
                "GROUP BY state",
                (workflow_id,),
            )
            rows = cur.fetchall()
        counts = {r["state"]: int(r["n"]) for r in rows}
        completed = counts.get(TaskState.COMPLETED.value, 0)
        cancelled = counts.get(TaskState.CANCELLED.value, 0)
        blocked = counts.get(TaskState.BLOCKED.value, 0)
        failed = (counts.get(TaskState.FAILED.value, 0)
                  + counts.get(TaskState.DEAD_LETTER.value, 0))
        active = sum(
            counts.get(s.value, 0)
            for s in (TaskState.QUEUED, TaskState.ASSIGNED, TaskState.RUNNING,
                      TaskState.VERIFYING, TaskState.RECOVERING)
        )
        return {
            "total": sum(counts.values()),
            "completed": completed,
            "cancelled": cancelled,
            "blocked": blocked,
            "failed": failed,
            "active": active,
        }

    def set_workflow_state(self, workflow_id: str, state: str) -> None:
        """Move a workflow to *state*. Only the verifier calls this."""
        with self._tx() as cur:
            cur.execute(
                "UPDATE workflows SET state=%s, updated_at=now() WHERE id=%s",
                (state, workflow_id),
            )
            if cur.rowcount == 0:
                raise NotFound(f"workflow {workflow_id}")
            self._event(cur, "workflow_state", workflow_id=workflow_id,
                        detail={"state": state})

    def cancel_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Cancel a workflow: a terminal, operator-chosen stop.

        ``cancelled`` is terminal for the verifier and for ``SrRuntime.tick``,
        which both skip it, so no further Sr tool call is serviced and no task is
        dispatched. Cancelling a workflow that is already terminal is a no-op
        rather than an error, so a double click is harmless.

        Child tasks still in flight are moved to ``cancelled`` too; without that
        the Jr dispatcher would keep polling their Cloud conversations after the
        workflow they belong to has been abandoned.
        """
        with self._tx() as cur:
            cur.execute(
                "SELECT state FROM workflows WHERE id=%s FOR UPDATE", (workflow_id,)
            )
            row = cur.fetchone()
            if row is None:
                raise NotFound(f"workflow {workflow_id}")
            already = row["state"] == "cancelled"
            cur.execute(
                "UPDATE workflows SET state='cancelled', sr_status='cancelled', "
                "sr_next_attempt_at=NULL, updated_at=now() WHERE id=%s",
                (workflow_id,),
            )
            if not already:
                self._event(cur, "workflow_state", workflow_id=workflow_id,
                            detail={"state": "cancelled", "by": "operator"})
            cur.execute(
                "SELECT id, state FROM tasks WHERE workflow_id=%s", (workflow_id,)
            )
            children = cur.fetchall()
        cancelled: list[str] = []
        for child in children:
            if TaskState(child["state"]) in TERMINAL_STATES:
                continue
            try:
                self.force_transition(child["id"], TaskState.CANCELLED)
                cancelled.append(str(child["id"]))
            except (InvalidTransition, NotFound):
                # A task that cannot legally be cancelled (already terminal) is
                # left alone; cancellation is best-effort per child.
                pass
        return {"workflow_id": workflow_id, "cancelled": already,
                "tasks_cancelled": cancelled}

    def resume_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Reopen a blocked or failed workflow so it is serviced again.

        Clears the Sr start backoff and puts the workflow back to ``queued``, so
        the verifier re-evaluates it and the Sr loop may retry a start that had
        given up. Child tasks that are retryable (``blocked``/``failed`` with
        attempts left) are requeued; a dead-lettered task is left dead, because a
        human asking to resume the *workflow* is not the same as asking to spend
        another attempt on a task that exhausted its budget.
        """
        with self._tx() as cur:
            cur.execute(
                "SELECT state FROM workflows WHERE id=%s FOR UPDATE", (workflow_id,)
            )
            row = cur.fetchone()
            if row is None:
                raise NotFound(f"workflow {workflow_id}")
            if row["state"] in ("completed", "cancelled"):
                raise InvalidTransition(
                    f"workflow {workflow_id} is {row['state']}, cannot resume"
                )
            cur.execute(
                "UPDATE workflows SET state='queued', sr_attempt=0, "
                "sr_last_error=NULL, sr_next_attempt_at=NULL, "
                "sr_start_owner=NULL, sr_start_lease_expires=NULL, "
                "sr_status = CASE WHEN sr_conversation_id IS NULL THEN NULL "
                "                 ELSE sr_status END, "
                "updated_at=now() WHERE id=%s",
                (workflow_id,),
            )
            self._event(cur, "workflow_resumed", workflow_id=workflow_id,
                        detail={"state": "queued"})
            cur.execute(
                "SELECT id, state, attempt, max_attempts FROM tasks "
                "WHERE workflow_id=%s",
                (workflow_id,),
            )
            children = cur.fetchall()
        requeued: list[str] = []
        for child in children:
            state = TaskState(child["state"])
            if state not in (TaskState.BLOCKED, TaskState.FAILED):
                continue
            if int(child["attempt"]) >= int(child["max_attempts"]):
                continue
            try:
                self.force_transition(child["id"], TaskState.QUEUED)
                requeued.append(str(child["id"]))
            except (InvalidTransition, NotFound):
                pass
        return {"workflow_id": workflow_id, "requeued": requeued}

    def retry_task(self, task_id: str) -> Task:
        """Requeue one task, clearing its lease, for an explicit operator retry.

        Unlike the automatic path this does not consult ``max_attempts``: the
        operator has seen the failure and asked for another run. The counter is
        not reset, so if the retry fails again the task still advances toward its
        limit and dead-letters instead of looping forever.
        """
        return self.force_transition(task_id, TaskState.QUEUED)

    # --------------------------------------------------------------- auth
    def create_session(self, *, token_hash: str, username: str,
                       csrf_token: str, ttl_seconds: int) -> None:
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO sessions(token_hash, username, csrf_token, expires_at)
                VALUES (%s, %s, %s, now() + make_interval(secs => %s))
                """,
                (token_hash, username, csrf_token, ttl_seconds),
            )

    def get_session(self, token_hash: str) -> dict[str, Any] | None:
        """Return the live session row, or ``None``. Expired rows are ignored."""
        with self._tx() as cur:
            cur.execute(
                "SELECT username, csrf_token, expires_at FROM sessions "
                "WHERE token_hash=%s AND expires_at > now()",
                (token_hash,),
            )
            row = cur.fetchone()
            if row is not None:
                cur.execute(
                    "UPDATE sessions SET last_seen=now() WHERE token_hash=%s",
                    (token_hash,),
                )
        return row

    def delete_session(self, token_hash: str) -> None:
        with self._tx() as cur:
            cur.execute("DELETE FROM sessions WHERE token_hash=%s", (token_hash,))

    def purge_expired_sessions(self) -> int:
        with self._tx() as cur:
            cur.execute("DELETE FROM sessions WHERE expires_at <= now()")
            return cur.rowcount

    def login_locked_for(self, key: str) -> int:
        """Seconds until the lock on *key* expires, or 0 if it is not locked.

        Returning the remaining time (rather than a bool) lets the login page
        say "try again in N minutes" instead of blaming the credentials.
        """
        with self._tx() as cur:
            cur.execute(
                "SELECT locked_until FROM login_attempts WHERE key=%s", (key,)
            )
            row = cur.fetchone()
        if not row or row["locked_until"] is None:
            return 0
        remaining = row["locked_until"].timestamp() - time.time()
        return int(remaining) + 1 if remaining > 0 else 0

    def login_locked(self, key: str) -> bool:
        return self.login_locked_for(key) > 0

    def record_login_failure(self, key: str, *, max_failures: int,
                             lockout_seconds: int) -> None:
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO login_attempts(key, failures, window_start, locked_until)
                VALUES (%s, 1, now(), NULL)
                ON CONFLICT (key) DO UPDATE
                   SET failures = CASE
                           WHEN login_attempts.window_start < now() - interval '15 minutes'
                           THEN 1 ELSE login_attempts.failures + 1 END,
                       window_start = CASE
                           WHEN login_attempts.window_start < now() - interval '15 minutes'
                           THEN now() ELSE login_attempts.window_start END,
                       locked_until = CASE
                           WHEN (CASE
                                   WHEN login_attempts.window_start < now() - interval '15 minutes'
                                   THEN 1 ELSE login_attempts.failures + 1 END) >= %s
                           THEN now() + make_interval(secs => %s)
                           ELSE NULL END
                """,
                (key, max_failures, lockout_seconds),
            )

    def clear_login_failures(self, key: str) -> None:
        with self._tx() as cur:
            cur.execute("DELETE FROM login_attempts WHERE key=%s", (key,))

    # ---------------------------------------------------------- dashboard
    def dashboard_counts(self) -> dict[str, int]:
        """Real counts from durable state. No synthetic metrics."""
        with self._tx() as cur:
            cur.execute("SELECT count(*) AS n FROM workflows")
            workflows = int(cur.fetchone()["n"])
            cur.execute("SELECT state, count(*) AS n FROM tasks GROUP BY state")
            by_state = {r["state"]: int(r["n"]) for r in cur.fetchall()}
        queued = by_state.get(TaskState.QUEUED.value, 0)
        assigned = by_state.get(TaskState.ASSIGNED.value, 0)
        running = by_state.get(TaskState.RUNNING.value, 0)
        verifying = by_state.get(TaskState.VERIFYING.value, 0)
        recovering = by_state.get(TaskState.RECOVERING.value, 0)
        return {
            "workflows": workflows,
            "tasks": sum(by_state.values()),
            "queued": queued + assigned,
            "running": running + verifying + recovering,
            "completed": by_state.get(TaskState.COMPLETED.value, 0),
            "failed": (by_state.get(TaskState.FAILED.value, 0)
                       + by_state.get(TaskState.DEAD_LETTER.value, 0)),
            "blocked": by_state.get(TaskState.BLOCKED.value, 0),
            "cancelled": by_state.get(TaskState.CANCELLED.value, 0),
        }

    def list_tasks_all(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """All tasks, newest first, each with its workflow goal for display."""
        with self._tx() as cur:
            cur.execute(
                """
                SELECT t.id, t.goal, t.state, t.attempt, t.max_attempts,
                       t.selected_repository, t.selected_branch, t.result,
                       t.evidence, t.owner, t.updated_at,
                       t.workflow_id, w.goal AS workflow_goal,
                       c.external_conversation_id AS conversation_id,
                       c.url AS conversation_url
                  FROM tasks t
                  LEFT JOIN workflows w ON w.id = t.workflow_id
                  LEFT JOIN conversations c ON c.task_id = t.id
                 ORDER BY t.updated_at DESC
                 LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        for r in rows:
            for key in ("id", "workflow_id"):
                if r.get(key) is not None:
                    r[key] = str(r[key])
            r["updated_at"] = r["updated_at"].timestamp() if r.get("updated_at") else None
        return rows

    def workflow_events(self, workflow_id: str, *, limit: int = 100) -> list[dict]:
        return self.events(workflow_id=workflow_id, limit=limit)


def _row_to_task(row: dict[str, Any]) -> Task:
    deps = row.get("dependencies")
    if isinstance(deps, str):
        deps = json.loads(deps)
    ev = row.get("evidence")
    if isinstance(ev, str):
        ev = json.loads(ev)
    lease = row.get("lease_expires")
    return Task(
        id=str(row["id"]),
        state=TaskState(row["state"]),
        goal=row["goal"],
        workflow_id=str(row["workflow_id"]) if row.get("workflow_id") else None,
        parent_task_id=str(row["parent_task_id"]) if row.get("parent_task_id") else None,
        owner=row.get("owner"),
        lease_expires=lease.timestamp() if hasattr(lease, "timestamp") else lease,
        attempt=row["attempt"],
        max_attempts=row["max_attempts"],
        priority=row["priority"],
        dependencies=[str(d) for d in (deps or [])],
        requires_approval=bool(row["requires_approval"]),
        approved=None if row["approved"] is None else bool(row["approved"]),
        evidence=ev or [],
        result=row.get("result"),
        conversation_id=row.get("conversation_id"),
        selected_repository=row.get("selected_repository"),
        selected_branch=row.get("selected_branch"),
        updated_at=row["updated_at"].timestamp() if hasattr(row["updated_at"], "timestamp") else 0.0,
    )
