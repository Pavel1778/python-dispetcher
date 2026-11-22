//! Run lifecycle transitions, stop/cancel, and lease recovery - in Rust.
//!
//! Every function here mirrors a named Python method on `LabStore`
//! (`orchestration/nexus_lab_store.py`) statement for statement: the same
//! state table, the same lock order (run row, then task rows, then execution
//! rows), the same stop signals and the same fencing. They exist so the two
//! implementations can be compared on real PostgreSQL before either one is
//! made authoritative (`tests/test_rust_lifecycle_parity.py`).
//!
//! The Python method each mirrors is named on the function. A change here that
//! is not also made there - or the reverse - fails the parity test.

use serde::{Deserialize, Serialize};
use sqlx::{Postgres, Row, Transaction};
use uuid::Uuid;

use crate::{Store, TaskRow};

/// The run state machine: for each target state, the states it may be entered
/// from. `_RUN_ENTERED_FROM` in Python. Asking for the state a run is already
/// in is a no-op, not an error.
pub const RUN_STATES: &[&str] = &[
    "running",
    "paused",
    "waiting_for_approval",
    "blocked",
    "completed",
    "failed",
    "cancelled",
];

pub fn entered_from(state: &str) -> Option<&'static [&'static str]> {
    Some(match state {
        "running" => &["paused", "blocked"],
        "paused" => &["running"],
        "waiting_for_approval" => &["running"],
        "blocked" => &["running"],
        "completed" => &["running"],
        "failed" => &["running"],
        "cancelled" => &["running", "paused", "waiting_for_approval", "blocked"],
        _ => return None,
    })
}

/// `TERMINAL_RUN_STATES`.
pub const TERMINAL_RUN_STATES: &[&str] = &["completed", "failed", "cancelled"];

/// `ACTIVE_RUN_STATES`: a run a worker may advance.
pub const ACTIVE_RUN_STATES: &[&str] = &["running", "paused", "waiting_for_approval", "blocked"];

/// `UNFINISHED_TASK_STATES`: states that still expect work.
pub const UNFINISHED_TASK_STATES: &[&str] =
    &["pending", "waiting", "dispatched", "running", "verifying"];

/// `IN_FLIGHT_TASK_STATES`: a conversation may be at work.
pub const IN_FLIGHT_TASK_STATES: &[&str] = &["dispatched", "running", "verifying"];

/// `_RETRYABLE_TASK_STATES`: what a person's "resume" may send back to the queue.
pub const RETRYABLE_TASK_STATES: &[&str] =
    &["completed_unverified", "partial", "failed", "blocked"];

/// A state a run may not be moved to from where it is.
#[derive(Debug, thiserror::Error)]
#[error("a {from} run cannot become {to}")]
pub struct InvalidTransition {
    pub from: String,
    pub to: String,
}

/// The run does not exist (or is not in the project).
#[derive(Debug, thiserror::Error)]
#[error("run not found")]
pub struct RunNotFound;

/// The task does not exist.
#[derive(Debug, thiserror::Error)]
#[error("task not found")]
pub struct TaskNotFound;

#[derive(Debug, thiserror::Error)]
pub enum LifecycleError {
    #[error(transparent)]
    Db(#[from] sqlx::Error),
    #[error(transparent)]
    Invalid(#[from] InvalidTransition),
    #[error(transparent)]
    RunNotFound(#[from] RunNotFound),
    #[error(transparent)]
    TaskNotFound(#[from] TaskNotFound),
}

/// The part of a run row the lifecycle needs.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunRow {
    pub id: Uuid,
    pub project_id: Uuid,
    pub goal_id: Uuid,
    pub state: String,
    pub policy: String,
    pub conversation_id: Option<Uuid>,
}

fn read_run(row: &sqlx::postgres::PgRow) -> Result<RunRow, sqlx::Error> {
    Ok(RunRow {
        id: row.try_get("id")?,
        project_id: row.try_get("project_id")?,
        goal_id: row.try_get("goal_id")?,
        state: row.try_get("state")?,
        policy: row.try_get("policy")?,
        conversation_id: row.try_get("conversation_id")?,
    })
}

/// How a state transition is requested.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Strictness {
    /// A user request: an illegal transition is an error (`set_run_state`).
    Strict,
    /// The scheduler: an illegal transition returns `None` (`set_run_state_internal`).
    Internal,
}

/// Whether a [`LifecycleError`] wraps a PostgreSQL deadlock (`40P01`). Python's
/// `_is_deadlock` retries exactly this; a serialization failure (`40001`) is not
/// a deadlock and is not retried, matching the Python behaviour.
pub fn is_deadlock(err: &LifecycleError) -> bool {
    if let LifecycleError::Db(sqlx::Error::Database(db)) = err {
        return db.code().as_deref() == Some("40P01");
    }
    false
}

/// The decision a run's state implies, mirroring `LabScheduler._decide`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Decision {
    /// Keep going; work is in flight or will be claimed next pass.
    Continue,
    /// The run is done: every task verified (the caller still confirms against
    /// GitHub before completing, as `_decide` defers to `_complete`).
    Completed,
    /// Move the run to this state with this reason.
    Settle { state: &'static str, reason: String },
    /// A durable run used its cycle's task starts: pause and renew (the caller
    /// applies `pause_for_budget`), do not fail.
    Pause { reason: String },
}

impl Store {
    /// Move a run to `state`, mirroring `LabStore.set_run_state` /
    /// `_apply_run_state`. Returns the new run row, or `None` when the move is
    /// not legal and `strict` is [`Strictness::Internal`].
    ///
    /// Like the Python method, a cancel settling a run against its tasks can
    /// deadlock with a worker settling one of those tasks; the aborted
    /// transaction commits nothing and every write inside is idempotent, so it
    /// is retried **once**. A second deadlock is a real defect and surfaces.
    pub async fn set_run_state(
        &self,
        project_id: Uuid,
        run_id: Uuid,
        state: &str,
        last_error: Option<&str>,
        strict: Strictness,
        actor: Option<&str>,
    ) -> Result<Option<RunRow>, LifecycleError> {
        if entered_from(state).is_none() {
            // Python raises ValueError("invalid run state"); model it as an
            // invalid transition with the same message shape.
            return Err(InvalidTransition {
                from: "?".into(),
                to: state.into(),
            }
            .into());
        }
        let mut attempt = 1;
        loop {
            let mut tx = self.pool().begin().await?;
            let out = self
                .apply_run_state(
                    &mut tx, project_id, run_id, state, last_error, strict, actor,
                )
                .await;
            match out {
                Ok(run) => {
                    tx.commit().await?;
                    return Ok(run);
                }
                Err(e) => {
                    tx.rollback().await.ok();
                    if attempt == 1 && is_deadlock(&e) {
                        attempt = 2;
                        continue;
                    }
                    return Err(e);
                }
            }
        }
    }

    /// `_apply_run_state`, on the caller's transaction.
    ///
    /// The parameter list mirrors the Python method one-for-one on purpose; a
    /// struct would hide that correspondence and the parity test relies on it.
    #[allow(clippy::too_many_arguments)]
    async fn apply_run_state(
        &self,
        tx: &mut Transaction<'_, Postgres>,
        project_id: Uuid,
        run_id: Uuid,
        state: &str,
        last_error: Option<&str>,
        strict: Strictness,
        actor: Option<&str>,
    ) -> Result<Option<RunRow>, LifecycleError> {
        // The run row is locked first, in the same order as claim_task.
        let row =
            sqlx::query("SELECT * FROM nexus_runs WHERE id = $1 AND project_id = $2 FOR UPDATE")
                .bind(run_id)
                .bind(project_id)
                .fetch_optional(&mut **tx)
                .await?
                .ok_or(RunNotFound)?;
        let current: String = row.try_get("state")?;
        if current == state {
            return Ok(Some(read_run(&row)?));
        }
        let allowed = entered_from(state).unwrap();
        if !allowed.contains(&current.as_str()) {
            if strict == Strictness::Internal {
                return Ok(None);
            }
            return Err(InvalidTransition {
                from: current,
                to: state.into(),
            }
            .into());
        }
        if state == "running" && current == "blocked" {
            let policy: String = row.try_get("policy")?;
            let new_cycle = policy == "durable";
            let n = self
                .requeue_retryable(tx, run_id, "resumed", new_cycle)
                .await?;
            if n == 0 {
                return Err(InvalidTransition {
                    from: current,
                    to: state.into(),
                }
                .into());
            }
        }
        if state == "cancelled" || state == "failed" {
            let why = if state == "cancelled" {
                "run cancelled".to_string()
            } else {
                format!("run failed: {}", last_error.unwrap_or("no reason given"))
            };
            // A conversation at work is owed a stop signal, then tasks are
            // blocked, then open executions are closed - the lock order every
            // worker also takes (run, tasks, executions).
            sqlx::query(
                "INSERT INTO nexus_executor_stops(run_id, task_id, project_id, conversation_id, \
                   openhands_account_id, repository, branch, reason) \
                 SELECT run_id, id, project_id, conversation_id, openhands_account_id, \
                   repository, branch, $1 FROM nexus_run_tasks \
                 WHERE run_id=$2 AND state = ANY($3) AND conversation_id IS NOT NULL \
                 ON CONFLICT (conversation_id) DO NOTHING",
            )
            .bind(truncate(&why, 200))
            .bind(run_id)
            .bind(IN_FLIGHT_TASK_STATES)
            .execute(&mut **tx)
            .await?;
            sqlx::query(
                "UPDATE nexus_run_tasks SET state='blocked', lease_expires=NULL, retry_at=NULL, \
                   lease_owner=NULL, lease_token=lease_token+1, \
                   wait_reason=NULL, resume_conversation_id=NULL, last_error=$1, updated_at=now() \
                 WHERE run_id=$2 AND state = ANY($3)",
            )
            .bind(truncate(&why, 500))
            .bind(run_id)
            .bind(UNFINISHED_TASK_STATES)
            .execute(&mut **tx)
            .await?;
            sqlx::query(
                "UPDATE nexus_task_executions SET ended_at=now(), outcome=$1, reason=$2 \
                 WHERE run_id=$3 AND ended_at IS NULL",
            )
            .bind(if state == "cancelled" {
                "cancelled"
            } else {
                "stopped"
            })
            .bind(truncate(&why, 500))
            .bind(run_id)
            .execute(&mut **tx)
            .await?;
        }
        // The run's own row, per state. The columns each branch writes are
        // exactly Python's.
        let updated = match state {
            "running" => {
                sqlx::query(
                    "UPDATE nexus_runs SET state=$1, last_error=NULL, resume_at=NULL, \
                   wait_reason=NULL, updated_at=now() WHERE id=$2 RETURNING *",
                )
                .bind(state)
                .bind(run_id)
                .fetch_one(&mut **tx)
                .await?
            }
            "cancelled" => {
                sqlx::query(
                    "UPDATE nexus_runs SET state=$1, last_error=COALESCE($2, last_error), \
                   resume_at=NULL, wait_reason=NULL, cancelled_at=now(), cancelled_by=$3, \
                   hidden_at=COALESCE(hidden_at, now()), \
                   completed_at=COALESCE(completed_at, now()), updated_at=now() \
                 WHERE id=$4 RETURNING *",
                )
                .bind(state)
                .bind(last_error)
                .bind(actor)
                .bind(run_id)
                .fetch_one(&mut **tx)
                .await?
            }
            "completed" => {
                sqlx::query(
                    "UPDATE nexus_runs SET state=$1, last_error=COALESCE($2, last_error), \
                   resume_at=NULL, wait_reason=NULL, \
                   hidden_at=COALESCE(hidden_at, now()), \
                   completed_at=COALESCE(completed_at, now()), updated_at=now() \
                 WHERE id=$3 RETURNING *",
                )
                .bind(state)
                .bind(last_error)
                .bind(run_id)
                .fetch_one(&mut **tx)
                .await?
            }
            "failed" => {
                sqlx::query(
                    "UPDATE nexus_runs SET state=$1, last_error=COALESCE($2, last_error), \
                   resume_at=NULL, wait_reason=NULL, \
                   completed_at=COALESCE(completed_at, now()), updated_at=now() \
                 WHERE id=$3 RETURNING *",
                )
                .bind(state)
                .bind(last_error)
                .bind(run_id)
                .fetch_one(&mut **tx)
                .await?
            }
            _ => {
                sqlx::query(
                    "UPDATE nexus_runs SET state=$1, last_error=COALESCE($2, last_error), \
                   resume_at=NULL, wait_reason=NULL, updated_at=now() \
                 WHERE id=$3 RETURNING *",
                )
                .bind(state)
                .bind(last_error)
                .bind(run_id)
                .fetch_one(&mut **tx)
                .await?
            }
        };
        let run = read_run(&updated)?;
        if state == "cancelled" {
            sqlx::query(
                "INSERT INTO nexus_project_events(project_id, actor, kind, resource_id) \
                 VALUES ($1,$2,'run.cancelled',$3)",
            )
            .bind(project_id)
            .bind(actor.unwrap_or("system"))
            .bind(run_id)
            .execute(&mut **tx)
            .await?;
        }
        if state == "blocked" {
            self.tell_chat(tx, &run, "Прогон заблокирован.", last_error.unwrap_or(""))
                .await?;
            sqlx::query(
                "INSERT INTO nexus_notifications(project_id, kind, title, resource_id) \
                 VALUES ($1,'run_blocked','Прогон заблокирован',$2)",
            )
            .bind(project_id)
            .bind(run_id)
            .execute(&mut **tx)
            .await?;
        }
        if TERMINAL_RUN_STATES.contains(&state) {
            // Mirror the terminal state onto the goal, notify it, and move the
            // originating conversation's workflow out of EXECUTING so a later
            // "launch" cannot start a second run of a spent plan - exactly as
            // `_apply_run_state` does.
            sqlx::query("UPDATE nexus_goals SET state=$1, updated_at=now() WHERE id=$2")
                .bind(state)
                .bind(run.goal_id)
                .execute(&mut **tx)
                .await?;
            sqlx::query(
                "INSERT INTO nexus_notifications(project_id, kind, title, resource_id) \
                 VALUES ($1,$2,$3,$4)",
            )
            .bind(project_id)
            .bind(format!("goal_{state}"))
            .bind(truncate(&format!("Цель: {state}"), 200))
            .bind(run_id)
            .execute(&mut **tx)
            .await?;
            if let Some(conv) = run.conversation_id {
                sqlx::query(
                    "UPDATE nexus_conversations SET workflow_state=$1, updated_at=now() \
                     WHERE id=$2 AND workflow_state IN ('EXECUTING','REPORTING')",
                )
                .bind(state.to_uppercase())
                .bind(conv)
                .execute(&mut **tx)
                .await?;
            }
        }
        Ok(Some(run))
    }

    /// `_requeue_retryable`: send every task that ended without verifying and
    /// still has attempts back to the queue. Returns the count.
    pub async fn requeue_retryable(
        &self,
        tx: &mut Transaction<'_, Postgres>,
        run_id: Uuid,
        why: &str,
        new_cycle: bool,
    ) -> Result<u64, sqlx::Error> {
        let res = sqlx::query(
            "UPDATE nexus_run_tasks SET state='pending', lease_expires=NULL, retry_at=NULL, \
               lease_owner=NULL, lease_token=lease_token+1, wait_reason=NULL, \
               evidence = evidence || jsonb_build_array(jsonb_build_object( \
                 'requeued', $1::text, 'attempt', attempt, 'was', state, \
                 'conversation_id', conversation_id)), \
               cycle = cycle + CASE WHEN attempt >= max_attempts THEN 1 ELSE 0 END, \
               tried_accounts = CASE WHEN attempt >= max_attempts THEN '[]'::jsonb \
                 ELSE tried_accounts END, \
               attempt = CASE WHEN attempt >= max_attempts THEN 0 ELSE attempt END, \
               conversation_id=NULL, verification=NULL, updated_at=now() \
             WHERE run_id=$2 AND state = ANY($3) AND (attempt < max_attempts OR $4)",
        )
        .bind(why)
        .bind(run_id)
        .bind(RETRYABLE_TASK_STATES)
        .bind(new_cycle)
        .execute(&mut **tx)
        .await?;
        Ok(res.rows_affected())
    }

    /// `_tell_chat`: append one assistant note about a run to its conversation.
    async fn tell_chat(
        &self,
        tx: &mut Transaction<'_, Postgres>,
        run: &RunRow,
        title: &str,
        reason: &str,
    ) -> Result<(), sqlx::Error> {
        let Some(conv) = run.conversation_id else {
            return Ok(());
        };
        let exists = sqlx::query("SELECT 1 FROM nexus_conversations WHERE id=$1 FOR KEY SHARE")
            .bind(conv)
            .fetch_optional(&mut **tx)
            .await?;
        if exists.is_none() {
            return Ok(());
        }
        let text = if reason.is_empty() {
            title.to_string()
        } else {
            format!("{title} {reason}")
        };
        sqlx::query(
            "INSERT INTO nexus_conversation_messages(conversation_id, role, content, meta, created_at) \
             VALUES ($1,'assistant',$2,$3, GREATEST(clock_timestamp(), COALESCE( \
               (SELECT max(created_at) FROM nexus_conversation_messages WHERE conversation_id=$1), \
               '-infinity'::timestamptz) + interval '1 microsecond'))",
        )
        .bind(conv)
        .bind(&text)
        .bind(serde_json::json!({"mode": "autonomous", "run_id": run.id.to_string(), "blocked": true}))
        .execute(&mut **tx)
        .await?;
        sqlx::query("UPDATE nexus_conversations SET updated_at=now(), run_id=COALESCE($1, run_id) WHERE id=$2")
            .bind(run.id)
            .bind(conv)
            .execute(&mut **tx)
            .await?;
        Ok(())
    }

    /// `release_task`: return a claimed task to `pending` (requeue) or `failed`.
    /// Only a task still `dispatched` is released; `lease_token` fences it.
    pub async fn release_task(
        &self,
        task_id: Uuid,
        error: &str,
        requeue: bool,
        failed_account_id: Option<Uuid>,
        lease_token: Option<i64>,
    ) -> Result<TaskRow, LifecycleError> {
        let state = if requeue { "pending" } else { "failed" };
        let mut tx = self.pool().begin().await?;
        let mut sql = String::from(
            "UPDATE nexus_run_tasks SET state=$1, lease_expires=NULL, \
               lease_owner=NULL, lease_token=lease_token+1, last_error=$2, \
               tried_accounts = CASE WHEN $3::uuid IS NULL THEN tried_accounts \
                 ELSE tried_accounts || to_jsonb($3::text) END, \
               updated_at=now() WHERE id=$4 AND state='dispatched'",
        );
        if lease_token.is_some() {
            sql.push_str(" AND lease_token=$5");
        }
        sql.push_str(" RETURNING *");
        let mut q = sqlx::query(&sql)
            .bind(state)
            .bind(error)
            .bind(failed_account_id)
            .bind(task_id);
        if let Some(t) = lease_token {
            q = q.bind(t);
        }
        let row = q.fetch_optional(&mut *tx).await?;
        let row = match row {
            Some(r) => r,
            None => sqlx::query("SELECT * FROM nexus_run_tasks WHERE id=$1")
                .bind(task_id)
                .fetch_optional(&mut *tx)
                .await?
                .ok_or(TaskNotFound)?,
        };
        let out = TaskRow::read(&row)?;
        tx.commit().await?;
        Ok(out)
    }
}

/// Arguments for [`Store::postpone_task`]: put a claimed task back because a
/// start was refused (a rate limit, a daily quota). Mirrors the keyword
/// arguments of the Python `LabStore.postpone_task`.
#[derive(Debug, Clone, Default)]
pub struct Postpone {
    pub error: String,
    pub seconds: f64,
    /// The account that refused, when another account can take the task.
    pub failed_account_id: Option<Uuid>,
    /// No account left to ask: empty `tried_accounts` so the whole pool is
    /// offered the task again after the wait.
    pub forget_tried: bool,
    /// Fences the postpone on the claim's token.
    pub lease_token: Option<i64>,
}

impl Store {
    /// `postpone_task`: put a claimed task back because a start was refused,
    /// giving back the attempt and the iteration. `lease_token` fences it.
    pub async fn postpone_task(
        &self,
        task_id: Uuid,
        opts: &Postpone,
    ) -> Result<TaskRow, LifecycleError> {
        let error = opts.error.as_str();
        let seconds = opts.seconds;
        let failed_account_id = opts.failed_account_id;
        let forget_tried = opts.forget_tried;
        let lease_token = opts.lease_token;
        let mut tx = self.pool().begin().await?;
        let found = sqlx::query("SELECT run_id FROM nexus_run_tasks WHERE id=$1")
            .bind(task_id)
            .fetch_optional(&mut *tx)
            .await?
            .ok_or(TaskNotFound)?;
        let run_id: Uuid = found.try_get("run_id")?;
        // Run row first, as everywhere that locks both.
        sqlx::query("SELECT id FROM nexus_runs WHERE id=$1 FOR UPDATE")
            .bind(run_id)
            .fetch_one(&mut *tx)
            .await?;
        let wait = seconds.max(0.0);
        let mut sql = String::from(
            "UPDATE nexus_run_tasks SET state='pending', attempt=GREATEST(attempt - 1, 0), \
               lease_expires=now() + ($1 || ' seconds')::interval, last_error=$2, \
               retry_at = CASE WHEN $3 > 0 THEN now() + ($4 || ' seconds')::interval ELSE NULL END, \
               refusals = refusals + 1, lease_owner=NULL, lease_token=lease_token+1, \
               tried_accounts = CASE WHEN $5 THEN '[]'::jsonb \
                 WHEN $6::text IS NULL OR tried_accounts ? $6::text THEN tried_accounts \
                 ELSE tried_accounts || to_jsonb($6::text) END, \
               updated_at=now() WHERE id=$7 AND state='dispatched'",
        );
        if lease_token.is_some() {
            sql.push_str(" AND lease_token=$8");
        }
        sql.push_str(" RETURNING *");
        let wait_s = wait.to_string();
        let mut q = sqlx::query(&sql)
            .bind(&wait_s)
            .bind(error)
            .bind(wait)
            .bind(&wait_s)
            .bind(forget_tried)
            .bind(failed_account_id.map(|u| u.to_string()))
            .bind(task_id);
        if let Some(t) = lease_token {
            q = q.bind(t);
        }
        let row = q.fetch_optional(&mut *tx).await?;
        let row = match row {
            Some(r) => {
                sqlx::query(
                    "UPDATE nexus_runs SET iteration=GREATEST(iteration - 1, 0), \
                       total_iterations=GREATEST(total_iterations - 1, 0), updated_at=now() \
                     WHERE id=$1",
                )
                .bind(run_id)
                .execute(&mut *tx)
                .await?;
                r
            }
            None => sqlx::query("SELECT * FROM nexus_run_tasks WHERE id=$1")
                .bind(task_id)
                .fetch_optional(&mut *tx)
                .await?
                .ok_or(TaskNotFound)?,
        };
        let out = TaskRow::read(&row)?;
        tx.commit().await?;
        Ok(out)
    }

    /// `reclaim_expired`: requeue tasks a worker claimed and never started (it
    /// died in between). Only a task with **no conversation bound** is taken
    /// back. Mirrors the Python method, including the durable-run cycle reset.
    pub async fn reclaim_expired_full(&self, run_id: Uuid) -> Result<Vec<TaskRow>, LifecycleError> {
        let mut tx = self.pool().begin().await?;
        // The run row is locked first, in the same order every site takes.
        let run = sqlx::query("SELECT policy FROM nexus_runs WHERE id=$1 FOR UPDATE")
            .bind(run_id)
            .fetch_optional(&mut *tx)
            .await?;
        let durable = run
            .as_ref()
            .map(|r| r.try_get::<String, _>("policy").map(|p| p == "durable"))
            .transpose()?
            .unwrap_or(false);
        let rows = sqlx::query(
            "SELECT * FROM nexus_run_tasks WHERE run_id=$1 AND state IN ('dispatched','running') \
             AND conversation_id IS NULL \
             AND lease_expires IS NOT NULL AND lease_expires < now() FOR UPDATE",
        )
        .bind(run_id)
        .fetch_all(&mut *tx)
        .await?;
        let mut out = Vec::with_capacity(rows.len());
        for row in rows {
            let task = TaskRow::read(&row)?;
            let updated = if task.attempt >= task.max_attempts && durable {
                sqlx::query(
                    "UPDATE nexus_run_tasks SET state='pending', lease_expires=NULL, \
                       attempt=0, cycle=cycle+1, tried_accounts='[]'::jsonb, \
                       evidence = evidence || jsonb_build_array(jsonb_build_object( \
                         'reclaimed', true, 'attempt', attempt, 'cycle', cycle)), \
                       last_error='lease expired', updated_at=now() WHERE id=$1 RETURNING *",
                )
                .bind(task.id)
                .fetch_one(&mut *tx)
                .await?
            } else if task.attempt >= task.max_attempts {
                sqlx::query(
                    "UPDATE nexus_run_tasks SET state='failed', lease_expires=NULL, \
                       lease_owner=NULL, lease_token=lease_token+1, \
                       last_error='lease expired after max attempts', updated_at=now() \
                     WHERE id=$1 RETURNING *",
                )
                .bind(task.id)
                .fetch_one(&mut *tx)
                .await?
            } else {
                sqlx::query(
                    "UPDATE nexus_run_tasks SET state='pending', lease_expires=NULL, \
                       lease_owner=NULL, lease_token=lease_token+1, \
                       evidence = evidence || jsonb_build_array(jsonb_build_object( \
                         'reclaimed', true, 'attempt', attempt, 'cycle', cycle)), \
                       last_error='lease expired', updated_at=now() WHERE id=$1 RETURNING *",
                )
                .bind(task.id)
                .fetch_one(&mut *tx)
                .await?
            };
            out.push(TaskRow::read(&updated)?);
        }
        tx.commit().await?;
        Ok(out)
    }

    /// `record_verification`: set the terminal task state from a verifier
    /// verdict. Only an in-flight task is settled; `lease_token` fences it.
    /// Mirrors the Python method, including closing the open execution and the
    /// `task_<verdict>` notification.
    pub async fn record_verification(
        &self,
        task_id: Uuid,
        verdict: &str,
        evidence: serde_json::Value,
        detail: Option<serde_json::Value>,
        lease_token: Option<i64>,
    ) -> Result<TaskRow, LifecycleError> {
        if !matches!(
            verdict,
            "completed_verified"
                | "completed_no_changes"
                | "completed_unverified"
                | "partial"
                | "failed"
                | "blocked"
        ) {
            return Err(InvalidTransition {
                from: "?".into(),
                to: verdict.into(),
            }
            .into());
        }
        let mut tx = self.pool().begin().await?;
        let mut sql = String::from(
            "UPDATE nexus_run_tasks SET state=$1, verification=$2, \
               evidence = evidence || $3::jsonb, lease_expires=NULL, \
               lease_owner=NULL, updated_at=now() \
             WHERE id=$4 AND state IN ('dispatched','running','verifying')",
        );
        if lease_token.is_some() {
            sql.push_str(" AND lease_token=$5");
        }
        sql.push_str(" RETURNING *");
        let mut q = sqlx::query(&sql)
            .bind(verdict)
            .bind(detail.clone().unwrap_or(serde_json::json!({})))
            .bind(serde_json::json!([evidence]))
            .bind(task_id);
        if let Some(t) = lease_token {
            q = q.bind(t);
        }
        let row = q.fetch_optional(&mut *tx).await?;
        let row = match row {
            Some(r) => {
                // Close the open execution of this attempt.
                sqlx::query(
                    "UPDATE nexus_task_executions SET ended_at=now(), outcome=$1, reason=$2, \
                       head_sha=COALESCE($3, head_sha) WHERE task_id=$4 AND ended_at IS NULL",
                )
                .bind(truncate(verdict, 60))
                .bind(truncate(
                    evidence
                        .get("reason")
                        .and_then(|v| v.as_str())
                        .unwrap_or(""),
                    1000,
                ))
                .bind(evidence.get("remote_sha").and_then(|v| v.as_str()))
                .bind(task_id)
                .execute(&mut *tx)
                .await?;
                let _task = TaskRow::read(&r)?;
                sqlx::query(
                    "INSERT INTO nexus_notifications(project_id, kind, title, resource_id) \
                     SELECT project_id, $1, title, id FROM nexus_run_tasks WHERE id=$2",
                )
                .bind(format!("task_{verdict}"))
                .bind(task_id)
                .execute(&mut *tx)
                .await?;
                r
            }
            None => sqlx::query("SELECT * FROM nexus_run_tasks WHERE id=$1")
                .bind(task_id)
                .fetch_optional(&mut *tx)
                .await?
                .ok_or(TaskNotFound)?,
        };
        let out = TaskRow::read(&row)?;
        tx.commit().await?;
        Ok(out)
    }

    /// `run_progress`: how many of the run's tasks are in each state.
    pub async fn run_progress(
        &self,
        run_id: Uuid,
    ) -> Result<std::collections::HashMap<String, i64>, sqlx::Error> {
        let rows: Vec<(String, i64)> = sqlx::query_as(
            "SELECT state, count(*)::bigint FROM nexus_run_tasks WHERE run_id=$1 GROUP BY state",
        )
        .bind(run_id)
        .fetch_all(&self.pool)
        .await?;
        Ok(rows.into_iter().collect())
    }

    /// `list_tasks_internal`: every task of a run, oldest first.
    pub async fn list_tasks_internal(&self, run_id: Uuid) -> Result<Vec<TaskRow>, sqlx::Error> {
        let rows =
            sqlx::query("SELECT * FROM nexus_run_tasks WHERE run_id=$1 ORDER BY created_at, id")
                .bind(run_id)
                .fetch_all(&self.pool)
                .await?;
        rows.iter().map(TaskRow::read).collect()
    }

    /// `active_run_ids`: the runs a worker should advance, oldest first.
    pub async fn active_run_ids(&self, limit: i64) -> Result<Vec<Uuid>, sqlx::Error> {
        sqlx::query_scalar(
            "SELECT id FROM nexus_runs WHERE state = ANY($1) \
             ORDER BY created_at, id LIMIT $2",
        )
        .bind(crate::lifecycle::ACTIVE_RUN_STATES)
        .bind(limit)
        .fetch_all(&self.pool)
        .await
    }

    /// Whether the run is `running` and not pausing (`resume_at` in the past or
    /// none). The loop only claims from such a run.
    pub async fn run_claimable(&self, run_id: Uuid) -> Result<bool, sqlx::Error> {
        let ok: Option<i64> = sqlx::query_scalar(
            "SELECT 1::bigint FROM nexus_runs WHERE id=$1 AND state='running' \
             AND (resume_at IS NULL OR resume_at <= now())",
        )
        .bind(run_id)
        .fetch_optional(&self.pool)
        .await?;
        Ok(ok.is_some())
    }

    /// The run's policy (`durable` or `bounded`).
    pub async fn run_policy(&self, run_id: Uuid) -> Result<String, sqlx::Error> {
        sqlx::query_scalar("SELECT policy FROM nexus_runs WHERE id=$1")
            .bind(run_id)
            .fetch_one(&self.pool)
            .await
    }

    /// The run's project id (needed for `set_run_state`, which is
    /// project-scoped in the Python store).
    pub async fn run_project(&self, run_id: Uuid) -> Result<Uuid, sqlx::Error> {
        sqlx::query_scalar("SELECT project_id FROM nexus_runs WHERE id=$1")
            .bind(run_id)
            .fetch_one(&self.pool)
            .await
    }

    /// `has_claimable`: whether any pending/waiting task has attempts left and
    /// every dependency verified - i.e. it can be claimed now or once its wait
    /// ends. Tells a *waiting* run from a *deadlocked* one. Mirrors
    /// `nexus_lab_store.has_claimable`.
    pub async fn has_claimable(&self, run_id: Uuid) -> Result<bool, sqlx::Error> {
        let n: i64 = sqlx::query_scalar(
            "SELECT count(*)::bigint FROM nexus_run_tasks t \
             WHERE t.run_id=$1 AND t.state IN ('pending','waiting') \
             AND t.attempt < t.max_attempts AND \
             NOT EXISTS ( \
               SELECT 1 FROM jsonb_array_elements_text(t.dependencies) AS d(dep) \
               WHERE NOT EXISTS ( \
                 SELECT 1 FROM nexus_run_tasks dep \
                 WHERE dep.run_id = t.run_id AND dep.task_key = d.dep \
                   AND dep.state = ANY(ARRAY['completed_verified','completed_no_changes'])))",
        )
        .bind(run_id)
        .fetch_one(&self.pool)
        .await?;
        Ok(n > 0)
    }

    /// `blocked_ancestry`: whether a task that still expects work waits,
    /// directly or through others, for a task that ended without verifying and
    /// will not be tried again. Mirrors `nexus_lab_store.blocked_ancestry`.
    pub async fn blocked_ancestry(&self, run_id: Uuid) -> Result<bool, sqlx::Error> {
        let n: i64 = sqlx::query_scalar(
            "SELECT count(*)::bigint FROM nexus_run_tasks t \
             WHERE t.run_id=$1 AND t.state IN ('pending','waiting') AND EXISTS ( \
               SELECT 1 FROM jsonb_array_elements_text(t.dependencies) AS d(dep) \
               JOIN nexus_run_tasks dep ON dep.run_id = t.run_id AND dep.task_key = d.dep \
               WHERE dep.state = ANY($2))",
        )
        .bind(run_id)
        .bind(crate::lifecycle::RETRYABLE_TASK_STATES)
        .fetch_one(&self.pool)
        .await?;
        Ok(n > 0)
    }

    /// `budget_spent`: which of the run's limits stops new work, if any. Read on
    /// the database's clock, as the Python method does.
    pub async fn budget_spent(&self, run_id: Uuid) -> Result<Option<String>, sqlx::Error> {
        let row = sqlx::query(
            "SELECT iteration, max_iterations, policy, \
               COALESCE(deadline_at, created_at + interval '86400 seconds') <= now() AS late \
             FROM nexus_runs WHERE id=$1",
        )
        .bind(run_id)
        .fetch_optional(&self.pool)
        .await?;
        let Some(row) = row else { return Ok(None) };
        let iteration: i32 = row.try_get("iteration")?;
        let max_iterations: i32 = row.try_get("max_iterations")?;
        let policy: String = row.try_get("policy")?;
        let late: bool = row.try_get("late")?;
        if iteration >= max_iterations {
            return Ok(Some(format!(
                "iteration budget used: {iteration} of {max_iterations} task starts"
            )));
        }
        if late && policy != "durable" {
            return Ok(Some("deadline passed".to_string()));
        }
        Ok(None)
    }

    /// `run_pausing`: whether the run is inside a pause of its own (`resume_at`).
    pub async fn run_pausing(&self, run_id: Uuid) -> Result<bool, sqlx::Error> {
        let row = sqlx::query(
            "SELECT (resume_at IS NOT NULL AND resume_at > now()) AS pausing \
             FROM nexus_runs WHERE id=$1",
        )
        .bind(run_id)
        .fetch_optional(&self.pool)
        .await?;
        Ok(row
            .map(|r| r.try_get::<bool, _>("pausing").unwrap_or(false))
            .unwrap_or(false))
    }

    /// The run's current state (`running`, `blocked`, ...).
    pub async fn run_state(&self, run_id: Uuid) -> Result<String, sqlx::Error> {
        sqlx::query_scalar("SELECT state FROM nexus_runs WHERE id=$1")
            .bind(run_id)
            .fetch_one(&self.pool)
            .await
    }

    /// The run's policy (`durable` or `bounded`).
    pub async fn run_policy_of(&self, run_id: Uuid) -> Result<String, sqlx::Error> {
        sqlx::query_scalar("SELECT policy FROM nexus_runs WHERE id=$1")
            .bind(run_id)
            .fetch_one(&self.pool)
            .await
    }

    /// A complete mirror of `LabScheduler._decide`, backed by the store.
    ///
    /// Returns the decision for a run from its task counts plus the
    /// deadlock/budget probes. `Completed` means "every task verified" - the
    /// caller still confirms against GitHub before completing, exactly as
    /// `_decide` defers to `_complete`. `Pause` is the durable budget-pause
    /// side effect (`pause_for_budget`), which the caller applies.
    pub async fn decide_run(&self, run_id: Uuid) -> Result<Decision, sqlx::Error> {
        let counts = self.run_progress(run_id).await?;
        let total: i64 = counts.values().sum();
        if total == 0 {
            return Ok(Decision::Settle {
                state: "failed",
                reason: "the run has no tasks".into(),
            });
        }
        let accepted: i64 = counts.get("completed_verified").copied().unwrap_or(0)
            + counts.get("completed_no_changes").copied().unwrap_or(0);
        if accepted == total {
            return Ok(Decision::Completed);
        }
        let unfinished: i64 = UNFINISHED_TASK_STATES
            .iter()
            .map(|s| counts.get(*s).copied().unwrap_or(0))
            .sum();
        if unfinished == 0 {
            return Ok(Decision::Settle {
                state: "blocked",
                reason: "no claimable tasks; some tasks not verified".into(),
            });
        }
        let in_flight: i64 = IN_FLIGHT_TASK_STATES
            .iter()
            .map(|s| counts.get(*s).copied().unwrap_or(0))
            .sum();
        if in_flight > 0 {
            return Ok(Decision::Continue);
        }
        let has_unverified = total > accepted + unfinished;
        let spent = self.budget_spent(run_id).await?;
        let durable = self.run_policy_of(run_id).await? == "durable";
        if durable {
            if has_unverified
                && self.blocked_ancestry(run_id).await?
                && !self.has_claimable(run_id).await?
            {
                return Ok(Decision::Settle {
                    state: "blocked",
                    reason: "dependency graph deadlocked: a dependency is not verified".into(),
                });
            }
            if spent.is_some() && !self.run_pausing(run_id).await? {
                // The task starts of this cycle are used: pause and renew, do
                // not fail. The caller applies the pause (pause_for_budget).
                return Ok(Decision::Pause {
                    reason: spent.unwrap_or_default(),
                });
            }
            return Ok(Decision::Continue);
        }
        if let Some(spent) = spent {
            return Ok(Decision::Settle {
                state: "failed",
                reason: spent,
            });
        }
        if !self.has_claimable(run_id).await? {
            return Ok(Decision::Settle {
                state: "blocked",
                reason: "dependency graph deadlocked: a dependency is not verified".into(),
            });
        }
        Ok(Decision::Continue)
    }

    /// `ProjectService.list`: the caller's non-archived, non-deleted projects,
    /// newest first. Same columns and order as the Python method.
    pub async fn list_projects(
        &self,
        principal: &str,
    ) -> Result<Vec<serde_json::Value>, sqlx::Error> {
        let rows = sqlx::query(
            "SELECT p.id, p.name, p.description, p.archived_at, p.deleted_at, \
               p.quality_gate, p.created_at, p.updated_at \
             FROM nexus_projects p JOIN nexus_project_members m ON m.project_id=p.id \
             WHERE m.principal=$1 AND p.deleted_at IS NULL AND p.archived_at IS NULL \
             ORDER BY p.created_at DESC, p.id",
        )
        .bind(principal)
        .fetch_all(&self.pool)
        .await?;
        Ok(rows.iter().map(row_to_json).collect())
    }

    /// `ProjectService.get`: one project the caller is a member of, or `None`.
    pub async fn project_for_member(
        &self,
        principal: &str,
        project_id: Uuid,
    ) -> Result<Option<serde_json::Value>, sqlx::Error> {
        let row = sqlx::query(
            "SELECT p.id, p.name, p.description, p.archived_at, p.deleted_at, \
               p.quality_gate, p.created_at, p.updated_at \
             FROM nexus_projects p JOIN nexus_project_members m ON m.project_id=p.id \
             WHERE p.id=$1 AND m.principal=$2 FOR KEY SHARE OF m",
        )
        .bind(project_id)
        .bind(principal)
        .fetch_optional(&self.pool)
        .await?;
        Ok(row.as_ref().map(row_to_json))
    }

    /// The project's active runs (not hidden), newest first, as `_run_view`
    /// shows them. `None` when the caller is not a member.
    pub async fn list_runs_for_member(
        &self,
        principal: &str,
        project_id: Uuid,
    ) -> Result<Option<Vec<serde_json::Value>>, sqlx::Error> {
        // Membership first: a non-member learns nothing (Python's 404).
        let member: Option<i64> = sqlx::query_scalar(
            "SELECT 1::bigint FROM nexus_project_members WHERE project_id=$1 AND principal=$2",
        )
        .bind(project_id)
        .bind(principal)
        .fetch_optional(&self.pool)
        .await?;
        if member.is_none() {
            return Ok(None);
        }
        let rows = sqlx::query(
            "SELECT id, project_id, goal_id, state, policy, iteration, max_iterations, \
               max_attempts_per_task, deadline_at, last_error, created_by, created_at, \
               updated_at, hidden_at, completed_at, cancelled_at, cancelled_by, \
               conversation_id, cycle, total_iterations, resume_at, wait_reason \
             FROM nexus_runs WHERE project_id=$1 AND hidden_at IS NULL \
             ORDER BY created_at DESC, id",
        )
        .bind(project_id)
        .fetch_all(&self.pool)
        .await?;
        Ok(Some(rows.iter().map(run_view_json).collect()))
    }
}

/// A row as a JSON object, timestamps as RFC3339 strings (the shape Python's
/// psycopg `dict(row)` serialises to through `json.dumps`).
fn row_to_json(row: &sqlx::postgres::PgRow) -> serde_json::Value {
    use sqlx::{Column, Row, TypeInfo};
    let mut map = serde_json::Map::new();
    for (i, col) in row.columns().iter().enumerate() {
        let name = col.name().to_string();
        let ty = col.type_info().name();
        let val = match ty {
            "UUID" => row
                .try_get::<Option<Uuid>, _>(i)
                .ok()
                .flatten()
                .map(|u| serde_json::Value::String(u.to_string()))
                .unwrap_or(serde_json::Value::Null),
            "TIMESTAMPTZ" | "TIMESTAMP" => row
                .try_get::<Option<chrono::DateTime<chrono::Utc>>, _>(i)
                .ok()
                .flatten()
                .map(|t| serde_json::Value::String(t.to_rfc3339()))
                .unwrap_or(serde_json::Value::Null),
            "BOOL" => row
                .try_get::<Option<bool>, _>(i)
                .ok()
                .flatten()
                .map(serde_json::Value::Bool)
                .unwrap_or(serde_json::Value::Null),
            "INT4" => row
                .try_get::<Option<i32>, _>(i)
                .ok()
                .flatten()
                .map(|n| serde_json::json!(n))
                .unwrap_or(serde_json::Value::Null),
            "INT8" => row
                .try_get::<Option<i64>, _>(i)
                .ok()
                .flatten()
                .map(|n| serde_json::json!(n))
                .unwrap_or(serde_json::Value::Null),
            "JSONB" | "JSON" => row
                .try_get::<Option<serde_json::Value>, _>(i)
                .ok()
                .flatten()
                .unwrap_or(serde_json::Value::Null),
            _ => row
                .try_get::<Option<String>, _>(i)
                .ok()
                .flatten()
                .map(serde_json::Value::String)
                .unwrap_or(serde_json::Value::Null),
        };
        map.insert(name, val);
    }
    serde_json::Value::Object(map)
}

/// `_run_view`: a run row with `hidden` and the durable-deadline rule applied.
fn run_view_json(row: &sqlx::postgres::PgRow) -> serde_json::Value {
    let mut v = row_to_json(row);
    if let Some(obj) = v.as_object_mut() {
        let hidden = obj.get("hidden_at").map(|h| !h.is_null()).unwrap_or(false);
        obj.insert("hidden".into(), serde_json::Value::Bool(hidden));
        if obj.get("policy").and_then(|p| p.as_str()) == Some("durable") {
            obj.insert("deadline_at".into(), serde_json::Value::Null);
        }
    }
    v
}

/// Truncate to at most `n` characters (the Python slices are on characters).
fn truncate(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}
