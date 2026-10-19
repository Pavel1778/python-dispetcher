"""PostgreSQL / Supabase state store.

Uses ``psycopg`` (async). Claims are atomic via ``SELECT ... FOR UPDATE SKIP
LOCKED`` inside a transaction, which makes concurrent dispatchers safe and
prevents duplicate conversation creation across restarts.

Migrations in ``app/migrations`` are applied in lexical order and recorded in
``schema_migrations`` so re-running is a no-op.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .base import StateStore, StoreUnavailableError
from .models import EventRecord, Notification, Task, Workflow
from ..status import TERMINAL_TASK_STATUSES

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_TASK_COLUMNS = (
    "id, workflow_id, parent_task_id, role, status, idempotency_key, title, prompt, "
    "start_task_id, app_conversation_id, sandbox_id, selected_repository, selected_branch, "
    "attempts, max_attempts, last_error, result_summary, next_attempt_at, lease_owner, "
    "lease_expires_at, tags, created_at, updated_at, started_at, finished_at"
)


def _row_to_task(row: dict[str, Any]) -> Task:
    return Task(
        id=str(row["id"]),
        workflow_id=str(row["workflow_id"]),
        parent_task_id=str(row["parent_task_id"]) if row.get("parent_task_id") else None,
        role=row["role"],
        status=row["status"],
        idempotency_key=row["idempotency_key"],
        title=row.get("title"),
        prompt=row.get("prompt"),
        start_task_id=row.get("start_task_id"),
        app_conversation_id=row.get("app_conversation_id"),
        sandbox_id=row.get("sandbox_id"),
        selected_repository=row.get("selected_repository"),
        selected_branch=row.get("selected_branch"),
        attempts=row.get("attempts", 0),
        max_attempts=row.get("max_attempts", 5),
        last_error=row.get("last_error"),
        result_summary=row.get("result_summary"),
        next_attempt_at=row.get("next_attempt_at"),
        lease_owner=row.get("lease_owner"),
        lease_expires_at=row.get("lease_expires_at"),
        tags=row.get("tags") or {},
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
    )


def _row_to_workflow(row: dict[str, Any]) -> Workflow:
    return Workflow(
        id=str(row["id"]),
        name=row["name"],
        status=row["status"],
        idempotency_key=row["idempotency_key"],
        sr_task_id=str(row["sr_task_id"]) if row.get("sr_task_id") else None,
        plan_summary=row.get("plan_summary"),
        result_summary=row.get("result_summary"),
        expected_jr_count=row.get("expected_jr_count"),
        tags=row.get("tags") or {},
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


class PostgresStateStore(StateStore):
    def __init__(
        self,
        database_url: str,
        *,
        pool_min: int = 1,
        pool_max: int = 5,
        connect_timeout: int = 10,
        transaction_pooler: bool = True,
    ) -> None:
        self._database_url = database_url
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._connect_timeout = connect_timeout
        # PgBouncer (Supabase transaction pooler) cannot use server-side
        # prepared statements; psycopg prepares them automatically, so disable.
        self._transaction_pooler = transaction_pooler
        self._pool: AsyncConnectionPool | None = None

    async def connect(self) -> None:
        if self._pool is None:
            conn_kwargs: dict[str, Any] = {
                "row_factory": dict_row,
                "autocommit": True,
                "connect_timeout": self._connect_timeout,
            }
            if self._transaction_pooler:
                conn_kwargs["prepare_threshold"] = None
            self._pool = AsyncConnectionPool(
                conninfo=self._database_url,
                min_size=self._pool_min,
                max_size=self._pool_max,
                open=False,
                kwargs=conn_kwargs,
            )
        try:
            await self._pool.open(wait=True, timeout=self._connect_timeout)
        except Exception as exc:  # noqa: BLE001 - surfaced as StoreUnavailableError
            raise StoreUnavailableError("could not connect to database") from exc

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def healthcheck(self) -> bool:
        if self._pool is None:
            return False
        try:
            async with self._pool.connection() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:  # noqa: BLE001 - healthcheck must not raise
            return False

    def _require_pool(self) -> AsyncConnectionPool:
        if self._pool is None:
            raise StoreUnavailableError("state store is not connected")
        return self._pool

    async def apply_migrations(self) -> list[str]:
        """Apply pending SQL migrations. Returns the list of applied filenames."""
        pool = self._require_pool()
        applied: list[str] = []
        async with pool.connection() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "filename TEXT PRIMARY KEY, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            cur = await conn.execute("SELECT filename FROM schema_migrations")
            done = {r["filename"] for r in await cur.fetchall()}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in done:
                    continue
                sql = path.read_text(encoding="utf-8")
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
                    )
                applied.append(path.name)
        return applied

    # --- workflows ---

    async def create_workflow(
        self, *, name: str, idempotency_key: str, tags: dict[str, Any] | None = None
    ) -> Workflow:
        pool = self._require_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO workflows (name, status, idempotency_key, tags) "
                "VALUES (%s, 'QUEUED', %s, %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING *",
                (name, idempotency_key, json.dumps(tags or {})),
            )
            row = await cur.fetchone()
            if row is None:
                cur = await conn.execute(
                    "SELECT * FROM workflows WHERE idempotency_key = %s", (idempotency_key,)
                )
                row = await cur.fetchone()
        return _row_to_workflow(row)

    async def get_workflow(self, workflow_id: str) -> Workflow | None:
        pool = self._require_pool()
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT * FROM workflows WHERE id = %s", (workflow_id,))
            row = await cur.fetchone()
        return _row_to_workflow(row) if row else None

    async def get_workflow_by_idempotency_key(self, key: str) -> Workflow | None:
        pool = self._require_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM workflows WHERE idempotency_key = %s", (key,)
            )
            row = await cur.fetchone()
        return _row_to_workflow(row) if row else None

    async def update_workflow_status(
        self,
        workflow_id: str,
        status: str,
        *,
        sr_task_id: str | None = None,
        plan_summary: str | None = None,
        result_summary: str | None = None,
        expected_jr_count: int | None = None,
    ) -> None:
        pool = self._require_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE workflows SET status = %s, "
                "sr_task_id = COALESCE(%s, sr_task_id), "
                "plan_summary = COALESCE(%s, plan_summary), "
                "result_summary = COALESCE(%s, result_summary), "
                "expected_jr_count = COALESCE(%s, expected_jr_count), "
                "updated_at = now() WHERE id = %s",
                (status, sr_task_id, plan_summary, result_summary, expected_jr_count, workflow_id),
            )
            if cur.rowcount == 0:
                raise StoreUnavailableError(f"workflow {workflow_id} not found")

    async def list_workflows(self, *, statuses: Sequence[str] | None = None) -> list[Workflow]:
        pool = self._require_pool()
        async with pool.connection() as conn:
            if statuses:
                cur = await conn.execute(
                    "SELECT * FROM workflows WHERE status = ANY(%s) ORDER BY created_at",
                    (list(statuses),),
                )
            else:
                cur = await conn.execute("SELECT * FROM workflows ORDER BY created_at")
            rows = await cur.fetchall()
        return [_row_to_workflow(r) for r in rows]

    # --- tasks ---

    async def create_task(
        self,
        *,
        workflow_id: str,
        role: str,
        title: str | None,
        prompt: str | None,
        idempotency_key: str,
        parent_task_id: str | None = None,
        selected_repository: str | None = None,
        selected_branch: str | None = None,
        max_attempts: int = 5,
        tags: dict[str, Any] | None = None,
    ) -> tuple[Task, bool]:
        pool = self._require_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO tasks (workflow_id, parent_task_id, role, status, idempotency_key, "
                "title, prompt, selected_repository, selected_branch, max_attempts, "
                "next_attempt_at, tags) "
                "VALUES (%s, %s, %s, 'QUEUED', %s, %s, %s, %s, %s, %s, now(), %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING " + _TASK_COLUMNS,
                (
                    workflow_id,
                    parent_task_id,
                    role,
                    idempotency_key,
                    title,
                    prompt,
                    selected_repository,
                    selected_branch,
                    max_attempts,
                    json.dumps(tags or {}),
                ),
            )
            row = await cur.fetchone()
            if row is not None:
                return _row_to_task(row), True
            cur = await conn.execute(
                "SELECT " + _TASK_COLUMNS + " FROM tasks WHERE idempotency_key = %s",
                (idempotency_key,),
            )
            row = await cur.fetchone()
        return _row_to_task(row), False

    async def get_task(self, task_id: str) -> Task | None:
        pool = self._require_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT " + _TASK_COLUMNS + " FROM tasks WHERE id = %s", (task_id,)
            )
            row = await cur.fetchone()
        return _row_to_task(row) if row else None

    async def get_task_by_idempotency_key(self, key: str) -> Task | None:
        pool = self._require_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT " + _TASK_COLUMNS + " FROM tasks WHERE idempotency_key = %s", (key,)
            )
            row = await cur.fetchone()
        return _row_to_task(row) if row else None

    async def list_tasks_for_workflow(self, workflow_id: str) -> list[Task]:
        pool = self._require_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT " + _TASK_COLUMNS + " FROM tasks WHERE workflow_id = %s ORDER BY created_at",
                (workflow_id,),
            )
            rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    async def claim_due_tasks(
        self, *, worker_id: str, lease_seconds: int, limit: int
    ) -> list[Task]:
        pool = self._require_pool()
        terminal = list(TERMINAL_TASK_STATUSES)
        async with pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "SELECT " + _TASK_COLUMNS + " FROM tasks "
                    "WHERE status <> ALL(%s) "
                    "AND (next_attempt_at IS NULL OR next_attempt_at <= now()) "
                    "AND (lease_expires_at IS NULL OR lease_expires_at <= now()) "
                    "AND attempts < max_attempts "
                    "ORDER BY created_at "
                    "LIMIT %s FOR UPDATE SKIP LOCKED",
                    (terminal, limit),
                )
                rows = await cur.fetchall()
                if not rows:
                    return []
                ids = [r["id"] for r in rows]
                await conn.execute(
                    "UPDATE tasks SET lease_owner = %s, "
                    "lease_expires_at = now() + (%s * interval '1 second'), "
                    "updated_at = now() WHERE id = ANY(%s)",
                    (worker_id, lease_seconds, ids),
                )
        claimed = []
        for r in rows:
            task = _row_to_task(r)
            task.lease_owner = worker_id
            task.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
            claimed.append(task)
        return claimed

    async def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        start_task_id: str | None = None,
        app_conversation_id: str | None = None,
        sandbox_id: str | None = None,
        last_error: str | None = None,
        result_summary: str | None = None,
        next_attempt_at: datetime | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        release_lease: bool = False,
    ) -> None:
        pool = self._require_pool()
        sets: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("status", status),
            ("start_task_id", start_task_id),
            ("app_conversation_id", app_conversation_id),
            ("sandbox_id", sandbox_id),
            ("last_error", last_error),
            ("result_summary", result_summary),
            ("next_attempt_at", next_attempt_at),
            ("started_at", started_at),
            ("finished_at", finished_at),
        ):
            if value is not None:
                sets.append(f"{column} = %s")
                params.append(value)
        if release_lease:
            sets.append("lease_owner = NULL")
            sets.append("lease_expires_at = NULL")
        sets.append("updated_at = now()")
        params.append(task_id)
        async with pool.connection() as conn:
            cur = await conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id = %s", tuple(params)
            )
            if cur.rowcount == 0:
                raise StoreUnavailableError(f"task {task_id} not found")

    async def schedule_retry(
        self,
        task_id: str,
        *,
        error: str,
        next_attempt_at: datetime,
        release_lease: bool = True,
    ) -> None:
        pool = self._require_pool()
        lease_clause = "lease_owner = NULL, lease_expires_at = NULL, " if release_lease else ""
        async with pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE tasks SET attempts = attempts + 1, last_error = %s, "
                "next_attempt_at = %s, " + lease_clause + "updated_at = now() WHERE id = %s",
                (error, next_attempt_at, task_id),
            )
            if cur.rowcount == 0:
                raise StoreUnavailableError(f"task {task_id} not found")

    async def list_tasks_by_status(self, statuses: Sequence[str]) -> list[Task]:
        pool = self._require_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT " + _TASK_COLUMNS + " FROM tasks WHERE status = ANY(%s) ORDER BY created_at",
                (list(statuses),),
            )
            rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    # --- events ---

    async def record_event(
        self,
        *,
        event_type: str,
        workflow_id: str | None = None,
        task_id: str | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        pool = self._require_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO task_events (workflow_id, task_id, event_type, message, payload) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (workflow_id, task_id, event_type, message, json.dumps(payload or {})),
            )
            row = await cur.fetchone()
        return int(row["id"])

    async def list_events(
        self, *, workflow_id: str | None = None, task_id: str | None = None, limit: int = 100
    ) -> list[EventRecord]:
        pool = self._require_pool()
        clauses, params = [], []
        if workflow_id is not None:
            clauses.append("workflow_id = %s")
            params.append(workflow_id)
        if task_id is not None:
            clauses.append("task_id = %s")
            params.append(task_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        async with pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT id, workflow_id, task_id, event_type, message, payload, created_at "
                f"FROM task_events {where} ORDER BY created_at DESC LIMIT %s",
                tuple(params),
            )
            rows = await cur.fetchall()
        return [
            EventRecord(
                id=r["id"],
                workflow_id=str(r["workflow_id"]) if r["workflow_id"] else None,
                task_id=str(r["task_id"]) if r["task_id"] else None,
                event_type=r["event_type"],
                message=r["message"],
                payload=r["payload"] or {},
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # --- notifications ---

    async def enqueue_notification(
        self,
        *,
        kind: str,
        payload: dict[str, Any] | None = None,
        workflow_id: str | None = None,
        task_id: str | None = None,
    ) -> int:
        pool = self._require_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO notifications (workflow_id, task_id, kind, payload) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (workflow_id, task_id, kind, json.dumps(payload or {})),
            )
            row = await cur.fetchone()
        return int(row["id"])

    async def list_pending_notifications(self, limit: int = 50) -> list[Notification]:
        pool = self._require_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, workflow_id, task_id, kind, payload, status, attempts, created_at, "
                "sent_at FROM notifications WHERE status = 'PENDING' ORDER BY created_at LIMIT %s",
                (limit,),
            )
            rows = await cur.fetchall()
        return [
            Notification(
                id=r["id"],
                kind=r["kind"],
                payload=r["payload"] or {},
                workflow_id=str(r["workflow_id"]) if r["workflow_id"] else None,
                task_id=str(r["task_id"]) if r["task_id"] else None,
                status=r["status"],
                attempts=r["attempts"],
                created_at=r["created_at"],
                sent_at=r["sent_at"],
            )
            for r in rows
        ]

    async def mark_notification_sent(self, notification_id: int) -> None:
        pool = self._require_pool()
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE notifications SET status = 'SENT', sent_at = now(), "
                "attempts = attempts + 1 WHERE id = %s",
                (notification_id,),
            )
