//! `nexus-domain` - the pure, deterministic core of NEXUS, in Rust.
//!
//! Everything here is a **pure function or a pure data type**. There is no I/O,
//! no database, no clock, no environment read except in the one constructor
//! that exists for it ([`retry::RetryPolicy::from_env`]). That is deliberate:
//! a pure component can be run against the Python implementation over the same
//! inputs and compared byte for byte, which is how a migration earns the right
//! to become authoritative (`docs/RUST_MIGRATION.md`, "parity gates").
//!
//! The three migrated components, each mirroring a named Python module:
//!
//! | Module | Python source | What it decides |
//! |---|---|---|
//! | [`state`] | `nexus_lab_store`, `nexus_workflow`, `control_plane` | which state transitions are legal |
//! | [`dag`] | `nexus_planner`, `nexus_workflow.plan_hash_of` | whether a plan is well-formed, and its version hash |
//! | [`retry`] | `nexus_retry` | how long to wait before trying again |
//!
//! The parity test lives in the repository's Python suite
//! (`tests/test_rust_parity.py`), because that is where the Python side already
//! is; it drives both implementations and fails on any disagreement.

pub mod dag;
pub mod error;
pub mod retry;
pub mod state;

pub use error::DomainError;
