//! `nexus-core` - the orchestration decisions, in Rust, over `nexus-domain`.
//!
//! Where `nexus-domain` is pure data and pure arithmetic, this crate is the
//! **scheduler**: given a run, its tasks and a moment in time, which task may
//! be claimed next, and is the run finished? That question is exactly what
//! `LabStore.claim_task` and `LabScheduler.tick` answer in Python today, and it
//! is the part the requirement names "Priority A - Core orchestration".
//!
//! The scheduler here is deliberately **stateless and pure**: it reads a
//! snapshot and returns a decision. Persisting the decision (the SQL that
//! claims a row, with `FOR UPDATE SKIP LOCKED`) stays in Python until the whole
//! claim path is migrated; keeping the *decision* pure is what lets it be
//! compared against Python over the same snapshots before it is authoritative.
//!
//! The dependency rule this crate encodes is the one `nexus_lab_store` states:
//! *a task is claimable only when every dependency is `completed_verified`*
//! (or `completed_no_changes` - a task whose kind legitimately changed
//! nothing).

pub mod scheduler;

pub use scheduler::{claimable, DependencyVerdict, RunSnapshot, TaskSnapshot};
