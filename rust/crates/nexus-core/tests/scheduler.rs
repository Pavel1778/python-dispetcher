//! Rust-side tests for the scheduler's claim decision.
//!
//! The rules mirrored here are `LabStore.claim_task`'s: a task is claimable
//! when the run accepts work, the task has attempts left, it is due, every
//! dependency is verified, and its branch is free. The cross-language proof is
//! `tests/test_rust_parity.py`.

use nexus_core::scheduler::{claimable, run_complete, RunSnapshot, TaskSnapshot};

fn task(id: &str, state: &str) -> TaskSnapshot {
    TaskSnapshot {
        id: id.to_string(),
        task_key: id.to_string(),
        state: state.to_string(),
        dependencies: vec![],
        repository: "owner/repo".to_string(),
        branch: "main".to_string(),
        attempt: 0,
        max_attempts: 3,
        lease_live: false,
        retry_due: true,
        abandoned: false,
    }
}

fn run() -> RunSnapshot {
    RunSnapshot {
        state: "running".to_string(),
        policy: "durable".to_string(),
        iteration: 0,
        max_iterations: 50,
        pausing: false,
        within_deadline: true,
    }
}

#[test]
fn a_plain_pending_task_is_claimable() {
    let tasks = vec![task("t1", "pending")];
    assert!(claimable(&run(), &tasks, "t1").claimable);
}

#[test]
fn a_task_with_no_attempts_left_is_not_claimable() {
    let mut t = task("t1", "pending");
    t.attempt = 3;
    let tasks = vec![t];
    let v = claimable(&run(), &tasks, "t1");
    assert!(!v.claimable);
    assert!(v.reason.unwrap().contains("attempt"));
}

#[test]
fn a_pending_task_with_a_live_lease_is_not_due() {
    let mut t = task("t1", "pending");
    t.lease_live = true;
    let tasks = vec![t];
    assert!(!claimable(&run(), &tasks, "t1").claimable);
}

#[test]
fn an_unverified_dependency_blocks_the_child() {
    let mut child = task("t2", "pending");
    child.dependencies = vec!["t1".to_string()];
    let parent = task("t1", "running");
    let tasks = vec![parent, child];
    let v = claimable(&run(), &tasks, "t2");
    assert!(!v.claimable);
    assert!(v.reason.unwrap().contains("dependency"));
}

#[test]
fn a_verified_dependency_unblocks_the_child() {
    let mut child = task("t2", "pending");
    child.dependencies = vec!["t1".to_string()];
    let parent = task("t1", "completed_verified");
    let tasks = vec![parent, child];
    assert!(claimable(&run(), &tasks, "t2").claimable);
}

#[test]
fn completed_no_changes_also_unblocks_the_child() {
    let mut child = task("t2", "pending");
    child.dependencies = vec!["t1".to_string()];
    let parent = task("t1", "completed_no_changes");
    let tasks = vec![parent, child];
    assert!(claimable(&run(), &tasks, "t2").claimable);
}

#[test]
fn a_busy_branch_blocks_a_second_task() {
    let a = task("t1", "running");
    let b = task("t2", "pending");
    let tasks = vec![a, b];
    let v = claimable(&run(), &tasks, "t2");
    assert!(!v.claimable);
    assert!(v.reason.unwrap().contains("branch"));
}

#[test]
fn a_different_branch_does_not_contend() {
    let mut a = task("t1", "running");
    a.branch = "feature".to_string();
    let b = task("t2", "pending");
    let tasks = vec![a, b];
    assert!(claimable(&run(), &tasks, "t2").claimable);
}

#[test]
fn an_abandoned_conversation_holds_the_branch() {
    let mut a = task("t1", "pending");
    a.abandoned = true;
    let b = task("t2", "pending");
    let tasks = vec![a, b];
    assert!(!claimable(&run(), &tasks, "t2").claimable);
}

#[test]
fn a_repository_less_task_never_contends() {
    let mut a = task("t1", "running");
    a.repository = String::new();
    let mut b = task("t2", "pending");
    b.repository = String::new();
    let tasks = vec![a, b];
    assert!(claimable(&run(), &tasks, "t2").claimable);
}

#[test]
fn a_paused_run_claims_nothing() {
    let mut r = run();
    r.state = "paused".to_string();
    let tasks = vec![task("t1", "pending")];
    assert!(!claimable(&r, &tasks, "t1").claimable);
}

#[test]
fn a_bounded_run_past_its_deadline_claims_nothing() {
    let mut r = run();
    r.policy = "bounded".to_string();
    r.within_deadline = false;
    let tasks = vec![task("t1", "pending")];
    assert!(!claimable(&r, &tasks, "t1").claimable);
}

#[test]
fn a_durable_run_ignores_the_deadline() {
    let mut r = run();
    r.policy = "durable".to_string();
    r.within_deadline = false;
    let tasks = vec![task("t1", "pending")];
    assert!(claimable(&r, &tasks, "t1").claimable);
}

#[test]
fn a_run_out_of_iterations_claims_nothing() {
    let mut r = run();
    r.iteration = 50;
    let tasks = vec![task("t1", "pending")];
    assert!(!claimable(&r, &tasks, "t1").claimable);
}

#[test]
fn run_complete_requires_every_task_verified() {
    assert!(run_complete(&[
        task("t1", "completed_verified"),
        task("t2", "completed_no_changes"),
    ]));
    assert!(!run_complete(&[
        task("t1", "completed_verified"),
        task("t2", "completed_unverified"),
    ]));
    assert!(!run_complete(&[]));
}
