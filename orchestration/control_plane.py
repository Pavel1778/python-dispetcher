"""Durable control-plane state for multi-agent orchestration.

Everything a Commander needs to survive its own restart lives here, in a single
SQLite database on persistent storage. Conversation transcripts are *not* the
source of truth; this store is.

Design constraints that shaped the schema:

* One task has at most one owner at a time, enforced by a lease with an
  expiry, not by trusting a heartbeat to arrive.
* Task submission is idempotent: a caller supplies an idempotency key and a
  retry returns the original task instead of creating a second one.
* State transitions are atomic and validated. An illegal transition raises
  rather than silently corrupting the row.
* No secrets are stored. Only identifiers, states, and free-form notes that
  callers are expected to keep secret-free.

The store is deliberately dependency-free (stdlib ``sqlite3`` only) so it can
be embedded in the agent server, run from a CLI, or exercised in tests without
infrastructure.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator


class TaskState(str, Enum):
    """Lifecycle of a unit of work.

    The happy path is queued -> assigned -> running -> verifying -> completed.
    Every other value is a side exit that recovery must be able to reason about.
    """

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


#: Transitions the store will accept. Anything else raises ``InvalidTransition``.
_ALLOWED: dict[TaskState, frozenset[TaskState]] = {
    TaskState.QUEUED: frozenset(
        {TaskState.ASSIGNED, TaskState.CANCELLED, TaskState.BLOCKED}
    ),
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
        {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.RUNNING,
            TaskState.BLOCKED,
            TaskState.DEAD_LETTER,
        }
    ),
    # Recovery is a distinct state so a crashed task is never mistaken for a
    # running one while it is being reconciled. From here reality decides, so
    # every safe outcome is reachable, including completion: a worker can die
    # while its conversation finished successfully.
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
    TaskState.FAILED: frozenset(
        {TaskState.QUEUED, TaskState.DEAD_LETTER, TaskState.CANCELLED}
    ),
    TaskState.DEAD_LETTER: frozenset({TaskState.QUEUED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}

#: States from which a task still needs someone to do work.
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


class ControlPlaneError(RuntimeError):
    pass


class InvalidTransition(ControlPlaneError):
    pass


class LeaseConflict(ControlPlaneError):
    """Another owner holds a live lease on the task."""


class NotFound(ControlPlaneError):
    pass


SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id            TEXT PRIMARY KEY,
    repo_identity TEXT,
    directory     TEXT NOT NULL,
    default_branch TEXT,
    policies_json TEXT NOT NULL DEFAULT '{}',
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    role          TEXT NOT NULL,
    capabilities  TEXT NOT NULL DEFAULT '[]',
    project_id    TEXT,
    worktree      TEXT,
    state         TEXT NOT NULL DEFAULT 'idle',
    last_heartbeat REAL,
    recovery_attempts INTEGER NOT NULL DEFAULT 0,
    last_failure  TEXT,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    project_id    TEXT,
    goal          TEXT NOT NULL,
    request       TEXT NOT NULL DEFAULT '',
    acceptance    TEXT NOT NULL DEFAULT '',
    risk          TEXT NOT NULL DEFAULT 'normal',
    priority      INTEGER NOT NULL DEFAULT 0,
    dependencies  TEXT NOT NULL DEFAULT '[]',
    state         TEXT NOT NULL,
    owner         TEXT,
    lease_expires REAL,
    attempt       INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    idempotency_key TEXT UNIQUE,
    requires_approval INTEGER NOT NULL DEFAULT 0,
    approved      INTEGER,
    evidence      TEXT NOT NULL DEFAULT '[]',
    result        TEXT,
    conversation_id TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner);
CREATE INDEX IF NOT EXISTS idx_tasks_lease ON tasks(lease_expires);

CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    at         REAL NOT NULL,
    kind       TEXT NOT NULL,
    task_id    TEXT,
    session_id TEXT,
    detail     TEXT NOT NULL DEFAULT '{}'
);
"""


@dataclass
class Task:
    id: str
    state: TaskState
    goal: str
    project_id: str | None
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
    updated_at: float
    conversation_id: str | None = None

    @property
    def lease_live(self) -> bool:
        return self.lease_expires is not None and self.lease_expires > time.time()


def _now() -> float:
    return time.time()


class ControlPlane:
    """SQLite-backed durable state.

    Thread-safe via a re-entrant lock around every write. SQLite is opened with
    ``check_same_thread=False`` and ``isolation_level=None`` so transactions are
    explicit and short.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # One connection, one lock. Every read and write goes through the lock,
        # so no thread can observe another thread's half-finished transaction.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the first schema, in place.

        ``CREATE TABLE IF NOT EXISTS`` does not alter an existing table, so a
        database written by an earlier version keeps its old columns. Adding
        the column here keeps old state files usable instead of forcing a
        migration script.
        """
        cols = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "conversation_id" not in cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN conversation_id TEXT")

    def _read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---------------------------------------------------------------- infra
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _event(
        self,
        conn: sqlite3.Connection,
        kind: str,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO events(at, kind, task_id, session_id, detail) VALUES(?,?,?,?,?)",
            (
                _now(),
                kind,
                task_id,
                session_id,
                json.dumps(detail or {}, ensure_ascii=False),
            ),
        )

    # ------------------------------------------------------------- projects
    def upsert_project(
        self,
        project_id: str,
        directory: str,
        *,
        repo_identity: str | None = None,
        default_branch: str | None = None,
        policies: dict[str, Any] | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO projects(id, repo_identity, directory, default_branch,
                                     policies_json, created_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    repo_identity=excluded.repo_identity,
                    directory=excluded.directory,
                    default_branch=excluded.default_branch,
                    policies_json=excluded.policies_json
                """,
                (
                    project_id,
                    repo_identity,
                    directory,
                    default_branch,
                    json.dumps(policies or {}),
                    _now(),
                ),
            )

    def get_project(self, project_id: str) -> dict | None:
        rows = self._read("SELECT * FROM projects WHERE id=?", (project_id,))
        return dict(rows[0]) if rows else None

    # ------------------------------------------------------------- sessions
    def register_session(
        self,
        session_id: str,
        role: str,
        *,
        capabilities: list[str] | None = None,
        project_id: str | None = None,
        worktree: str | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO sessions(id, role, capabilities, project_id, worktree,
                                     state, last_heartbeat, created_at)
                VALUES(?,?,?,?,?, 'idle', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    role=excluded.role,
                    capabilities=excluded.capabilities,
                    project_id=excluded.project_id,
                    worktree=excluded.worktree,
                    last_heartbeat=excluded.last_heartbeat
                """,
                (
                    session_id,
                    role,
                    json.dumps(capabilities or []),
                    project_id,
                    worktree,
                    _now(),
                    _now(),
                ),
            )
            self._event(
                conn, "session_registered", session_id=session_id, detail={"role": role}
            )

    def heartbeat(self, session_id: str, *, state: str | None = None) -> None:
        with self._tx() as conn:
            sets = "last_heartbeat=?"
            params: list[Any] = [_now()]
            if state is not None:
                sets += ", state=?"
                params.append(state)
            params.append(session_id)
            cur = conn.execute(f"UPDATE sessions SET {sets} WHERE id=?", params)
            if cur.rowcount == 0:
                raise NotFound(f"session {session_id}")

    def get_session(self, session_id: str) -> dict | None:
        rows = self._read("SELECT * FROM sessions WHERE id=?", (session_id,))
        return dict(rows[0]) if rows else None

    def list_sessions(self) -> list[dict]:
        return [
            dict(r)
            for r in self._read("SELECT * FROM sessions ORDER BY created_at")
        ]

    def stale_sessions(self, ttl_seconds: float) -> list[dict]:
        cutoff = _now() - ttl_seconds
        return [
            dict(r)
            for r in self._read(
                "SELECT * FROM sessions "
                "WHERE last_heartbeat IS NULL OR last_heartbeat < ?",
                (cutoff,),
            )
        ]

    # ---------------------------------------------------------------- tasks
    def submit_task(
        self,
        task_id: str,
        goal: str,
        *,
        project_id: str | None = None,
        request: str = "",
        acceptance: str = "",
        risk: str = "normal",
        priority: int = 0,
        dependencies: list[str] | None = None,
        max_attempts: int = 3,
        idempotency_key: str | None = None,
        requires_approval: bool = False,
    ) -> Task:
        """Create a task, or return the existing one for the same key.

        Idempotency is enforced by a UNIQUE index, so two concurrent submissions
        with the same key cannot both insert. The loser reads the winner's row.
        """
        now = _now()
        try:
            with self._tx() as conn:
                conn.execute(
                    """
                    INSERT INTO tasks(id, project_id, goal, request, acceptance, risk,
                                      priority, dependencies, state, attempt, max_attempts,
                                      idempotency_key, requires_approval,
                                      created_at, updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)
                    """,
                    (
                        task_id,
                        project_id,
                        goal,
                        request,
                        acceptance,
                        risk,
                        priority,
                        json.dumps(dependencies or []),
                        TaskState.QUEUED.value,
                        max_attempts,
                        idempotency_key,
                        int(requires_approval),
                        now,
                        now,
                    ),
                )
                self._event(
                    conn, "task_submitted", task_id=task_id, detail={"goal": goal}
                )
        except sqlite3.IntegrityError:
            if idempotency_key is None:
                raise
            existing = self._by_idempotency(idempotency_key)
            if existing is None:
                raise
            return existing
        return self.get_task(task_id)

    def _by_idempotency(self, key: str) -> Task | None:
        rows = self._read(
            "SELECT * FROM tasks WHERE idempotency_key=?", (key,)
        )
        return _row_to_task(rows[0]) if rows else None

    def get_task(self, task_id: str) -> Task:
        rows = self._read("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not rows:
            raise NotFound(f"task {task_id}")
        return _row_to_task(rows[0])

    def try_get_task(self, task_id: str) -> Task | None:
        rows = self._read("SELECT * FROM tasks WHERE id=?", (task_id,))
        return _row_to_task(rows[0]) if rows else None

    def list_tasks(
        self, *, state: TaskState | None = None, owner: str | None = None
    ) -> list[Task]:
        sql = "SELECT * FROM tasks"
        clauses: list[str] = []
        params: list[Any] = []
        if state is not None:
            clauses.append("state=?")
            params.append(state.value)
        if owner is not None:
            clauses.append("owner=?")
            params.append(owner)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY priority DESC, created_at"
        return [_row_to_task(r) for r in self._read(sql, tuple(params))]

    def claim_task(
        self, task_id: str, owner: str, *, lease_seconds: float = 300
    ) -> Task:
        """Assign a queued task to *owner* under a lease.

        Refuses when another owner holds a live lease, or when the task's
        dependencies are not all completed.
        """
        now = _now()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise NotFound(f"task {task_id}")
            task = _row_to_task(row)
            if task.owner is not None and task.lease_live and task.owner != owner:
                raise LeaseConflict(f"task {task_id} leased by {task.owner}")
            if task.state not in (
                TaskState.QUEUED,
                TaskState.RECOVERING,
                TaskState.FAILED,
            ):
                raise InvalidTransition(f"{task.state.value} -> assigned")
            unmet = self._unmet_dependencies(conn, task)
            if unmet:
                raise InvalidTransition(f"unmet dependencies: {unmet}")
            if task.requires_approval and task.approved is not True:
                raise InvalidTransition("approval required")

            conn.execute(
                """
                UPDATE tasks SET state=?, owner=?, lease_expires=?, updated_at=?
                WHERE id=?
                """,
                (TaskState.ASSIGNED.value, owner, now + lease_seconds, now, task_id),
            )
            self._event(
                conn,
                "task_claimed",
                task_id=task_id,
                session_id=owner,
                detail={"lease_seconds": lease_seconds},
            )
        return self.get_task(task_id)

    def _unmet_dependencies(
        self, conn: sqlite3.Connection, task: Task
    ) -> list[str]:
        unmet: list[str] = []
        for dep in task.dependencies:
            row = conn.execute(
                "SELECT state FROM tasks WHERE id=?", (dep,)
            ).fetchone()
            if row is None or row["state"] != TaskState.COMPLETED.value:
                unmet.append(dep)
        return unmet

    def renew_lease(
        self, task_id: str, owner: str, *, lease_seconds: float = 300
    ) -> Task:
        now = _now()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise NotFound(f"task {task_id}")
            if row["owner"] != owner:
                raise LeaseConflict(f"task {task_id} not owned by {owner}")
            if row["state"] not in (
                TaskState.ASSIGNED.value,
                TaskState.RUNNING.value,
            ):
                raise InvalidTransition(f"cannot renew in state {row['state']}")
            conn.execute(
                "UPDATE tasks SET lease_expires=?, updated_at=? WHERE id=?",
                (now + lease_seconds, now, task_id),
            )
        return self.get_task(task_id)

    def transition(
        self,
        task_id: str,
        to: TaskState,
        *,
        owner: str | None = None,
        result: str | None = None,
        evidence: list[dict] | None = None,
    ) -> Task:
        """Move a task to *to*, enforcing ownership and the transition table."""
        now = _now()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise NotFound(f"task {task_id}")
            task = _row_to_task(row)
            frm = task.state
            if to not in _ALLOWED[frm]:
                raise InvalidTransition(f"{frm.value} -> {to.value}")
            if owner is not None and task.owner is not None and task.owner != owner:
                raise LeaseConflict(f"task {task_id} owned by {task.owner}")

            new_owner = task.owner
            new_lease = task.lease_expires
            attempt = task.attempt
            if to is TaskState.QUEUED:
                # Releasing back to the queue clears ownership so recovery can
                # pick it up; the attempt counter is what prevents a hot loop.
                new_owner = None
                new_lease = None
            if to is TaskState.FAILED:
                attempt = task.attempt + 1
                new_owner = None
                new_lease = None
            if to is TaskState.DEAD_LETTER:
                new_owner = None
                new_lease = None

            new_evidence = list(task.evidence)
            if evidence:
                new_evidence.extend(evidence)

            conn.execute(
                """
                UPDATE tasks SET state=?, owner=?, lease_expires=?, attempt=?,
                                 result=COALESCE(?, result), evidence=?, updated_at=?
                WHERE id=?
                """,
                (
                    to.value,
                    new_owner,
                    new_lease,
                    attempt,
                    result,
                    json.dumps(new_evidence),
                    now,
                    task_id,
                ),
            )
            self._event(
                conn,
                "task_transition",
                task_id=task_id,
                session_id=new_owner,
                detail={"from": frm.value, "to": to.value},
            )
        return self.get_task(task_id)

    def approve(self, task_id: str, *, approved: bool) -> Task:
        now = _now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"task {task_id}")
            conn.execute(
                "UPDATE tasks SET approved=?, updated_at=? WHERE id=?",
                (int(approved), now, task_id),
            )
            self._event(
                conn,
                "task_approval",
                task_id=task_id,
                detail={"approved": bool(approved)},
            )
        return self.get_task(task_id)

    def bind_conversation(self, task_id: str, conversation_id: str) -> Task:
        """Record which conversation is executing a task.

        Written before the task is started, so a crash between "create
        conversation" and "start run" leaves the link recoverable.
        """
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE tasks SET conversation_id=?, updated_at=? WHERE id=?",
                (conversation_id, _now(), task_id),
            )
            if cur.rowcount == 0:
                raise NotFound(f"task {task_id}")
            self._event(
                conn,
                "conversation_bound",
                task_id=task_id,
                detail={"conversation_id": conversation_id},
            )
        return self.get_task(task_id)

    def set_result(self, task_id: str, result: str) -> Task:
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE tasks SET result=?, updated_at=? WHERE id=?",
                (result, _now(), task_id),
            )
            if cur.rowcount == 0:
                raise NotFound(f"task {task_id}")
        return self.get_task(task_id)

    def add_evidence(self, task_id: str, evidence: dict) -> Task:
        now = _now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"task {task_id}")
            items = json.loads(row["evidence"])
            items.append(evidence)
            conn.execute(
                "UPDATE tasks SET evidence=?, updated_at=? WHERE id=?",
                (json.dumps(items), now, task_id),
            )
        return self.get_task(task_id)

    # ------------------------------------------------------------- recovery
    def expired_leases(self) -> list[Task]:
        """Tasks that are assigned or running with a lease past its expiry."""
        now = _now()
        rows = self._read(
            """
            SELECT * FROM tasks
            WHERE state IN (?, ?, ?)
              AND (lease_expires IS NULL OR lease_expires < ?)
            """,
            (
                TaskState.ASSIGNED.value,
                TaskState.RUNNING.value,
                TaskState.RECOVERING.value,
                now,
            ),
        )
        return [_row_to_task(r) for r in rows]

    def reclaim_expired(self, *, max_attempts: int | None = None) -> list[dict]:
        """Move expired tasks to RECOVERING, or DEAD_LETTER when out of tries.

        This is the safe half of recovery: it changes control-plane state only.
        It never re-runs work and never touches a repository. A task that has
        exhausted its attempts goes to the dead-letter state instead of being
        retried forever.
        """
        actions: list[dict] = []
        for task in self.expired_leases():
            if task.state is TaskState.RECOVERING:
                # Already reclaimed on an earlier pass; leaving it here would
                # attempt RECOVERING -> RECOVERING and fail the transition table.
                continue
            limit = task.max_attempts if max_attempts is None else max_attempts
            if task.attempt >= limit:
                self.transition(task.id, TaskState.DEAD_LETTER)
                actions.append({"task": task.id, "action": "dead_letter"})
            else:
                self.transition(task.id, TaskState.RECOVERING)
                actions.append({"task": task.id, "action": "recovering"})
        return actions

    def reconcile(self, *, session_ttl: float = 120) -> dict:
        """Reconcile durable state with reality after a crash.

        Returns a report and changes nothing outside this database. Callers use
        it to decide what to resume; the store does not guess.
        """
        report: dict[str, Any] = {
            "stale_sessions": [],
            "reclaimed": [],
            "blocked_on_dependencies": [],
            "awaiting_approval": [],
            "dead_letter": [],
            "resumable": [],
        }
        for sess in self.stale_sessions(session_ttl):
            report["stale_sessions"].append(sess["id"])
            with self._tx() as conn:
                conn.execute(
                    "UPDATE sessions SET state='stale', "
                    "last_failure='heartbeat expired' WHERE id=?",
                    (sess["id"],),
                )

        report["reclaimed"] = self.reclaim_expired()

        for task in self.list_tasks():
            if task.state in TERMINAL_STATES:
                continue
            if task.state is TaskState.QUEUED:
                unmet = [
                    d
                    for d in task.dependencies
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

    # --------------------------------------------------------------- events
    def events(
        self, *, task_id: str | None = None, limit: int = 100
    ) -> list[dict]:
        if task_id is None:
            rows = self._read(
                "SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)
            )
        else:
            rows = self._read(
                "SELECT * FROM events WHERE task_id=? ORDER BY seq DESC LIMIT ?",
                (task_id, limit),
            )
        return [dict(r) for r in rows]


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        state=TaskState(row["state"]),
        goal=row["goal"],
        project_id=row["project_id"],
        owner=row["owner"],
        lease_expires=row["lease_expires"],
        attempt=row["attempt"],
        max_attempts=row["max_attempts"],
        priority=row["priority"],
        dependencies=json.loads(row["dependencies"]),
        requires_approval=bool(row["requires_approval"]),
        approved=None if row["approved"] is None else bool(row["approved"]),
        evidence=json.loads(row["evidence"]),
        result=row["result"],
        updated_at=row["updated_at"],
        conversation_id=row["conversation_id"],
    )
