//! Rust-side tests for the migrated domain logic.
//!
//! These prove the Rust implementation is internally correct and total (no
//! panics on any input shape). The *cross-language* claim - that Rust and
//! Python agree - is proven separately by `tests/test_rust_parity.py`, which
//! drives both implementations over the same cases. Both are needed: these
//! tests would still pass if the Rust table were subtly different from
//! Python's; the parity test would catch that, and these catch a Rust-internal
//! regression a Python-driven comparison might not build an input for.

use nexus_domain::dag::{plan_hash_of, validate_tasks};
use nexus_domain::retry::{cycle_wait, refused_wait, spread};
use nexus_domain::state::{
    can_transition_run, can_transition_task, can_transition_workflow, RunState, TaskState,
    WorkflowState,
};
use serde_json::json;

#[test]
fn run_no_op_is_allowed_and_unknown_target_is_refused() {
    for s in [
        RunState::Running,
        RunState::Paused,
        RunState::Blocked,
        RunState::Completed,
        RunState::Cancelled,
    ] {
        assert!(can_transition_run(s, s), "a no-op must be allowed for {s:?}");
    }
    assert!(can_transition_run(RunState::Running, RunState::Cancelled));
    assert!(can_transition_run(RunState::Blocked, RunState::Running));
    assert!(!can_transition_run(RunState::Completed, RunState::Running));
    assert!(!can_transition_run(RunState::Cancelled, RunState::Running));
    assert!(!can_transition_run(RunState::Completed, RunState::Paused));
}

#[test]
fn run_active_and_terminal_partition_the_states() {
    for s in [
        RunState::Running,
        RunState::Paused,
        RunState::WaitingForApproval,
        RunState::Blocked,
        RunState::Completed,
        RunState::Failed,
        RunState::Cancelled,
    ] {
        assert!(
            s.is_active() ^ s.is_terminal(),
            "{s:?} must be exactly one of active/terminal"
        );
    }
}

#[test]
fn executing_is_reachable_only_from_awaiting_approval() {
    for from in [
        WorkflowState::Discovery,
        WorkflowState::Clarification,
        WorkflowState::Planning,
    ] {
        assert!(
            !can_transition_workflow(Some(from), WorkflowState::Executing),
            "{from:?} must not reach EXECUTING"
        );
    }
    assert!(can_transition_workflow(
        Some(WorkflowState::AwaitingApproval),
        WorkflowState::Executing
    ));
    assert!(!can_transition_workflow(None, WorkflowState::Executing));
}

#[test]
fn control_plane_terminal_states_have_no_exit() {
    assert!(can_transition_task(TaskState::Queued, TaskState::Assigned));
    assert!(!can_transition_task(TaskState::Completed, TaskState::Queued));
    assert!(!can_transition_task(TaskState::Cancelled, TaskState::Queued));
    assert!(can_transition_task(
        TaskState::Recovering,
        TaskState::Completed
    ));
    assert!(can_transition_task(
        TaskState::Recovering,
        TaskState::DeadLetter
    ));
    // Strict: a no-op is refused (control_plane.transition has no exception).
    assert!(!can_transition_task(TaskState::Queued, TaskState::Queued));
}

#[test]
fn a_valid_plan_normalises_and_keeps_order() {
    let plan = json!({"tasks": [
        {"id": "t2", "title": "  second  ", "dependencies": ["t1"]},
        {"id": "t1", "title": "first", "acceptance": "one criterion"}
    ]});
    let tasks = validate_tasks(&plan).expect("valid");
    assert_eq!(tasks[0].id, "t2");
    assert_eq!(tasks[0].title, "second");
    assert_eq!(tasks[0].dependencies, vec!["t1"]);
    assert_eq!(tasks[1].acceptance, vec!["one criterion"]);
}

#[test]
fn a_plan_with_a_cycle_is_refused() {
    let plan = json!({"tasks": [
        {"id": "a", "title": "a", "dependencies": ["b"]},
        {"id": "b", "title": "b", "dependencies": ["a"]}
    ]});
    let err = validate_tasks(&plan).unwrap_err();
    assert!(err.to_string().contains("cycle"), "{err}");
}

#[test]
fn a_plan_with_an_unknown_dependency_is_refused() {
    let plan = json!({"tasks": [{"id": "a", "title": "a", "dependencies": ["ghost"]}]});
    let err = validate_tasks(&plan).unwrap_err();
    assert!(err.to_string().contains("unknown"), "{err}");
}

#[test]
fn a_self_dependency_is_refused() {
    let plan = json!({"tasks": [{"id": "a", "title": "a", "dependencies": ["a"]}]});
    assert!(validate_tasks(&plan).is_err());
}

#[test]
fn an_empty_plan_is_refused() {
    assert!(validate_tasks(&json!({"tasks": []})).is_err());
    assert!(validate_tasks(&json!({})).is_err());
}

#[test]
fn a_bad_task_id_is_refused() {
    assert!(validate_tasks(&json!({"tasks": [{"id": "has space", "title": "x"}]})).is_err());
    assert!(validate_tasks(&json!({"tasks": [{"id": "", "title": "x"}]})).is_err());
}

#[test]
fn the_plan_hash_is_stable_and_sensitive() {
    let ta = validate_tasks(&json!({"tasks": [{"id": "t1", "title": "one"}]})).unwrap();
    let tb = validate_tasks(&json!({"tasks": [{"id": "t1", "title": "one"}]})).unwrap();
    let tc = validate_tasks(&json!({"tasks": [{"id": "t1", "title": "two"}]})).unwrap();
    assert_eq!(plan_hash_of(&ta), plan_hash_of(&tb));
    assert_ne!(plan_hash_of(&ta), plan_hash_of(&tc));
    assert_eq!(plan_hash_of(&ta).len(), 64);
}

#[test]
fn the_spread_is_deterministic_and_in_range() {
    for seed in ["a", "b", "task-1:3", ""] {
        let x = spread(seed);
        let y = spread(seed);
        assert_eq!(x, y);
        assert!((0.0..0.1).contains(&x), "spread out of range: {x}");
    }
}

#[test]
fn cycle_wait_doubles_and_is_capped() {
    assert_eq!(cycle_wait(1, "", 60, 3600), 60.0);
    assert_eq!(cycle_wait(2, "", 60, 3600), 120.0);
    assert_eq!(cycle_wait(3, "", 60, 3600), 240.0);
    assert_eq!(cycle_wait(40, "", 60, 3600), 3600.0);
    let with_seed = cycle_wait(1, "task-1", 60, 3600);
    assert!(with_seed >= 60.0 && with_seed < 66.0, "{with_seed}");
}

#[test]
fn refused_wait_honours_retry_after_and_clamps() {
    assert_eq!(refused_wait(Some(120.0), false, 0), 120.0);
    assert_eq!(refused_wait(Some(1.0), false, 0), 30.0);
    assert_eq!(refused_wait(Some(99999.0), false, 0), 3600.0);
    assert_eq!(refused_wait(None, false, 0), 60.0);
    assert_eq!(refused_wait(None, true, 0), 900.0);
    assert_eq!(refused_wait(None, false, 20), 3600.0);
}
