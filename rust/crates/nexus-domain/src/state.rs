//! The state machines, as pure data and pure functions.
//!
//! Three machines live in NEXUS, and each is authoritative in Python today:
//!
//! * the **run** machine (`orchestration/nexus_lab_store.py`,
//!   `_RUN_ENTERED_FROM`) - an autonomous run;
//! * the **chat workflow** machine (`orchestration/nexus_workflow.py`,
//!   `_ALLOWED`) - a conversation that discusses, plans and then runs;
//! * the **control-plane task** machine (`orchestration/control_plane.py`,
//!   `_ALLOWED`) - the original dispatcher's unit of work.
//!
//! This module reproduces them exactly. "Exact" is the requirement: the Python
//! tables are read from the source, not from a summary, and a parity test
//! (`tests/test_rust_parity.py`) drives both implementations over the same
//! inputs and compares the answers. A transition that is legal here and not in
//! Python - or the other way round - is a defect this module exists to catch.
//!
//! Two conventions, both copied from Python:
//!
//! * asking for the state a thing is already in is a **no-op**, not an error
//!   (the caller sees `Ok` and the same state);
//! * an **unknown** current state is treated as the machine's start state
//!   (`run` -> `running` is *not* the convention; see each `can_*` below), and
//!   an unknown *target* is always refused.

use crate::error::DomainError;
use serde::{Deserialize, Serialize};

/// Run states. `ACTIVE_RUN_STATES` = running, paused, waiting_for_approval,
/// blocked; `TERMINAL_RUN_STATES` = completed, failed, cancelled.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RunState {
    Running,
    Paused,
    WaitingForApproval,
    Blocked,
    Completed,
    Failed,
    Cancelled,
}

impl RunState {
    pub fn as_str(&self) -> &'static str {
        match self {
            RunState::Running => "running",
            RunState::Paused => "paused",
            RunState::WaitingForApproval => "waiting_for_approval",
            RunState::Blocked => "blocked",
            RunState::Completed => "completed",
            RunState::Failed => "failed",
            RunState::Cancelled => "cancelled",
        }
    }

    pub fn parse(s: &str) -> Option<RunState> {
        Some(match s {
            "running" => RunState::Running,
            "paused" => RunState::Paused,
            "waiting_for_approval" => RunState::WaitingForApproval,
            "blocked" => RunState::Blocked,
            "completed" => RunState::Completed,
            "failed" => RunState::Failed,
            "cancelled" => RunState::Cancelled,
            _ => return None,
        })
    }

    /// Whether the loop should keep servicing a run in this state.
    pub fn is_active(&self) -> bool {
        matches!(
            self,
            RunState::Running | RunState::Paused | RunState::WaitingForApproval | RunState::Blocked
        )
    }

    /// Whether the run has no way forward.
    pub fn is_terminal(&self) -> bool {
        matches!(
            self,
            RunState::Completed | RunState::Failed | RunState::Cancelled
        )
    }

    /// The states a run in this state may be moved *to*.
    ///
    /// Derived from `_RUN_ENTERED_FROM` (the reverse index Python stores): a
    /// target `t` is allowed from `self` iff `self` appears in
    /// `_RUN_ENTERED_FROM[t]`. `self == t` is always allowed (a no-op).
    pub fn allowed_targets(&self) -> Vec<RunState> {
        use RunState::*;
        let entered_from: &[(RunState, &[RunState])] = &[
            (Running, &[Paused, Blocked]),
            (Paused, &[Running]),
            (WaitingForApproval, &[Running]),
            (Blocked, &[Running]),
            (Completed, &[Running]),
            (Failed, &[Running]),
            (Cancelled, &[Running, Paused, WaitingForApproval, Blocked]),
        ];
        entered_from
            .iter()
            .filter(|(target, sources)| {
                // A no-op is always allowed; otherwise `self` must be a source.
                *target == *self || sources.contains(self)
            })
            .map(|(target, _)| *target)
            .collect()
    }
}

/// Whether a run may move from `current` to `target`.
///
/// A no-op (`current == target`) is allowed. An unknown target is refused.
pub fn can_transition_run(current: RunState, target: RunState) -> bool {
    current == target || current.allowed_targets().contains(&target)
}

/// `target` if the move is legal, else the error the Python raises.
pub fn check_transition_run(current: RunState, target: RunState) -> Result<RunState, DomainError> {
    if can_transition_run(current, target) {
        Ok(target)
    } else {
        Err(DomainError::RunTransition {
            from: current.as_str().to_string(),
            to: target.as_str().to_string(),
        })
    }
}

/// Chat-workflow states (`nexus_workflow.STATES`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum WorkflowState {
    Discovery,
    Clarification,
    Planning,
    AwaitingApproval,
    Executing,
    Reporting,
    Completed,
    Cancelled,
    Failed,
}

impl WorkflowState {
    pub fn as_str(&self) -> &'static str {
        match self {
            WorkflowState::Discovery => "DISCOVERY",
            WorkflowState::Clarification => "CLARIFICATION",
            WorkflowState::Planning => "PLANNING",
            WorkflowState::AwaitingApproval => "AWAITING_APPROVAL",
            WorkflowState::Executing => "EXECUTING",
            WorkflowState::Reporting => "REPORTING",
            WorkflowState::Completed => "COMPLETED",
            WorkflowState::Cancelled => "CANCELLED",
            WorkflowState::Failed => "FAILED",
        }
    }

    pub fn parse(s: &str) -> Option<WorkflowState> {
        Some(match s {
            "DISCOVERY" => WorkflowState::Discovery,
            "CLARIFICATION" => WorkflowState::Clarification,
            "PLANNING" => WorkflowState::Planning,
            "AWAITING_APPROVAL" => WorkflowState::AwaitingApproval,
            "EXECUTING" => WorkflowState::Executing,
            "REPORTING" => WorkflowState::Reporting,
            "COMPLETED" => WorkflowState::Completed,
            "CANCELLED" => WorkflowState::Cancelled,
            "FAILED" => WorkflowState::Failed,
            _ => return None,
        })
    }

    pub fn is_terminal(&self) -> bool {
        matches!(
            self,
            WorkflowState::Completed | WorkflowState::Cancelled | WorkflowState::Failed
        )
    }

    /// The states this one may move to (`nexus_workflow._ALLOWED`).
    pub fn allowed_targets(&self) -> Vec<WorkflowState> {
        use WorkflowState::*;
        let targets: &[WorkflowState] = match self {
            Discovery => &[
                Discovery,
                Clarification,
                Planning,
                AwaitingApproval,
                Cancelled,
                Failed,
            ],
            Clarification => &[
                Clarification,
                Discovery,
                Planning,
                AwaitingApproval,
                Cancelled,
                Failed,
            ],
            Planning => &[
                Planning,
                Clarification,
                Discovery,
                AwaitingApproval,
                Cancelled,
                Failed,
            ],
            AwaitingApproval => &[
                AwaitingApproval,
                Planning,
                Clarification,
                Discovery,
                Executing,
                Cancelled,
                Failed,
            ],
            Executing => &[
                Executing,
                Reporting,
                Completed,
                Cancelled,
                Failed,
                AwaitingApproval,
            ],
            Reporting => &[Reporting, Completed, Cancelled, Failed, Executing],
            Completed => &[Completed, Discovery, Planning, AwaitingApproval, Executing],
            Cancelled => &[Cancelled, Discovery, Planning, AwaitingApproval, Executing],
            Failed => &[Failed, Discovery, Planning, AwaitingApproval, Executing],
        };
        targets.to_vec()
    }
}

/// Whether a conversation may move from `current` to `target`.
///
/// An unknown *current* falls back to `DISCOVERY`, exactly as Python's
/// `can_transition` does; an unknown target is refused.
pub fn can_transition_workflow(current: Option<WorkflowState>, target: WorkflowState) -> bool {
    let current = current.unwrap_or(WorkflowState::Discovery);
    current == target || current.allowed_targets().contains(&target)
}

/// `target` if the move is legal, else the error Python's `check_transition`
/// raises.
pub fn check_transition_workflow(
    current: Option<WorkflowState>,
    target: WorkflowState,
) -> Result<WorkflowState, DomainError> {
    if can_transition_workflow(current, target) {
        Ok(target)
    } else {
        Err(DomainError::WorkflowTransition {
            from: current
                .unwrap_or(WorkflowState::Discovery)
                .as_str()
                .to_string(),
            to: target.as_str().to_string(),
        })
    }
}

/// The original control-plane task machine (`control_plane.TaskState`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskState {
    Queued,
    Assigned,
    Running,
    Verifying,
    Completed,
    Blocked,
    Failed,
    Cancelled,
    Recovering,
    DeadLetter,
}

impl TaskState {
    pub fn as_str(&self) -> &'static str {
        match self {
            TaskState::Queued => "queued",
            TaskState::Assigned => "assigned",
            TaskState::Running => "running",
            TaskState::Verifying => "verifying",
            TaskState::Completed => "completed",
            TaskState::Blocked => "blocked",
            TaskState::Failed => "failed",
            TaskState::Cancelled => "cancelled",
            TaskState::Recovering => "recovering",
            TaskState::DeadLetter => "dead_letter",
        }
    }

    pub fn parse(s: &str) -> Option<TaskState> {
        Some(match s {
            "queued" => TaskState::Queued,
            "assigned" => TaskState::Assigned,
            "running" => TaskState::Running,
            "verifying" => TaskState::Verifying,
            "completed" => TaskState::Completed,
            "blocked" => TaskState::Blocked,
            "failed" => TaskState::Failed,
            "cancelled" => TaskState::Cancelled,
            "recovering" => TaskState::Recovering,
            "dead_letter" => TaskState::DeadLetter,
            _ => return None,
        })
    }

    /// The states this one may move to (`control_plane._ALLOWED`).
    pub fn allowed_targets(&self) -> Vec<TaskState> {
        use TaskState::*;
        let targets: &[TaskState] = match self {
            Queued => &[Assigned, Cancelled, Blocked],
            Assigned => &[
                Running, Queued, Failed, Cancelled, Recovering, Blocked, Verifying,
            ],
            Running => &[
                Verifying, Failed, Blocked, Cancelled, Recovering, DeadLetter,
            ],
            Verifying => &[Completed, Failed, Running, Blocked, DeadLetter],
            // "From here reality decides, so every safe outcome is reachable,
            // including completion: a worker can die while its conversation
            // finished successfully."
            Recovering => &[
                Running, Queued, Failed, Cancelled, Blocked, Verifying, Completed, DeadLetter,
            ],
            Completed => &[],
            Blocked => &[Queued, Cancelled],
            Failed => &[Queued, DeadLetter, Cancelled],
            Cancelled => &[],
            DeadLetter => &[Queued],
        };
        targets.to_vec()
    }
}

/// Whether a control-plane task may move from `current` to `target`.
///
/// **Strict**, unlike the run and workflow machines: `control_plane.transition`
/// tests `to not in _ALLOWED[frm]` with no no-op exception, so a task may not be
/// "moved" to the state it is already in. The parity test compares this
/// strictly.
pub fn can_transition_task(current: TaskState, target: TaskState) -> bool {
    current.allowed_targets().contains(&target)
}
