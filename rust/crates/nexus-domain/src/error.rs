//! The error model. One enum, `thiserror`, no panics in library code.
//!
//! Every variant carries enough context to be reported to an operator without
//! a stack trace, because the callers are HTTP handlers and a CLI that must
//! answer with a status code, not a backtrace.

use thiserror::Error;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum DomainError {
    #[error("plan must contain between 1 and {max} tasks (got {got})")]
    PlanTaskCount { got: usize, max: usize },

    #[error("task {index} is not an object")]
    TaskNotObject { index: usize },

    #[error("task {index} has an invalid id")]
    TaskInvalidId { index: usize },

    #[error("duplicate task id {0}")]
    TaskDuplicateId(String),

    #[error("task {0} has no title")]
    TaskNoTitle(String),

    #[error("task {0} acceptance must be a list of strings")]
    TaskBadAcceptance(String),

    #[error("task {0} dependencies must be a list of ids")]
    TaskBadDependencies(String),

    #[error("task {0} has a duplicate or self dependency")]
    TaskBadDependencyList(String),

    #[error("task {0} depends on unknown task {1}")]
    TaskUnknownDependency(String, String),

    #[error("plan contains a dependency cycle")]
    PlanCycle,

    #[error("a {from} run cannot become {to}")]
    RunTransition { from: String, to: String },

    #[error("a {from} conversation cannot become {to}")]
    WorkflowTransition { from: String, to: String },

    #[error("invalid run state {0}")]
    InvalidRunState(String),
}
