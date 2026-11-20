//! The claim decision: which task of a run may be taken next, and is the run
//! finished?
//!
//! This mirrors the conditions `LabStore.claim_task` evaluates in SQL
//! (`_TASK_DUE`, `_DEPS_VERIFIED`, `_BRANCH_FREE`) and the run-level gate on
//! the locked run row. This is only a **run-local prediction**, not authority
//! to start work. The Python store and the SQLx candidate in `nexus-storage`
//! arbitrate across runs using committed PostgreSQL state.
//!
//! The snapshot types are plain data, so a test (Python or Rust) can build one
//! without a database.

use nexus_domain::state::TaskState;
use serde::{Deserialize, Serialize};

/// Task states that count as "done well enough to unblock a child".
/// `nexus_lab_store.VERIFIED_STATES`.
pub const VERIFIED_STATES: &[&str] = &["completed_verified", "completed_no_changes"];

/// Task states with a conversation that may be at work (`IN_FLIGHT_TASK_STATES`).
pub const IN_FLIGHT_STATES: &[&str] = &["dispatched", "running", "verifying"];

/// One task, as the claim decision needs it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TaskSnapshot {
    pub id: String,
    pub task_key: String,
    /// `pending` | `waiting` | `dispatched` | `running` | `verifying` |
    /// `completed_verified` | `completed_no_changes` | `completed_unverified` |
    /// `partial` | `failed` | `blocked`.
    pub state: String,
    pub dependencies: Vec<String>,
    pub repository: String,
    pub branch: String,
    pub attempt: i64,
    pub max_attempts: i64,
    /// Whether a live lease is held (a `pending` task with a lease is not due).
    pub lease_live: bool,
    /// For a `waiting` task, whether `retry_at` has come.
    pub retry_due: bool,
    /// Whether an abandoned conversation is recorded for this task
    /// (`jsonb_array_length(abandoned) > 0`).
    pub abandoned: bool,
}

/// One run, as the claim decision needs it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RunSnapshot {
    /// `running` | `paused` | `waiting_for_approval` | `blocked` | terminal.
    pub state: String,
    /// `durable` or `bounded`.
    pub policy: String,
    pub iteration: i64,
    pub max_iterations: i64,
    /// Whether the run is pausing (`resume_at` in the future).
    pub pausing: bool,
    /// For a `bounded` run, whether `now() < deadline_at`.
    pub within_deadline: bool,
}

/// A verdict about one candidate task: why it can or cannot be claimed.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DependencyVerdict {
    pub task_id: String,
    pub claimable: bool,
    /// The first reason it is not claimable, in the order the SQL checks them.
    pub reason: Option<String>,
}

impl TaskSnapshot {
    fn is_in_flight(&self) -> bool {
        IN_FLIGHT_STATES.contains(&self.state.as_str())
    }

    /// `_TASK_DUE`: `pending` with no live lease, or `waiting` and due.
    fn is_due(&self) -> bool {
        match self.state.as_str() {
            "pending" => !self.lease_live,
            "waiting" => self.retry_due,
            _ => false,
        }
    }
}

/// Whether the run itself accepts a claim right now (`claim_task`'s run gate).
pub fn run_accepts_claim(run: &RunSnapshot) -> bool {
    run.state == "running"
        && run.iteration < run.max_iterations
        && !run.pausing
        && (run.policy == "durable" || run.within_deadline)
}

/// Whether a task may be claimed, with the reason it may not.
///
/// The order of the checks follows the SQL: run gate, attempts, due,
/// dependencies, branch. `tasks` is the whole run's task list, needed for the
/// dependency and branch checks.
pub fn claimable(run: &RunSnapshot, tasks: &[TaskSnapshot], task_id: &str) -> DependencyVerdict {
    let no = |reason: &str| DependencyVerdict {
        task_id: task_id.to_string(),
        claimable: false,
        reason: Some(reason.to_string()),
    };
    let yes = || DependencyVerdict {
        task_id: task_id.to_string(),
        claimable: true,
        reason: None,
    };

    let task = match tasks.iter().find(|t| t.id == task_id) {
        Some(t) => t,
        None => return no("no such task"),
    };

    if !run_accepts_claim(run) {
        return no("the run is not accepting work");
    }
    if task.attempt >= task.max_attempts {
        return no("no attempts left");
    }
    if !task.is_due() {
        return no("not due");
    }
    if !dependencies_verified(task, tasks) {
        return no("a dependency is not verified");
    }
    if !branch_free(task, tasks) {
        return no("another task is at work on this branch");
    }
    yes()
}

/// `_DEPS_VERIFIED`: every dependency of `task` is `completed_verified` or
/// `completed_no_changes`. A dependency that does not exist is not verified.
pub fn dependencies_verified(task: &TaskSnapshot, tasks: &[TaskSnapshot]) -> bool {
    task.dependencies.iter().all(|dep| {
        tasks
            .iter()
            .find(|t| t.task_key == *dep)
            .map(|t| VERIFIED_STATES.contains(&t.state.as_str()))
            .unwrap_or(false)
    })
}

/// Run-local projection of `_BRANCH_FREE`. An abandoned conversation (even
/// this task's own) holds the branch. This snapshot cannot see other runs or
/// requested stops; only the storage transaction can authorize a claim.
pub fn branch_free(task: &TaskSnapshot, tasks: &[TaskSnapshot]) -> bool {
    if task.repository.is_empty() {
        return true;
    }
    for other in tasks {
        if other.repository != task.repository || other.branch != task.branch {
            continue;
        }
        if (other.id != task.id && other.is_in_flight()) || other.abandoned {
            return false;
        }
    }
    true
}

/// Whether a run is finished: every task is in a verified state.
///
/// This is the condition `LabScheduler` uses to decide `completed`. A run with
/// an unverified dependency is *not* finished - it is blocked, and the caller
/// distinguishes that from "still working" by whether anything is claimable.
pub fn run_complete(tasks: &[TaskSnapshot]) -> bool {
    !tasks.is_empty()
        && tasks
            .iter()
            .all(|t| VERIFIED_STATES.contains(&t.state.as_str()))
}

/// Whether any task of the run is claimable right now.
pub fn has_claimable(run: &RunSnapshot, tasks: &[TaskSnapshot]) -> bool {
    tasks
        .iter()
        .any(|t| claimable(run, tasks, &t.id).claimable)
}

/// A convenience: the state of a task as the enum, when it is a known state.
pub fn task_state(task: &TaskSnapshot) -> Option<TaskState> {
    TaskState::parse(&task.state)
}
