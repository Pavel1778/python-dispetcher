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
    PlanHash { path: String },
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
    /// Claim one task of a run against a real database (the Rust claim
    /// transaction). Reads the DSN from `NEXUS_DB_URL`, the run id and worker
    /// id from the arguments; prints the claimed task as JSON, or `null`.
    Claim {
        run_id: String,
        worker_id: String,
        #[arg(long, default_value_t = 900)]
        lease_seconds: i64,
    },
    /// In-process claim latency: claim `n` times on `n` freshly-built runs and
    /// print p50/p99/mean in milliseconds as JSON. One connection pool for the
    /// whole batch, so the number is the claim itself, not process start.
    ClaimBench {
        #[arg(long, default_value_t = 100)]
        n: usize,
    },
    /// Move a run to a state (the Rust `set_run_state`), against a real
    /// database. Prints the new run as JSON, or `null`. `--strict` makes an
    /// illegal transition an error (a user request); without it, an illegal
    /// transition prints `null` (the scheduler).
    RunState {
        project_id: String,
        run_id: String,
        state: String,
        #[arg(long)]
        last_error: Option<String>,
        #[arg(long, default_value_t = false)]
        strict: bool,
        #[arg(long)]
        actor: Option<String>,
    },
    /// Reclaim expired-lease tasks of a run (the Rust `reclaim_expired`).
    /// Prints the reclaimed tasks as a JSON array.
    Reclaim { run_id: String },
    /// Release a claimed task (the Rust `release_task`). Prints the task JSON.
    Release {
        task_id: String,
        #[arg(long, default_value = "")]
        error: String,
        #[arg(long, default_value_t = true)]
        requeue: bool,
        #[arg(long)]
        lease_token: Option<i64>,
    },
    /// Postpone a claimed task because a start was refused (`postpone_task`).
    Postpone {
        task_id: String,
        #[arg(long, default_value = "")]
        error: String,
        #[arg(long, default_value_t = 0.0)]
        seconds: f64,
        #[arg(long, default_value_t = false)]
        forget_tried: bool,
        #[arg(long)]
        lease_token: Option<i64>,
    },
    /// Print the run decision (`decide_run`, mirroring `_decide`) as JSON:
    /// {"decision":"continue"|"completed"|"settle"|"pause","state":..,"reason":..}
    Decide { run_id: String },
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
        Command::Claim {
            run_id,
            worker_id,
            lease_seconds,
        } => cmd_claim(&run_id, &worker_id, lease_seconds),
        Command::ClaimBench { n } => cmd_claim_bench(n),
        Command::RunState {
            project_id,
            run_id,
            state,
            last_error,
            strict,
            actor,
        } => cmd_run_state(
            &project_id,
            &run_id,
            &state,
            last_error.as_deref(),
            strict,
            actor.as_deref(),
        ),
        Command::Reclaim { run_id } => cmd_reclaim(&run_id),
        Command::Release {
            task_id,
            error,
            requeue,
            lease_token,
        } => cmd_release(&task_id, &error, requeue, lease_token),
        Command::Postpone {
            task_id,
            error,
            seconds,
            forget_tried,
            lease_token,
        } => cmd_postpone(&task_id, &error, seconds, forget_tried, lease_token),
        Command::Decide { run_id } => cmd_decide(&run_id),
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

fn cmd_claim(run_id: &str, worker_id: &str, lease_seconds: i64) -> ExitCode {
    use uuid::Uuid;
    let dsn = match std::env::var("NEXUS_DB_URL").map(|d| nexus_store::to_sqlx_url(&d)) {
        Ok(v) if !v.trim().is_empty() => v,
        _ => {
            eprintln!("set NEXUS_DB_URL");
            return ExitCode::from(2);
        }
    };
    let run = match Uuid::parse_str(run_id) {
        Ok(u) => u,
        Err(e) => {
            eprintln!("bad run id: {e}");
            return ExitCode::from(2);
        }
    };
    let rt = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(r) => r,
        Err(e) => {
            eprintln!("runtime: {e}");
            return ExitCode::FAILURE;
        }
    };
    let result: Result<Option<nexus_store::ClaimedTask>, sqlx::Error> = rt.block_on(async {
        let store = nexus_store::Store::connect(&dsn, 4).await?;
        store.claim_task(run, worker_id, lease_seconds).await
    });
    match result {
        Ok(Some(task)) => {
            println!("{}", serde_json::to_string(&task).unwrap());
            ExitCode::SUCCESS
        }
        Ok(None) => {
            println!("null");
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("claim failed: {e}");
            ExitCode::FAILURE
        }
    }
}

fn cmd_claim_bench(n: usize) -> ExitCode {
    use std::time::Instant;
    let dsn = match std::env::var("NEXUS_DB_URL").map(|d| nexus_store::to_sqlx_url(&d)) {
        Ok(v) if !v.trim().is_empty() => v,
        _ => {
            eprintln!("set NEXUS_DB_URL");
            return ExitCode::from(2);
        }
    };
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("runtime");
    let out = rt.block_on(async move {
        let store = nexus_store::Store::connect(&dsn, 4).await?;
        let pool = store.pool().clone();
        // One project, one goal reused, n independent runs each with 3 tasks.
        let project: uuid::Uuid =
            sqlx::query_scalar("INSERT INTO nexus_projects(name) VALUES('bench') RETURNING id")
                .fetch_one(&pool)
                .await?;
        let mut samples = Vec::with_capacity(n);
        for i in 0..n {
            let goal: uuid::Uuid = sqlx::query_scalar(
                "INSERT INTO nexus_goals(project_id, text, created_by) \
                 VALUES($1, 'g', 'bench') RETURNING id",
            )
            .bind(project)
            .fetch_one(&pool)
            .await?;
            let run: uuid::Uuid = sqlx::query_scalar(
                "INSERT INTO nexus_runs(project_id, goal_id, state, policy, created_by) \
                 VALUES($1,$2,'running','durable','bench') RETURNING id",
            )
            .bind(project)
            .bind(goal)
            .fetch_one(&pool)
            .await?;
            for t in 0..3 {
                sqlx::query(
                    "INSERT INTO nexus_run_tasks(run_id, project_id, task_key, title) \
                     VALUES($1,$2,$3,$3)",
                )
                .bind(run)
                .bind(project)
                .bind(format!("t{t}"))
                .execute(&pool)
                .await?;
            }
            let start = Instant::now();
            let _ = store.claim_task(run, &format!("w{i}"), 900).await?;
            samples.push(start.elapsed().as_secs_f64() * 1000.0);
        }
        Ok::<Vec<f64>, sqlx::Error>(samples)
    });
    match out {
        Ok(mut xs) => {
            xs.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let pct = |p: f64| xs[((xs.len() - 1) as f64 * p).round() as usize];
            let mean: f64 = xs.iter().sum::<f64>() / xs.len() as f64;
            let v = serde_json::json!({
                "n": xs.len(),
                "p50": (pct(0.50) * 100.0).round() / 100.0,
                "p99": (pct(0.99) * 100.0).round() / 100.0,
                "mean": (mean * 100.0).round() / 100.0,
            });
            println!("{v}");
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("bench failed: {e}");
            ExitCode::FAILURE
        }
    }
}

fn dsn_or_exit() -> Result<String, ExitCode> {
    match std::env::var("NEXUS_DB_URL").map(|d| nexus_store::to_sqlx_url(&d)) {
        Ok(v) if !v.trim().is_empty() => Ok(v),
        _ => {
            eprintln!("set NEXUS_DB_URL");
            Err(ExitCode::from(2))
        }
    }
}

fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("runtime")
}

fn parse_uuid(s: &str) -> Result<uuid::Uuid, ExitCode> {
    uuid::Uuid::parse_str(s).map_err(|e| {
        eprintln!("bad uuid {s}: {e}");
        ExitCode::from(2)
    })
}

fn cmd_run_state(
    project_id: &str,
    run_id: &str,
    state: &str,
    last_error: Option<&str>,
    strict: bool,
    actor: Option<&str>,
) -> ExitCode {
    use nexus_store::lifecycle::Strictness;
    let dsn = match dsn_or_exit() {
        Ok(d) => d,
        Err(e) => return e,
    };
    let (project, run) = match (parse_uuid(project_id), parse_uuid(run_id)) {
        (Ok(p), Ok(r)) => (p, r),
        (Err(e), _) | (_, Err(e)) => return e,
    };
    let strictness = if strict {
        Strictness::Strict
    } else {
        Strictness::Internal
    };
    let out = runtime().block_on(async move {
        let store = nexus_store::Store::connect(&dsn, 4).await?;
        store
            .set_run_state(project, run, state, last_error, strictness, actor)
            .await
            .map_err(|e| match e {
                nexus_store::lifecycle::LifecycleError::Db(db) => db,
                other => sqlx::Error::Protocol(other.to_string()),
            })
    });
    match out {
        Ok(Some(run)) => {
            println!("{}", serde_json::to_string(&run).unwrap());
            ExitCode::SUCCESS
        }
        Ok(None) => {
            println!("null");
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("run-state failed: {e}");
            ExitCode::FAILURE
        }
    }
}

fn cmd_reclaim(run_id: &str) -> ExitCode {
    let dsn = match dsn_or_exit() {
        Ok(d) => d,
        Err(e) => return e,
    };
    let run = match parse_uuid(run_id) {
        Ok(r) => r,
        Err(e) => return e,
    };
    let out = runtime().block_on(async move {
        let store = nexus_store::Store::connect(&dsn, 4).await?;
        store.reclaim_expired_full(run).await.map_err(|e| match e {
            nexus_store::lifecycle::LifecycleError::Db(db) => db,
            other => sqlx::Error::Protocol(other.to_string()),
        })
    });
    match out {
        Ok(tasks) => {
            println!("{}", serde_json::to_string(&tasks).unwrap());
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("reclaim failed: {e}");
            ExitCode::FAILURE
        }
    }
}

fn cmd_release(task_id: &str, error: &str, requeue: bool, lease_token: Option<i64>) -> ExitCode {
    let dsn = match dsn_or_exit() {
        Ok(d) => d,
        Err(e) => return e,
    };
    let task = match parse_uuid(task_id) {
        Ok(t) => t,
        Err(e) => return e,
    };
    let out = runtime().block_on(async move {
        let store = nexus_store::Store::connect(&dsn, 4).await?;
        store
            .release_task(task, error, requeue, None, lease_token)
            .await
            .map_err(|e| match e {
                nexus_store::lifecycle::LifecycleError::Db(db) => db,
                other => sqlx::Error::Protocol(other.to_string()),
            })
    });
    match out {
        Ok(task) => {
            println!("{}", serde_json::to_string(&task).unwrap());
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("release failed: {e}");
            ExitCode::FAILURE
        }
    }
}

fn cmd_postpone(
    task_id: &str,
    error: &str,
    seconds: f64,
    forget_tried: bool,
    lease_token: Option<i64>,
) -> ExitCode {
    let dsn = match dsn_or_exit() {
        Ok(d) => d,
        Err(e) => return e,
    };
    let task = match parse_uuid(task_id) {
        Ok(t) => t,
        Err(e) => return e,
    };
    let out = runtime().block_on(async move {
        let store = nexus_store::Store::connect(&dsn, 4).await?;
        store
            .postpone_task(
                task,
                &nexus_store::lifecycle::Postpone {
                    error: error.to_string(),
                    seconds,
                    forget_tried,
                    lease_token,
                    ..Default::default()
                },
            )
            .await
            .map_err(|e| match e {
                nexus_store::lifecycle::LifecycleError::Db(db) => db,
                other => sqlx::Error::Protocol(other.to_string()),
            })
    });
    match out {
        Ok(task) => {
            println!("{}", serde_json::to_string(&task).unwrap());
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("postpone failed: {e}");
            ExitCode::FAILURE
        }
    }
}

fn cmd_decide(run_id: &str) -> ExitCode {
    use nexus_store::lifecycle::Decision;
    let dsn = match dsn_or_exit() {
        Ok(d) => d,
        Err(e) => return e,
    };
    let run = match parse_uuid(run_id) {
        Ok(r) => r,
        Err(e) => return e,
    };
    let out = runtime().block_on(async move {
        let store = nexus_store::Store::connect(&dsn, 4).await?;
        store.decide_run(run).await
    });
    match out {
        Ok(d) => {
            let v = match d {
                Decision::Continue => serde_json::json!({"decision": "continue"}),
                Decision::Completed => serde_json::json!({"decision": "completed"}),
                Decision::Settle { state, reason } => {
                    serde_json::json!({"decision": "settle", "state": state, "reason": reason})
                }
                Decision::Pause { reason } => {
                    serde_json::json!({"decision": "pause", "reason": reason})
                }
            };
            println!("{}", serde_json::to_string(&v).unwrap());
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("decide failed: {e}");
            ExitCode::FAILURE
        }
    }
}
