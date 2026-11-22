//! `nexus-store` - the NEXUS PostgreSQL claim transaction, in Rust.
//!
//! This is the first I/O component of the NEXUS 3.0 Rust backend. It performs
//! the *same* atomic, fenced claim that `LabStore.claim_task` performs in
//! Python (`orchestration/nexus_lab_store.py`), over the same schema, so the
//! two can be compared on a real database under real concurrency. It is not a
//! re-architecture: it is the existing contract, implemented in Rust with SQLx.
//!
//! ## What the claim does (mirrors the Python exactly)
//!
//! 1. Lock the run row; refuse unless it is `running`, has iterations left in
//!    its cycle, is not pausing (`resume_at`), and - for a `bounded` run - is
//!    inside its deadline. A pause/cancel that committed first therefore wins.
//! 2. Refuse while `max_active_tasks()` tasks are in flight (a global
//!    backpressure cap), decided under one advisory lock.
//! 3. Select the oldest due task of the run that (a) has attempts left, (b) has
//!    every dependency `completed_verified`/`completed_no_changes`, and (c) has
//!    no other writer on its branch - one at a time, `FOR UPDATE SKIP LOCKED`.
//! 4. For a task with a repository, take an advisory lock on the branch and
//!    re-check it free (two workers may reach for one branch together).
//! 5. Spend one iteration on the run, and take the task: `state='dispatched'`,
//!    `attempt+1`, a fresh `lease_token`, `lease_owner=worker`, a new lease.
//!
//! The claim hands back the task row with its `lease_token`. Every later
//! mutation the worker makes is fenced on that token (see [`TaskFence`]), so a
//! worker that lost the task cannot write to it.
//!
//! ## Parity
//!
//! `tests/test_rust_claim_parity.py` drives this binary and the Python
//! `LabStore.claim_task` over the same database states and compares the
//! outcomes; `tests/claim_parity.rs` (this crate) checks the decision over a
//! real database directly. The gate is a real comparison, not a compile.

pub mod lifecycle;

/// Accept both a `postgres://` URL and a libpq `key=value` DSN, returning a URL
/// SQLx understands. SQLx only parses URLs; the Python test harness and some
/// deployments pass libpq's own form.
pub fn to_sqlx_url(dsn: &str) -> String {
    if dsn.contains("://") {
        return dsn.to_string();
    }
    let mut user = String::new();
    let mut password = String::new();
    let mut host = "127.0.0.1".to_string();
    let mut port = "5432".to_string();
    let mut dbname = String::new();
    for tok in dsn.split_whitespace() {
        if let Some((k, v)) = tok.split_once('=') {
            match k {
                "user" => user = v.to_string(),
                "password" => password = v.to_string(),
                "host" | "hostaddr" => host = v.to_string(),
                "port" => port = v.to_string(),
                "dbname" => dbname = v.to_string(),
                _ => {}
            }
        }
    }
    let auth = if password.is_empty() {
        user
    } else {
        format!("{user}:{password}")
    };
    format!("postgres://{auth}@{host}:{port}/{dbname}")
}

use serde::{Deserialize, Serialize};
use sqlx::postgres::PgPoolOptions;
use sqlx::{PgPool, Postgres, Row, Transaction};
use uuid::Uuid;

/// A `nexus_run_tasks` row, the fields the lifecycle and the loop read.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskRow {
    pub id: Uuid,
    pub run_id: Uuid,
    pub project_id: Uuid,
    pub task_key: String,
    pub state: String,
    pub attempt: i32,
    pub max_attempts: i32,
    pub cycle: i32,
    pub lease_token: i64,
    pub lease_owner: Option<String>,
    pub repository: String,
    pub branch: String,
    pub conversation_id: Option<String>,
    pub last_error: Option<String>,
}

impl TaskRow {
    pub fn read(row: &sqlx::postgres::PgRow) -> Result<TaskRow, sqlx::Error> {
        Ok(TaskRow {
            id: row.try_get("id")?,
            run_id: row.try_get("run_id")?,
            project_id: row.try_get("project_id")?,
            task_key: row.try_get("task_key")?,
            state: row.try_get("state")?,
            attempt: row.try_get("attempt")?,
            max_attempts: row.try_get("max_attempts")?,
            cycle: row.try_get("cycle")?,
            lease_token: row.try_get("lease_token")?,
            lease_owner: row.try_get("lease_owner")?,
            repository: row.try_get("repository")?,
            branch: row.try_get("branch")?,
            conversation_id: row.try_get("conversation_id")?,
            last_error: row.try_get("last_error")?,
        })
    }
}

/// A run has no claimable task right now (or the run does not accept a claim).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClaimOutcome {
    /// A task was taken; its row is returned.
    Taken,
    /// Nothing to take (run gate refused, no due task, deps unmet, branch busy).
    Nothing,
}

/// One claimed task, as the loop needs it after the claim.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ClaimedTask {
    pub id: Uuid,
    pub run_id: Uuid,
    pub project_id: Uuid,
    pub task_key: String,
    pub state: String,
    pub attempt: i32,
    pub max_attempts: i32,
    pub lease_token: i64,
    pub lease_owner: Option<String>,
    pub repository: String,
    pub branch: String,
    pub conversation_id: Option<String>,
}

/// The store: a connection pool over one PostgreSQL database.
#[derive(Clone)]
pub struct Store {
    pool: PgPool,
    /// The same cap the Python side reads (`NEXUS_LAB_MAX_ACTIVE_TASKS`, default
    /// 8); `0` = no cap. The name must match `nexus_lab_store.max_active_tasks`
    /// exactly, or the two implementations would enforce different ceilings.
    max_active_tasks: i64,
}

impl Store {
    /// Connect with a pool sized for the target server (4 CPU / 8 GB): a small
    /// pool is correct here - the worker is one process, not a crowd.
    pub async fn connect(dsn: &str, max_connections: u32) -> Result<Self, sqlx::Error> {
        let url = to_sqlx_url(dsn);
        let pool = PgPoolOptions::new()
            .max_connections(max_connections)
            .acquire_timeout(std::time::Duration::from_secs(5))
            // Keep the default, but set it explicitly: a dropped connection (a
            // database restart, a pooler recycling clients) is only replaced
            // when the pool tests it on acquire. `tests/reconnect.rs` proves the
            // store wedges without this, so a future change must not turn it off.
            .test_before_acquire(true)
            .connect(&url)
            .await?;
        Ok(Self {
            pool,
            // Default 8, exactly as `nexus_lab_store.DEFAULT_MAX_ACTIVE_TASKS`.
            // `0` (or a negative) means "no cap", which the SQL then skips.
            max_active_tasks: std::env::var("NEXUS_LAB_MAX_ACTIVE_TASKS")
                .ok()
                .and_then(|v| v.trim().parse::<i64>().ok())
                .unwrap_or(8)
                .max(0),
        })
    }

    /// Wrap an existing pool (tests share one pool across cases).
    pub fn from_pool(pool: PgPool, max_active_tasks: i64) -> Self {
        Self {
            pool,
            max_active_tasks,
        }
    }

    pub fn pool(&self) -> &PgPool {
        &self.pool
    }

    /// Atomically claim the next claimable task of `run_id`, or `Nothing`.
    ///
    /// This is the whole transaction: the run gate, the global cap, the
    /// candidate selection with all three predicates, the per-branch advisory
    /// lock and re-check, and the fenced take - one `BEGIN ... COMMIT`.
    pub async fn claim_task(
        &self,
        run_id: Uuid,
        worker_id: &str,
        lease_seconds: i64,
    ) -> Result<Option<ClaimedTask>, sqlx::Error> {
        let mut tx = self.pool.begin().await?;

        // 1. Run gate: locked row, so a pause/cancel that committed first wins.
        let run_ok = sqlx::query(
            "SELECT 1 FROM nexus_runs \
             WHERE id = $1 AND state = 'running' AND iteration < max_iterations \
             AND (resume_at IS NULL OR resume_at <= now()) \
             AND (policy = 'durable' OR \
                  COALESCE(deadline_at, created_at + interval '24 hours') > now()) \
             FOR UPDATE",
        )
        .bind(run_id)
        .fetch_optional(&mut *tx)
        .await?;
        if run_ok.is_none() {
            tx.rollback().await?;
            return Ok(None);
        }

        // 2. Global backpressure cap, decided under one advisory lock so two
        //    workers cannot both take "the last place".
        if self.max_active_tasks > 0 {
            sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended('nexus-active-tasks', 0))")
                .execute(&mut *tx)
                .await?;
            let n: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM nexus_run_tasks \
                 WHERE state = ANY($1::text[])",
            )
            .bind(&["dispatched", "running", "verifying"][..])
            .fetch_one(&mut *tx)
            .await?;
            if n >= self.max_active_tasks {
                tx.rollback().await?;
                return Ok(None);
            }
        }

        // 3. Candidate selection: due + deps verified + branch free, oldest
        //    first, one at a time (`FOR UPDATE SKIP LOCKED`).
        let row = sqlx::query(&format!(
            "SELECT t.* FROM nexus_run_tasks t \
             WHERE t.run_id = $1 AND t.attempt < t.max_attempts \
             AND {due} AND {deps} AND {branch_free} \
             ORDER BY t.created_at, t.id LIMIT 1 FOR UPDATE SKIP LOCKED",
            due = TASK_DUE,
            deps = DEPS_VERIFIED,
            branch_free = BRANCH_FREE,
        ))
        .bind(run_id)
        .fetch_optional(&mut *tx)
        .await?;

        let Some(row) = row else {
            tx.rollback().await?;
            return Ok(None);
        };

        let task_id: Uuid = row.try_get("id")?;
        let repository: String = row.try_get("repository")?;
        let branch: String = row.try_get("branch")?;

        // 4. A repository task also takes its branch: another transaction may
        //    be reaching for it right now. One at a time, then look again.
        if !repository.is_empty() {
            sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
                .bind(format!("nexus-branch:{repository}#{branch}"))
                .execute(&mut *tx)
                .await?;
            let still_free = sqlx::query(&format!(
                "SELECT 1 FROM nexus_run_tasks t WHERE t.id = $1 AND {branch_free}",
                branch_free = BRANCH_FREE,
            ))
            .bind(task_id)
            .fetch_optional(&mut *tx)
            .await?;
            if still_free.is_none() {
                tx.rollback().await?;
                return Ok(None);
            }
        }

        // 5. Spend one iteration, take the task, hand out a fresh lease token.
        sqlx::query(
            "UPDATE nexus_runs SET iteration = iteration + 1, \
             total_iterations = total_iterations + 1, wait_reason = NULL, \
             claimed_at = COALESCE(claimed_at, now()), updated_at = now() \
             WHERE id = $1",
        )
        .bind(run_id)
        .execute(&mut *tx)
        .await?;

        let claimed = sqlx::query(
            "UPDATE nexus_run_tasks SET state = 'dispatched', attempt = attempt + 1, \
             conversation_id = NULL, retry_at = NULL, wait_reason = NULL, pending_start = NULL, \
             lease_owner = $1, lease_token = lease_token + 1, \
             lease_expires = now() + ($2 || ' seconds')::interval, updated_at = now() \
             WHERE id = $3 RETURNING *",
        )
        .bind(worker_id)
        .bind(lease_seconds.to_string())
        .bind(task_id)
        .fetch_one(&mut *tx)
        .await?;

        tx.commit().await?;
        Ok(Some(ClaimedTask {
            id: claimed.try_get("id")?,
            run_id: claimed.try_get("run_id")?,
            project_id: claimed.try_get("project_id")?,
            task_key: claimed.try_get("task_key")?,
            state: claimed.try_get("state")?,
            attempt: claimed.try_get("attempt")?,
            max_attempts: claimed.try_get("max_attempts")?,
            lease_token: claimed.try_get("lease_token")?,
            lease_owner: claimed.try_get("lease_owner")?,
            repository: claimed.try_get("repository")?,
            branch: claimed.try_get("branch")?,
            conversation_id: claimed.try_get("conversation_id")?,
        }))
    }

    /// Fenced mark-running: bind the conversation and move `dispatched`→`running`,
    /// but only while the task still carries `lease_token`. A stale worker's
    /// write matches no row and changes nothing (the row is returned as it is).
    pub async fn mark_running(
        &self,
        task_id: Uuid,
        conversation_id: &str,
        lease_token: i64,
    ) -> Result<Option<ClaimedTask>, sqlx::Error> {
        let mut tx = self.pool.begin().await?;
        let row = sqlx::query(
            "UPDATE nexus_run_tasks SET \
             state = CASE WHEN state = 'dispatched' THEN 'running' ELSE state END, \
             conversation_id = COALESCE($1, conversation_id), \
             started_at = now(), progress_at = now(), \
             pending_start = NULL, updated_at = now() \
             WHERE id = $2 AND lease_token = $3 RETURNING *",
        )
        .bind(conversation_id)
        .bind(task_id)
        .bind(lease_token)
        .fetch_optional(&mut *tx)
        .await?;
        let out = match row {
            Some(r) => Some(read_task(&r)?),
            None => {
                // The fence matched no row: return the task as it stands, if it
                // exists - the caller must see that its write did not land.
                let held = sqlx::query("SELECT * FROM nexus_run_tasks WHERE id = $1")
                    .bind(task_id)
                    .fetch_optional(&mut *tx)
                    .await?;
                held.as_ref().map(read_task).transpose()?
            }
        };
        tx.commit().await?;
        Ok(out)
    }

    /// Whether the task still belongs to `lease_token` (the pre-dispatch check).
    pub async fn holds_lease(&self, task_id: Uuid, lease_token: i64) -> Result<bool, sqlx::Error> {
        let ok: Option<i64> = sqlx::query_scalar(
            "SELECT lease_token FROM nexus_run_tasks WHERE id = $1 AND lease_token = $2",
        )
        .bind(task_id)
        .bind(lease_token)
        .fetch_optional(&self.pool)
        .await?;
        Ok(ok.is_some())
    }

    /// Reclaim expired-lease tasks: the faithful Python `reclaim_expired` lives
    /// in [`lifecycle`](crate::lifecycle) as `reclaim_expired_full` (it takes
    /// the run row lock first, and resets a durable task's cycle). This thin
    /// wrapper keeps the old name for callers that only want the count.
    pub async fn reclaim_expired(&self, run_id: Uuid) -> Result<u64, sqlx::Error> {
        let reclaimed = self
            .reclaim_expired_full(run_id)
            .await
            .map_err(|e| match e {
                crate::lifecycle::LifecycleError::Db(db) => db,
                _ => sqlx::Error::RowNotFound,
            })?;
        Ok(reclaimed.len() as u64)
    }
}

fn read_task(row: &sqlx::postgres::PgRow) -> Result<ClaimedTask, sqlx::Error> {
    Ok(ClaimedTask {
        id: row.try_get("id")?,
        run_id: row.try_get("run_id")?,
        project_id: row.try_get("project_id")?,
        task_key: row.try_get("task_key")?,
        state: row.try_get("state")?,
        attempt: row.try_get("attempt")?,
        max_attempts: row.try_get("max_attempts")?,
        lease_token: row.try_get("lease_token")?,
        lease_owner: row.try_get("lease_owner")?,
        repository: row.try_get("repository")?,
        branch: row.try_get("branch")?,
        conversation_id: row.try_get("conversation_id")?,
    })
}

/// `_TASK_DUE`, `_DEPS_VERIFIED`, `_BRANCH_FREE`: the claim-eligibility
/// predicates, read from the **same** `orchestration/sql/*.sql` files the
/// Python store uses. This crate once carried its own copies and drifted: the
/// copies predated `nexus_dispatch_intents` (migration 0043), so they would
/// re-claim a task whose dispatch was in doubt. Sharing the bytes is what
/// keeps Rust and Python from disagreeing on what is claimable.
pub const TASK_DUE: &str = include_str!("../../../../orchestration/sql/task_due.sql");
pub const DEPS_VERIFIED: &str =
    include_str!("../../../../orchestration/sql/dependencies_verified.sql");
pub const BRANCH_FREE: &str = include_str!("../../../../orchestration/sql/branch_free.sql");

/// A helper to run a closure inside a transaction (used by tests and callers
/// that need the same connection for setup).
pub async fn with_tx<F, T>(store: &Store, f: F) -> Result<T, sqlx::Error>
where
    F: for<'c> FnOnce(
        &'c mut Transaction<'_, Postgres>,
    ) -> std::pin::Pin<
        Box<dyn std::future::Future<Output = Result<T, sqlx::Error>> + Send + 'c>,
    >,
{
    let mut tx = store.pool.begin().await?;
    let out = f(&mut tx).await?;
    tx.commit().await?;
    Ok(out)
}
