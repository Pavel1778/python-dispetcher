//! `nexus-rs` - the migrated CLI surface.
//!
//! The Python CLI (`orchestration/cli.py`) is an HTTP client for the NEXUS
//! server. The parts of it that are **pure decisions** - validating a plan,
//! hashing it, checking a transition, computing a wait - do not need the
//! server and are migrated here. This binary is therefore both:
//!
//! * a usable tool (`nexus-rs validate-plan`, `nexus-rs plan-hash`,
//!   `nexus-rs transition`, `nexus-rs cycle-wait`), and
//! * the **oracle** the parity test drives: `nexus-rs parity` reads a JSON
//!   document of cases on stdin and writes the same decisions as JSON on
//!   stdout, so `tests/test_rust_parity.py` can compare it with the Python
//!   implementation case by case.
//!
//! Nothing here talks to the network or the database. That keeps the parity
//! claim exact: the two implementations are compared on the decision, not on
//! anything either one happens to do afterwards.

use clap::{Parser, Subcommand};
use nexus_core::scheduler::{claimable, RunSnapshot, TaskSnapshot};
use nexus_domain::dag::{plan_hash_of, validate_tasks};
use nexus_domain::retry::{cycle_wait, refused_wait, RetryPolicy};
use nexus_domain::state::{
    can_transition_run, can_transition_task, can_transition_workflow, RunState, TaskState,
    WorkflowState,
};
use serde::{Deserialize, Serialize};
use std::io::Read;
use std::process::ExitCode;

#[derive(Parser)]
#[command(name = "nexus-rs", version, about = "NEXUS domain decisions, in Rust")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Validate a plan (JSON) and print it normalised. Exit 1 if invalid.
    ValidatePlan {
        /// Path to a JSON file, or `-` for stdin.
        path: String,
    },
    /// Print the plan version hash (the approval binding) of a plan (JSON).
    PlanHash {
        path: String,
    },
    /// Whether a run transition is legal. Prints `true`/`false`.
    Transition {
        /// running|paused|waiting_for_approval|blocked|completed|failed|cancelled
        from: String,
        to: String,
    },
    /// The wait before a work cycle begins, in seconds.
    CycleWait {
        cycle: i64,
        #[arg(long, default_value = "")]
        seed: String,
    },
    /// The wait after a refused start (429), in seconds.
    RefusedWait {
        #[arg(long)]
        retry_after: Option<f64>,
        #[arg(long, default_value_t = false)]
        daily: bool,
        #[arg(long, default_value_t = 0)]
        refusals: i64,
    },
    /// Whether a task of a run may be claimed, from a JSON snapshot on stdin:
    /// `{"run": {...}, "tasks": [...], "task_id": "..."}`. Prints the verdict.
    Claimable,
    /// The parity oracle: read cases on stdin, write decisions on stdout.
    Parity,
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    match cli.command {
        Command::ValidatePlan { path } => cmd_validate_plan(&path),
        Command::PlanHash { path } => cmd_plan_hash(&path),
        Command::Transition { from, to } => cmd_transition(&from, &to),
        Command::CycleWait { cycle, seed } => {
            let policy = RetryPolicy::from_env();
            println!("{}", policy.cycle_wait(cycle, &seed));
            ExitCode::SUCCESS
        }
        Command::RefusedWait {
            retry_after,
            daily,
            refusals,
        } => {
            println!("{}", refused_wait(retry_after, daily, refusals));
            ExitCode::SUCCESS
        }
        Command::Claimable => cmd_claimable(),
        Command::Parity => cmd_parity(),
    }
}

fn read_input(path: &str) -> Result<String, String> {
    if path == "-" {
        let mut s = String::new();
        std::io::stdin()
            .read_to_string(&mut s)
            .map_err(|e| e.to_string())?;
        Ok(s)
    } else {
        std::fs::read_to_string(path).map_err(|e| e.to_string())
    }
}

fn cmd_validate_plan(path: &str) -> ExitCode {
    let raw = match read_input(path) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("{e}");
            return ExitCode::FAILURE;
        }
    };
    let value: serde_json::Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("invalid JSON: {e}");
            return ExitCode::FAILURE;
        }
    };
    match validate_tasks(&value) {
        Ok(tasks) => {
            println!(
                "{}",
                serde_json::to_string(&tasks).unwrap_or_else(|_| "[]".into())
            );
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("{e}");
            ExitCode::FAILURE
        }
    }
}

fn cmd_plan_hash(path: &str) -> ExitCode {
    let raw = match read_input(path) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("{e}");
            return ExitCode::FAILURE;
        }
    };
    let value: serde_json::Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("invalid JSON: {e}");
            return ExitCode::FAILURE;
        }
    };
    let tasks = match validate_tasks(&value) {
        Ok(t) => t,
        Err(e) => {
            eprintln!("{e}");
            return ExitCode::FAILURE;
        }
    };
    println!("{}", plan_hash_of(&tasks));
    ExitCode::SUCCESS
}

fn cmd_transition(from: &str, to: &str) -> ExitCode {
    let (Some(f), Some(t)) = (RunState::parse(from), RunState::parse(to)) else {
        println!("false");
        return ExitCode::SUCCESS;
    };
    println!("{}", can_transition_run(f, t));
    ExitCode::SUCCESS
}

/// The parity oracle's case shapes. Each case names what it wants decided; the
/// Python test builds these from the real implementation's own inputs.
#[derive(Debug, Deserialize)]
struct ClaimableInput {
    run: RunSnapshot,
    tasks: Vec<TaskSnapshot>,
    task_id: String,
}

fn cmd_claimable() -> ExitCode {
    let mut raw = String::new();
    if std::io::stdin().read_to_string(&mut raw).is_err() {
        eprintln!("could not read stdin");
        return ExitCode::FAILURE;
    }
    let input: ClaimableInput = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("invalid claimable input: {e}");
            return ExitCode::FAILURE;
        }
    };
    let verdict = claimable(&input.run, &input.tasks, &input.task_id);
    println!("{}", serde_json::to_string(&verdict).expect("serialises"));
    ExitCode::SUCCESS
}

#[derive(Debug, Deserialize)]
struct ParityInput {
    #[serde(default)]
    run_transitions: Vec<(String, String)>,
    #[serde(default)]
    workflow_transitions: Vec<(Option<String>, String)>,
    #[serde(default)]
    task_transitions: Vec<(String, String)>,
    #[serde(default)]
    plans: Vec<serde_json::Value>,
    #[serde(default)]
    plan_hashes: Vec<serde_json::Value>,
    #[serde(default)]
    cycle_waits: Vec<CycleWaitCase>,
    #[serde(default)]
    refused_waits: Vec<RefusedWaitCase>,
    #[serde(default)]
    claim_cases: Vec<ClaimCase>,
}

#[derive(Debug, Deserialize)]
struct ClaimCase {
    run: RunSnapshot,
    tasks: Vec<TaskSnapshot>,
    task_id: String,
}

#[derive(Debug, Deserialize)]
struct CycleWaitCase {
    cycle: i64,
    #[serde(default)]
    seed: String,
    base: u64,
    ceiling: u64,
}

#[derive(Debug, Deserialize)]
struct RefusedWaitCase {
    retry_after: Option<f64>,
    daily: bool,
    refusals: i64,
}

#[derive(Debug, Serialize)]
struct ParityOutput {
    run_transitions: Vec<bool>,
    workflow_transitions: Vec<bool>,
    task_transitions: Vec<bool>,
    /// `{"ok": true, "tasks": [...]}` or `{"ok": false, "error": "..."}`.
    plans: Vec<serde_json::Value>,
    plan_hashes: Vec<Option<String>>,
    cycle_waits: Vec<f64>,
    refused_waits: Vec<f64>,
    /// One verdict per claim case: `{"claimable": bool, "reason": str|null}`.
    claim_cases: Vec<serde_json::Value>,
}

fn cmd_parity() -> ExitCode {
    let mut raw = String::new();
    if std::io::stdin().read_to_string(&mut raw).is_err() {
        eprintln!("could not read stdin");
        return ExitCode::FAILURE;
    }
    let input: ParityInput = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("invalid parity input: {e}");
            return ExitCode::FAILURE;
        }
    };

    let run_transitions = input
        .run_transitions
        .iter()
        .map(|(f, t)| match (RunState::parse(f), RunState::parse(t)) {
            (Some(f), Some(t)) => can_transition_run(f, t),
            _ => false,
        })
        .collect();

    let workflow_transitions = input
        .workflow_transitions
        .iter()
        .map(|(f, t)| {
            let from = f.as_deref().and_then(WorkflowState::parse);
            match WorkflowState::parse(t) {
                Some(t) => can_transition_workflow(from, t),
                None => false,
            }
        })
        .collect();

    let task_transitions = input
        .task_transitions
        .iter()
        .map(|(f, t)| match (TaskState::parse(f), TaskState::parse(t)) {
            (Some(f), Some(t)) => can_transition_task(f, t),
            _ => false,
        })
        .collect();

    let plans = input
        .plans
        .iter()
        .map(|p| match validate_tasks(p) {
            Ok(tasks) => serde_json::json!({"ok": true, "tasks": tasks}),
            Err(e) => serde_json::json!({"ok": false, "error": e.to_string()}),
        })
        .collect();

    let plan_hashes = input
        .plan_hashes
        .iter()
        .map(|p| validate_tasks(p).ok().map(|t| plan_hash_of(&t)))
        .collect();

    let cycle_waits = input
        .cycle_waits
        .iter()
        .map(|c| cycle_wait(c.cycle, &c.seed, c.base, c.ceiling))
        .collect();

    let refused_waits = input
        .refused_waits
        .iter()
        .map(|c| refused_wait(c.retry_after, c.daily, c.refusals))
        .collect();

    let claim_cases = input
        .claim_cases
        .iter()
        .map(|c| {
            let v = claimable(&c.run, &c.tasks, &c.task_id);
            serde_json::json!({"claimable": v.claimable, "reason": v.reason})
        })
        .collect();

    let out = ParityOutput {
        run_transitions,
        workflow_transitions,
        task_transitions,
        plans,
        plan_hashes,
        cycle_waits,
        refused_waits,
        claim_cases,
    };
    println!("{}", serde_json::to_string(&out).expect("serialises"));
    ExitCode::SUCCESS
}
