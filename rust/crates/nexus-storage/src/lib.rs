//! PostgreSQL lab queue candidate. Not an HTTP endpoint or a running worker.
//! Authorization happens at the API boundary; this transaction is for trusted
//! schedulers. It never creates a conversation or edits migration history.
use sqlx::{PgPool, Row};
use std::num::NonZeroU32;
use uuid::Uuid;

/// Only a committed grant is returned. Carry its token into *every* later write.
#[derive(Debug, PartialEq, Eq)]
pub struct Grant {
    pub id: Uuid,
    pub lease_token: i64,
}

// Keep the expensive eligibility predicates in one place with the Python
// store, including the global branch check *after* the advisory lock wait.
const SELECT_TASK: &str = concat!(
    "SELECT t.id,t.repository,t.branch FROM nexus_run_tasks t ",
    "WHERE t.run_id=$1 AND t.project_id=$2 AND t.attempt<t.max_attempts AND ",
    include_str!("../../../../orchestration/sql/task_due.sql"),
    " AND ",
    include_str!("../../../../orchestration/sql/dependencies_verified.sql"),
    " AND ",
    include_str!("../../../../orchestration/sql/branch_free.sql"),
    " ORDER BY t.created_at,t.id LIMIT 1 FOR UPDATE SKIP LOCKED"
);
const RECHECK_BRANCH: &str = concat!(
    "SELECT 1 FROM nexus_run_tasks t WHERE t.id=$1 AND ",
    include_str!("../../../../orchestration/sql/branch_free.sql")
);

/// `active_limit` is deployment policy, shared with `max_active_tasks()` in the
/// Python scheduler (8 by default; 0 disables it). Not a client-supplied knob.
/// Use a direct/local PostgreSQL pool with bounded size and acquire timeout.
/// A missing/paused/foreign run or an unavailable task yields `Ok(None)`.
/// A database/commit error is **never** interpreted as an empty queue.
pub async fn claim_task(
    pool: &PgPool,
    project_id: Uuid,
    run_id: Uuid,
    worker: &str,
    lease_seconds: NonZeroU32,
    active_limit: u32,
) -> Result<Option<Grant>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    // Advisory-lock waiters must observe the previous holder's *commit* in
    // their next statement. Do not silently inherit REPEATABLE READ here.
    sqlx::query("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        .execute(&mut *tx)
        .await?;
    let run = sqlx::query(
        "SELECT id FROM nexus_runs WHERE id=$1 AND project_id=$2 AND state='running' \
         AND iteration<max_iterations AND (resume_at IS NULL OR resume_at<=now()) \
         AND (policy='durable' OR COALESCE(deadline_at,created_at + interval '86400 seconds')>now()) \
         FOR UPDATE",
    ).bind(run_id).bind(project_id).fetch_optional(&mut *tx).await?;
    if run.is_none() {
        tx.rollback().await?;
        return Ok(None);
    }
    if active_limit != 0 {
        sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended('nexus-active-tasks',0))")
            .execute(&mut *tx)
            .await?;
        let count: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM nexus_run_tasks WHERE state IN ('dispatched','running','verifying')",
        ).fetch_one(&mut *tx).await?;
        if count >= i64::from(active_limit) {
            tx.rollback().await?;
            return Ok(None);
        }
    }
    let row = sqlx::query(SELECT_TASK)
        .bind(run_id)
        .bind(project_id)
        .fetch_optional(&mut *tx)
        .await?;
    let Some(row) = row else {
        tx.rollback().await?;
        return Ok(None);
    };
    let id: Uuid = row.try_get("id")?;
    let repo: &str = row.try_get("repository")?;
    if !repo.is_empty() {
        let branch: &str = row.try_get("branch")?;
        sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1,0))")
            .bind(format!("nexus-branch:{repo}#{branch}"))
            .execute(&mut *tx)
            .await?;
        if sqlx::query(RECHECK_BRANCH)
            .bind(id)
            .fetch_optional(&mut *tx)
            .await?
            .is_none()
        {
            tx.rollback().await?;
            return Ok(None);
        }
    }
    sqlx::query("UPDATE nexus_runs SET iteration=iteration+1,total_iterations=total_iterations+1,\
                 wait_reason=NULL,claimed_at=COALESCE(claimed_at,now()),updated_at=now() WHERE id=$1")
        .bind(run_id).execute(&mut *tx).await?;
    let lease_token = sqlx::query_scalar(
        "UPDATE nexus_run_tasks SET state='dispatched',attempt=attempt+1,conversation_id=NULL,\
         retry_at=NULL,wait_reason=NULL,pending_start=NULL,lease_owner=$2,lease_token=lease_token+1,\
         lease_expires=now()+make_interval(secs=>$3),updated_at=now() WHERE id=$1 RETURNING lease_token",
    ).bind(id).bind(worker).bind(f64::from(lease_seconds.get())).fetch_one(&mut *tx).await?;
    tx.commit().await?;
    Ok(Some(Grant { id, lease_token }))
}

/// Recover at most `batch` expired, unbound starts from one project's run.
/// Return the revoked task IDs, *not* write grants. A new dispatch still needs
/// a new claim and must try adoption first. A bound conversation is never
/// reclaimed; polling it belongs to the ordinary recovery loop. Multiple
/// batches are intentional: no sweep may hold unbounded task locks/results.
pub async fn reclaim_expired(
    pool: &PgPool,
    project_id: Uuid,
    run_id: Uuid,
    batch: std::num::NonZeroU16,
) -> Result<Vec<Uuid>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    // Identical lock order to claim and Python pause/cancel: run, then tasks.
    let durable: Option<bool> = sqlx::query_scalar(
        "SELECT policy='durable' FROM nexus_runs WHERE id=$1 AND project_id=$2 FOR UPDATE",
    )
    .bind(run_id)
    .bind(project_id)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(durable) = durable else {
        tx.rollback().await?;
        return Ok(Vec::new());
    };
    let ids = sqlx::query_scalar(include_str!("reclaim_expired.sql"))
        .bind(run_id)
        .bind(project_id)
        .bind(i32::from(batch.get()))
        .bind(durable)
        .fetch_all(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(ids)
}
