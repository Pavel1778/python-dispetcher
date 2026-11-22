//! `nexus-worker` - the autonomous loop, in Rust.
//!
//! One pass (`tick`) does what `LabScheduler.tick` does in Python, in the same
//! order, over the same store: for every active run, reclaim expired leases,
//! claim a due task, hand it to the executor, settle the verdict, and decide
//! the run's fate. The *decision* is the pure [`decide`] function; the store
//! operations are the parity-proven `nexus-store` methods.
//!
//! ## What is and is not migrated here
//!
//! - **Migrated and parity-tested**: the claim (`Store::claim_task`), the
//!   lifecycle transitions (`nexus-store::lifecycle`), reclaim, the run
//!   decision ([`decide`], compared with `LabScheduler._decide`), and the loop
//!   that ties them together.
//! - **Not migrated (Phase 3)**: *executing* a task - starting an OpenHands
//!   conversation, polling it, verifying the commit against GitHub. That is the
//!   [`Executor`] trait's job. This crate ships a real trait and a real loop
//!   over it; it does **not** ship an OpenHands client. A caller supplies one.
//!   Nothing here pretends to be an OpenHands integration.

use async_trait::async_trait;
use nexus_store::lifecycle::Strictness;
use nexus_store::{ClaimedTask, Store};

/// What an executor did with one task.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    /// The work is done and the result verifies against the repository.
    Verified {
        reason: String,
        commit: Option<String>,
    },
    /// The work is done but nothing observable confirms it (a text answer that
    /// names no commit). Never counted as success.
    Unverified { reason: String },
    /// The attempt failed; it may be retried.
    Failed { reason: String },
    /// The task cannot proceed (no usable account, a refused dispatch). The
    /// run is blocked, not spun.
    Blocked { reason: String },
}

impl Outcome {
    /// The task state this outcome settles to (`record_verification`'s verdict).
    pub fn verdict(&self) -> &'static str {
        match self {
            Outcome::Verified { .. } => "completed_verified",
            Outcome::Unverified { .. } => "completed_unverified",
            Outcome::Failed { .. } => "failed",
            Outcome::Blocked { .. } => "blocked",
        }
    }

    pub fn reason(&self) -> &str {
        match self {
            Outcome::Verified { reason, .. }
            | Outcome::Unverified { reason }
            | Outcome::Failed { reason }
            | Outcome::Blocked { reason } => reason,
        }
    }

    pub fn commit(&self) -> Option<&str> {
        match self {
            Outcome::Verified { commit, .. } => commit.as_deref(),
            _ => None,
        }
    }
}

/// The seam to the real executor (OpenHands + GitHub). Phase 3 supplies a
/// production implementation; tests supply a deterministic one.
#[async_trait]
pub trait Executor: Send + Sync {
    /// Run one claimed task to an outcome. It must not decide the run's fate;
    /// it only reports what happened to this task.
    async fn execute(&self, task: &ClaimedTask) -> Outcome;
}

// The decision is a store-level concept (it needs the deadlock/budget probes);
// re-export it so callers keep using `nexus_worker::Decision`.
pub use nexus_store::lifecycle::Decision;

/// The worker: a store, an executor, and an identity.
pub struct Worker<E: Executor> {
    store: Store,
    executor: E,
    id: String,
    lease_seconds: i64,
}

/// What one pass did, for logging and tests.
#[derive(Debug, Default, Clone)]
pub struct TickReport {
    pub reclaimed: usize,
    pub claimed: usize,
    pub settled: usize,
    pub runs_completed: usize,
    pub runs_blocked: usize,
}

impl<E: Executor> Worker<E> {
    pub fn new(store: Store, executor: E, id: impl Into<String>) -> Self {
        Self {
            store,
            executor,
            id: id.into(),
            lease_seconds: 900,
        }
    }

    pub fn with_lease_seconds(mut self, seconds: i64) -> Self {
        self.lease_seconds = seconds;
        self
    }

    /// One pass over every active run: reclaim, claim, execute, settle, decide.
    pub async fn tick(&self) -> Result<TickReport, WorkerError> {
        let mut report = TickReport::default();
        let runs = self.store.active_run_ids(100).await?;
        tracing::debug!(worker = %self.id, active_runs = runs.len(), "worker tick");
        for run_id in runs {
            // Structured span: run + worker. The project id is filled once the
            // run's project is known. Never log the DSN or any secret.
            let span = tracing::info_span!("run", run_id = %run_id, worker = %self.id);
            let _enter = span.enter();

            let reclaimed = self.store.reclaim_expired_full(run_id).await?;
            report.reclaimed += reclaimed.len();
            if !reclaimed.is_empty() {
                tracing::info!(count = reclaimed.len(), "reclaimed expired leases");
            }

            if self.store.run_claimable(run_id).await? {
                if let Some(task) = self
                    .store
                    .claim_task(run_id, &self.id, self.lease_seconds)
                    .await?
                {
                    report.claimed += 1;
                    // Identifiers the mission names, no secrets: task, project,
                    // lease generation, and (when bound) the conversation.
                    tracing::info!(
                        task_id = %task.id,
                        project_id = %task.project_id,
                        task_key = %task.task_key,
                        lease_token = task.lease_token,
                        "claimed task"
                    );
                    let outcome = self.executor.execute(&task).await;
                    let evidence = serde_json::json!({
                        "reason": outcome.reason(),
                        "remote_sha": outcome.commit(),
                    });
                    self.store
                        .record_verification(
                            task.id,
                            outcome.verdict(),
                            evidence,
                            None,
                            Some(task.lease_token),
                        )
                        .await?;
                    tracing::info!(
                        task_id = %task.id,
                        verdict = outcome.verdict(),
                        lease_token = task.lease_token,
                        "settled task"
                    );
                    report.settled += 1;
                }
            }

            let decision = self.store.decide_run(run_id).await?;
            match decision {
                Decision::Completed => {
                    let project = self.store.run_project(run_id).await?;
                    self.store
                        .set_run_state(
                            project,
                            run_id,
                            "completed",
                            None,
                            Strictness::Internal,
                            None,
                        )
                        .await?;
                    tracing::info!(project_id = %project, "run completed");
                    report.runs_completed += 1;
                }
                Decision::Settle { state, reason } => {
                    let project = self.store.run_project(run_id).await?;
                    let moved = self
                        .store
                        .set_run_state(
                            project,
                            run_id,
                            state,
                            Some(&reason),
                            Strictness::Internal,
                            None,
                        )
                        .await?;
                    if moved.is_some() {
                        tracing::info!(project_id = %project, state, reason = %reason, "run settled");
                    }
                    if moved.is_some() && state == "blocked" {
                        report.runs_blocked += 1;
                    }
                }
                Decision::Pause { reason } => {
                    // A durable run used its cycle's task starts. The pause
                    // itself (pause_for_budget) is applied by the scheduler in
                    // Python; here the decision is recorded and the run is left
                    // to wait. This is not a settlement.
                    tracing::debug!(reason = %reason, "run pausing for budget");
                }
                Decision::Continue => {}
            }
        }
        Ok(report)
    }
}

/// Errors a pass can raise (a database error, for the caller to log and retry).
#[derive(Debug, thiserror::Error)]
pub enum WorkerError {
    #[error(transparent)]
    Db(#[from] sqlx::Error),
    #[error(transparent)]
    Lifecycle(#[from] nexus_store::lifecycle::LifecycleError),
}
