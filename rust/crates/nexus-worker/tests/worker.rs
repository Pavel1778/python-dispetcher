//! Real-PostgreSQL tests for the worker loop and the run decision.
//!
//! The loop is driven against a real database with a deterministic executor,
//! so the assertions are on what the loop actually did to the rows: a task
//! claimed, settled to the executor's verdict, and the run completed only when
//! every task verified. `decide` is also checked as a pure function.

use async_trait::async_trait;
use nexus_store::{ClaimedTask, Store};
use nexus_worker::{Executor, Outcome, Worker};
use sqlx::postgres::PgPoolOptions;
use sqlx::{Executor as SqlxExecutor, PgPool};
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, OnceLock};
use uuid::Uuid;

fn base_dsn() -> Option<String> {
    std::env::var("TEST_SUPABASE_DB_URL")
        .ok()
        .or_else(|| std::env::var("SUPABASE_DB_URL").ok())
        .filter(|s| !s.trim().is_empty())
}

fn rt() -> &'static tokio::runtime::Runtime {
    static RT: OnceLock<tokio::runtime::Runtime> = OnceLock::new();
    RT.get_or_init(|| {
        tokio::runtime::Builder::new_multi_thread()
            .worker_threads(4)
            .enable_all()
            .build()
            .expect("runtime")
    })
}

fn with_dbname(dsn: &str, name: &str) -> String {
    if dsn.contains("://") {
        let (head, tail) = match dsn.split_once('?') {
            Some((h, t)) => (h, Some(t)),
            None => (dsn, None),
        };
        let slash = head.rfind('/').expect("db name");
        let mut out = format!("{}/{name}", &head[..slash]);
        if let Some(t) = tail {
            out.push('?');
            out.push_str(t);
        }
        out
    } else {
        let mut parts: Vec<String> = Vec::new();
        let mut found = false;
        for tok in dsn.split_whitespace() {
            if let Some((k, _)) = tok.split_once('=') {
                if k == "dbname" {
                    parts.push(format!("dbname={name}"));
                    found = true;
                    continue;
                }
            }
            parts.push(tok.to_string());
        }
        if !found {
            parts.push(format!("dbname={name}"));
        }
        parts.join(" ")
    }
}

fn db() -> Option<&'static PgPool> {
    base_dsn()?;
    // A **fresh** database per call: `Worker::tick()` scans every active run
    // (correct in production), so tests in one shared database would advance
    // each other's runs. The pool is leaked so helpers that take `&PgPool`
    // keep working; a test binary leaks a handful of pools, which is fine.
    let pool = std::thread::spawn(|| rt().block_on(build_db()))
        .join()
        .expect("db thread");
    Some(Box::leak(Box::new(pool)))
}

async fn build_db() -> PgPool {
    let dsn = base_dsn().unwrap();
    let name = format!("nexus_rs_wk_{}", Uuid::new_v4().simple());
    let admin = PgPoolOptions::new()
        .max_connections(2)
        .connect(&with_dbname(&dsn, "postgres"))
        .await
        .expect("admin");
    // No leftover-drop: tests run in parallel, and dropping another test's live
    // database terminates its connections. Each test has a unique name.
    admin
        .execute(format!("CREATE DATABASE \"{name}\"").as_str())
        .await
        .expect("create");
    let pool = PgPoolOptions::new()
        .max_connections(16)
        .connect(&with_dbname(&dsn, &name))
        .await
        .expect("connect");
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../migrations");
    let mut files: Vec<PathBuf> = std::fs::read_dir(&dir)
        .expect("migrations")
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().map(|x| x == "sql").unwrap_or(false))
        .collect();
    files.sort();
    for f in files {
        let sql = std::fs::read_to_string(&f).unwrap();
        pool.execute(sql.as_str())
            .await
            .unwrap_or_else(|e| panic!("migration {} failed: {e}", f.display()));
    }
    pool
}

/// An executor that always verifies, and counts how many tasks it saw.
struct AlwaysVerify {
    seen: Arc<AtomicUsize>,
}

#[async_trait]
impl Executor for AlwaysVerify {
    async fn execute(&self, _task: &ClaimedTask) -> Outcome {
        self.seen.fetch_add(1, Ordering::SeqCst);
        Outcome::Verified {
            reason: "ok".into(),
            commit: Some("deadbeef".into()),
        }
    }
}

async fn setup_run(pool: &PgPool, task_states: &[&str]) -> (Uuid, Uuid) {
    let project: Uuid =
        sqlx::query_scalar("INSERT INTO nexus_projects(name) VALUES($1) RETURNING id")
            .bind(format!("p-{}", Uuid::new_v4().simple()))
            .fetch_one(pool)
            .await
            .unwrap();
    let goal: Uuid = sqlx::query_scalar(
        "INSERT INTO nexus_goals(project_id, text, created_by) VALUES($1,'g','t') RETURNING id",
    )
    .bind(project)
    .fetch_one(pool)
    .await
    .unwrap();
    let run: Uuid = sqlx::query_scalar(
        "INSERT INTO nexus_runs(project_id, goal_id, state, policy, created_by) \
         VALUES($1,$2,'running','durable','t') RETURNING id",
    )
    .bind(project)
    .bind(goal)
    .fetch_one(pool)
    .await
    .unwrap();
    for (i, st) in task_states.iter().enumerate() {
        sqlx::query(
            "INSERT INTO nexus_run_tasks(run_id, project_id, task_key, title, state) \
             VALUES($1,$2,$3,$3,$4)",
        )
        .bind(run)
        .bind(project)
        .bind(format!("t{i}"))
        .bind(*st)
        .execute(pool)
        .await
        .unwrap();
    }
    (project, run)
}

#[test]
fn decide_run_matches_the_python_branching() {
    let Some(pool) = db() else {
        return;
    };
    rt().block_on(async {
        use nexus_store::lifecycle::Decision;
        let store = Store::from_pool(pool.clone(), 0);
        // No tasks -> failed.
        let (_p, empty_run) = setup_run(pool, &[]).await;
        assert!(matches!(
            store.decide_run(empty_run).await.unwrap(),
            Decision::Settle {
                state: "failed",
                ..
            }
        ));
        // All verified -> completed.
        let (_p2, done_run) = setup_run(pool, &["completed_verified", "completed_verified"]).await;
        assert_eq!(
            store.decide_run(done_run).await.unwrap(),
            Decision::Completed
        );
        // In flight -> continue.
        let (_p3, busy_run) = setup_run(pool, &["completed_verified", "running"]).await;
        assert_eq!(
            store.decide_run(busy_run).await.unwrap(),
            Decision::Continue
        );
        // Nothing unfinished, not all verified -> blocked.
        let (_p4, mixed_run) =
            setup_run(pool, &["completed_verified", "completed_unverified"]).await;
        assert!(matches!(
            store.decide_run(mixed_run).await.unwrap(),
            Decision::Settle {
                state: "blocked",
                ..
            }
        ));
    });
}

#[test]
fn the_loop_claims_settles_and_completes_a_run() {
    let Some(pool) = db() else {
        return;
    };
    rt().block_on(async {
        let store = Store::from_pool(pool.clone(), 0);
        let (project, run) = setup_run(pool, &["pending"]).await;
        let seen = Arc::new(AtomicUsize::new(0));
        let worker = Worker::new(store.clone(), AlwaysVerify { seen: seen.clone() }, "w1");
        // First pass: claim, execute, settle; the run is now all-verified and
        // the same pass decides it complete.
        let report = worker.tick().await.expect("tick");
        assert_eq!(report.claimed, 1);
        assert_eq!(report.settled, 1);
        assert_eq!(seen.load(Ordering::SeqCst), 1);

        let (state,): (String,) =
            sqlx::query_as("SELECT state FROM nexus_run_tasks WHERE run_id=$1")
                .bind(run)
                .fetch_one(pool)
                .await
                .unwrap();
        assert_eq!(state, "completed_verified");
        let run_state: String = sqlx::query_scalar("SELECT state FROM nexus_runs WHERE id=$1")
            .bind(run)
            .fetch_one(pool)
            .await
            .unwrap();
        assert_eq!(run_state, "completed");
        // The run left the active list.
        let active = store.active_run_ids(100).await.unwrap();
        assert!(!active.contains(&run));
        let _ = project;
    });
}

#[test]
fn a_second_pass_does_not_reclaim_a_settled_task() {
    let Some(pool) = db() else {
        return;
    };
    rt().block_on(async {
        let store = Store::from_pool(pool.clone(), 0);
        let (_project, run) = setup_run(pool, &["pending"]).await;
        let seen = Arc::new(AtomicUsize::new(0));
        let worker = Worker::new(store.clone(), AlwaysVerify { seen: seen.clone() }, "w1");
        worker.tick().await.expect("first");
        // The run is completed; a second pass over active runs does nothing.
        let report = worker.tick().await.expect("second");
        assert_eq!(report.claimed, 0);
        assert_eq!(
            seen.load(Ordering::SeqCst),
            1,
            "the executor ran exactly once"
        );
        let _ = run;
    });
}

#[test]
fn a_blocked_outcome_blocks_the_run() {
    let Some(pool) = db() else {
        return;
    };
    struct AlwaysBlocked;
    #[async_trait]
    impl Executor for AlwaysBlocked {
        async fn execute(&self, _t: &ClaimedTask) -> Outcome {
            Outcome::Blocked {
                reason: "no usable account".into(),
            }
        }
    }
    rt().block_on(async {
        let store = Store::from_pool(pool.clone(), 0);
        let (_p, run) = setup_run(pool, &["pending"]).await;
        let worker = Worker::new(store.clone(), AlwaysBlocked, "w1");
        let report = worker.tick().await.expect("tick");
        assert_eq!(report.claimed, 1);
        // The task is blocked; the run has nothing unfinished and not all
        // verified, so it is blocked too (not completed, not spun).
        let task_state: String =
            sqlx::query_scalar("SELECT state FROM nexus_run_tasks WHERE run_id=$1")
                .bind(run)
                .fetch_one(pool)
                .await
                .unwrap();
        assert_eq!(task_state, "blocked");
        let run_state: String = sqlx::query_scalar("SELECT state FROM nexus_runs WHERE id=$1")
            .bind(run)
            .fetch_one(pool)
            .await
            .unwrap();
        assert_eq!(run_state, "blocked");
    });
}
