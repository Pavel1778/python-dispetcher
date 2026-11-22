//! Full-lifecycle benchmark: a complete run, claim -> execute -> settle ->
//! complete, over real PostgreSQL, with the same loop a deployment uses.
//!
//! Reports p50/p95/p99 of a full task lifecycle (one claim + one settle + the
//! run decision), throughput (tasks/s), peak RSS and wall time. This is the
//! "benchmark complete task lifecycle, not only database claim" the
//! authorization asks for.
//!
//! Usage:
//!   NEXUS_DB_URL=postgresql://... cargo run -p nexus-worker --release \
//!     --example lifecycle_bench -- --runs 20 --tasks 10

use async_trait::async_trait;
use nexus_store::{ClaimedTask, Store};
use nexus_worker::{Executor, Outcome, Worker};

use std::time::Instant;

struct Verify;

#[async_trait]
impl Executor for Verify {
    async fn execute(&self, _t: &ClaimedTask) -> Outcome {
        Outcome::Verified {
            reason: "ok".into(),
            commit: Some("bench".into()),
        }
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut runs = 20usize;
    let mut tasks_per_run = 10usize;
    let args: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--runs" => {
                runs = args.get(i + 1).and_then(|v| v.parse().ok()).unwrap_or(runs);
                i += 2;
            }
            "--tasks" => {
                tasks_per_run = args
                    .get(i + 1)
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(tasks_per_run);
                i += 2;
            }
            _ => i += 1,
        }
    }
    let dsn = std::env::var("NEXUS_DB_URL")?;
    let store = Store::connect(&dsn, 8).await?;
    let pool = store.pool().clone();

    let project: uuid::Uuid =
        sqlx::query_scalar("INSERT INTO nexus_projects(name) VALUES('bench') RETURNING id")
            .fetch_one(&pool)
            .await?;

    let mut run_ids = Vec::with_capacity(runs);
    for _ in 0..runs {
        let goal: uuid::Uuid = sqlx::query_scalar(
            "INSERT INTO nexus_goals(project_id, text, created_by) VALUES($1,'g','bench') RETURNING id",
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
        for t in 0..tasks_per_run {
            sqlx::query(
                "INSERT INTO nexus_run_tasks(run_id, project_id, task_key, title) VALUES($1,$2,$3,$3)",
            )
            .bind(run)
            .bind(project)
            .bind(format!("t{t}"))
            .execute(&pool)
            .await?;
        }
        run_ids.push(run);
    }

    let worker = Worker::new(store.clone(), Verify, "bench");
    let start = Instant::now();
    let mut task_latencies = Vec::new();
    let mut total_settled = 0usize;
    loop {
        let active = store.active_run_ids(1000).await?;
        if active.is_empty() {
            break;
        }
        let before = settled_count(&pool, &run_ids).await?;
        let t = Instant::now();
        let _report = worker.tick().await?;
        let elapsed = t.elapsed().as_secs_f64() * 1000.0;
        let after = settled_count(&pool, &run_ids).await?;
        let settled_this = (after - before).max(1) as f64;
        for _ in 0..(after - before) {
            task_latencies.push(elapsed / settled_this);
        }
        total_settled = after;
    }
    let wall = start.elapsed().as_secs_f64();

    task_latencies.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let pct = |p: f64| {
        if task_latencies.is_empty() {
            0.0
        } else {
            task_latencies[((task_latencies.len() - 1) as f64 * p).round() as usize]
        }
    };
    let rss = peak_rss_mb();
    let out = serde_json::json!({
        "runs": runs,
        "tasks_per_run": tasks_per_run,
        "total_tasks": total_settled,
        "wall_seconds": (wall * 1000.0).round() / 1000.0,
        "tasks_per_second": ((total_settled as f64) / wall * 100.0).round() / 100.0,
        "task_lifecycle_ms": {
            "n": task_latencies.len(),
            "p50": (pct(0.50) * 1000.0).round() / 1000.0,
            "p95": (pct(0.95) * 1000.0).round() / 1000.0,
            "p99": (pct(0.99) * 1000.0).round() / 1000.0,
        },
        "peak_rss_mb": rss,
    });
    println!("{}", serde_json::to_string_pretty(&out)?);
    Ok(())
}

async fn settled_count(pool: &sqlx::PgPool, run_ids: &[uuid::Uuid]) -> Result<usize, sqlx::Error> {
    let n: i64 = sqlx::query_scalar(
        "SELECT count(*)::bigint FROM nexus_run_tasks \
         WHERE run_id = ANY($1) AND state IN ('completed_verified','completed_no_changes')",
    )
    .bind(run_ids)
    .fetch_one(pool)
    .await?;
    Ok(n as usize)
}

/// Peak RSS in MB, read from /proc/self/status (Linux).
fn peak_rss_mb() -> f64 {
    if let Ok(s) = std::fs::read_to_string("/proc/self/status") {
        for line in s.lines() {
            if let Some(rest) = line.strip_prefix("VmHWM:") {
                let kb: f64 = rest
                    .split_whitespace()
                    .next()
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(0.0);
                return (kb / 1024.0 * 10.0).round() / 10.0;
            }
        }
    }
    0.0
}
