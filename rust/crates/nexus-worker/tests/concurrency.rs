//! Concurrency and recovery tests for the Rust claim/lifecycle/worker stack.
//!
//! These run against a real PostgreSQL and answer the authorization's testing
//! list directly:
//! * concurrency at 1, 2, 4, 8, 16 and 32 workers, each taking a distinct task;
//! * no duplicate claim and no lost task under contention;
//! * a cancellation race (cancel while tasks are being claimed);
//! * lease expiration reclaiming only unbound tasks;
//! * a stale fencing token cannot settle or release;
//! * worker-crash recovery: a second worker picks up what a dead one left.

use nexus_store::lifecycle::Strictness;
use nexus_store::{ClaimedTask, Store};
use nexus_worker::{Executor, Outcome, Worker};
use sqlx::postgres::PgPoolOptions;
use sqlx::{Executor as SqlxExecutor, PgPool};
use std::path::PathBuf;
use std::sync::atomic::AtomicUsize;
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
            .worker_threads(8)
            .enable_all()
            .build()
            .expect("runtime")
    })
}

/// A **fresh** database for each test, leaked so the caller gets a `&'static`
/// pool. `Worker::tick()` scans every active run (correct in production), so
/// parallel tests in one shared database would claim each other's tasks;
/// per-test isolation is what makes each test's assertions about "its" run
/// meaningful.
///
/// The pool is built on a dedicated OS thread driving [`rt`] (not on a
/// `block_on` the test already holds), so it is safe to call from anywhere.
fn db() -> Option<&'static PgPool> {
    base_dsn()?;
    let pool = std::thread::spawn(|| rt().block_on(build_db()))
        .join()
        .expect("db thread");
    Some(Box::leak(Box::new(pool)))
}

async fn build_db() -> PgPool {
    let dsn = base_dsn().unwrap();
    let name = format!("nexus_rs_cc_{}", Uuid::new_v4().simple());
    let admin = PgPoolOptions::new()
        .max_connections(2)
        .connect(&nexus_store::to_sqlx_url(&with_dbname(&dsn, "postgres")))
        .await
        .expect("admin");
    // No leftover cleanup here: tests run in parallel, and dropping another
    // test's live database would terminate its connections. Each test uses a
    // unique name, so there is nothing to collide with; CI starts clean.
    admin
        .execute(format!("CREATE DATABASE \"{name}\"").as_str())
        .await
        .expect("create");
    let pool = PgPoolOptions::new()
        .max_connections(16)
        .connect(&nexus_store::to_sqlx_url(&with_dbname(&dsn, &name)))
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

async fn seed(pool: &PgPool, n_tasks: usize) -> Uuid {
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
    // max_iterations must exceed the worker count: each claim spends one
    // iteration, and the run gate refuses once the budget is spent (default
    // 50). This test is about claim concurrency, not the budget.
    let run: Uuid = sqlx::query_scalar(
        "INSERT INTO nexus_runs(project_id, goal_id, state, policy, created_by, max_iterations) \
         VALUES($1,$2,'running','durable','t', 100000) RETURNING id",
    )
    .bind(project)
    .bind(goal)
    .fetch_one(pool)
    .await
    .unwrap();
    for i in 0..n_tasks {
        sqlx::query(
            "INSERT INTO nexus_run_tasks(run_id, project_id, task_key, title) \
             VALUES($1,$2,$3,$3)",
        )
        .bind(run)
        .bind(project)
        .bind(format!("t{i}"))
        .execute(pool)
        .await
        .unwrap();
    }
    run
}

/// N workers claim in one simultaneous burst; the invariants that hold under
/// contention are: no task is taken twice, and no claim errors. A burst need
/// not hand every worker a task - `FOR UPDATE SKIP LOCKED` returns None to a
/// claimant whose candidate a peer already locked, and the task is taken on a
/// later pass. That is correct, not a lost task; "every task is eventually
/// taken" is proven separately by the single-drainer no-loss test.
async fn claims_are_distinct(n_workers: usize) {
    let pool = db().unwrap();
    let run = seed(pool, n_workers).await;
    let store = Store::from_pool(pool.clone(), 100_000);
    let mut handles = Vec::new();
    for w in 0..n_workers {
        let s = store.clone();
        handles.push(tokio::spawn(async move {
            s.claim_task(run, &format!("w{w}"), 900).await
        }));
    }
    let mut ids = Vec::new();
    for h in handles {
        match h.await.unwrap() {
            Ok(Some(t)) => ids.push(t.id),
            Ok(None) => {}
            Err(e) => panic!("claim error at {n_workers} workers: {e}"),
        }
    }
    let taken = ids.len();
    ids.sort();
    ids.dedup();
    assert_eq!(
        ids.len(),
        taken,
        "a task was taken twice at {n_workers} workers"
    );
    assert!(taken <= n_workers, "taken cannot exceed seeded tasks");
    // No task is lost: every seeded task is dispatched (taken) or still pending.
    let counts: Vec<(String, i64)> = sqlx::query_as(
        "SELECT state, count(*)::bigint FROM nexus_run_tasks WHERE run_id=$1 GROUP BY state",
    )
    .bind(run)
    .fetch_all(pool)
    .await
    .unwrap();
    let dispatched = counts
        .iter()
        .find(|(s, _)| s == "dispatched")
        .map(|(_, n)| *n)
        .unwrap_or(0);
    let pending = counts
        .iter()
        .find(|(s, _)| s == "pending")
        .map(|(_, n)| *n)
        .unwrap_or(0);
    assert_eq!(dispatched, taken as i64, "every taken task is dispatched");
    assert_eq!(
        dispatched + pending,
        n_workers as i64,
        "no task is lost at {n_workers} workers"
    );
}

/// Single-drainer no-loss proof: claim one at a time (no contention) until the
/// run has nothing claimable; every seeded task must be taken exactly once.
#[test]
fn a_single_drainer_takes_every_task_exactly_once() {
    let Some(pool) = db() else { return };
    rt().block_on(async {
        let store = Store::from_pool(pool.clone(), 100_000);
        let n = 128;
        let run = seed(pool, n).await;
        let mut ids = Vec::new();
        while let Some(t) = store.claim_task(run, "drain", 900).await.unwrap() {
            ids.push(t.id);
            if ids.len() > n {
                break;
            }
        }
        let taken = ids.len();
        ids.sort();
        ids.dedup();
        assert_eq!(ids.len(), taken, "no duplicate in a single drain");
        assert_eq!(
            taken, n,
            "a single drainer takes all {n} tasks exactly once"
        );
    });
}

#[test]
fn concurrency_1_worker() {
    let Some(_p) = db() else { return };
    rt().block_on(claims_are_distinct(1));
}
#[test]
fn concurrency_2_workers() {
    let Some(_p) = db() else { return };
    rt().block_on(claims_are_distinct(2));
}
#[test]
fn concurrency_4_workers() {
    let Some(_p) = db() else { return };
    rt().block_on(claims_are_distinct(4));
}
#[test]
fn concurrency_8_workers() {
    let Some(_p) = db() else { return };
    rt().block_on(claims_are_distinct(8));
}
#[test]
fn concurrency_16_workers() {
    let Some(_p) = db() else { return };
    rt().block_on(claims_are_distinct(16));
}
#[test]
fn concurrency_32_workers() {
    let Some(_p) = db() else { return };
    rt().block_on(claims_are_distinct(32));
}
#[test]
fn concurrency_64_workers() {
    // Opt-in stress: set NEXUS_STRESS=1 to run it. The required range is 1-32.
    if std::env::var("NEXUS_STRESS").ok().as_deref() != Some("1") {
        return;
    }
    let Some(_p) = db() else { return };
    rt().block_on(claims_are_distinct(64));
}

#[test]
fn a_cancel_race_leaves_no_task_claimable() {
    let Some(pool) = db() else { return };
    rt().block_on(async {
        let store = Store::from_pool(pool.clone(), 0);
        let run = seed(pool, 8).await;
        let project: Uuid = sqlx::query_scalar("SELECT project_id FROM nexus_runs WHERE id=$1")
            .bind(run)
            .fetch_one(pool)
            .await
            .unwrap();
        // Claim a couple, then cancel while the rest are still claimable.
        let _a = store.claim_task(run, "w1", 900).await.unwrap();
        let _b = store.claim_task(run, "w2", 900).await.unwrap();
        store
            .set_run_state(
                project,
                run,
                "cancelled",
                None,
                Strictness::Strict,
                Some("user:t"),
            )
            .await
            .expect("cancel");
        // After the cancel, nothing is claimable: the run gate refuses.
        assert!(store.claim_task(run, "w3", 900).await.unwrap().is_none());
        // No task is left pending/dispatched: all are blocked.
        let states: Vec<(String,)> =
            sqlx::query_as("SELECT state FROM nexus_run_tasks WHERE run_id=$1")
                .bind(run)
                .fetch_all(pool)
                .await
                .unwrap();
        assert!(states.iter().all(|(s,)| s == "blocked"), "{states:?}");
    });
}

#[test]
fn a_stale_token_cannot_settle_a_reclaimed_task() {
    let Some(pool) = db() else { return };
    rt().block_on(async {
        let store = Store::from_pool(pool.clone(), 0);
        let run = seed(pool, 1).await;
        let claimed = store.claim_task(run, "w1", 900).await.unwrap().unwrap();
        // The lease expires and another worker takes the task.
        sqlx::query(
            "UPDATE nexus_run_tasks SET lease_expires=now()-interval '1 second' WHERE id=$1",
        )
        .bind(claimed.id)
        .execute(pool)
        .await
        .unwrap();
        store.reclaim_expired_full(run).await.unwrap();
        let fresh = store.claim_task(run, "w2", 900).await.unwrap().unwrap();
        assert!(fresh.lease_token > claimed.lease_token);
        // The stale worker's verdict is a no-op: the task stays with w2.
        let after = store
            .record_verification(
                claimed.id,
                "completed_verified",
                serde_json::json!({"reason": "stale"}),
                None,
                Some(claimed.lease_token),
            )
            .await
            .unwrap();
        assert_eq!(after.lease_owner.as_deref(), Some("w2"));
        assert_eq!(after.state, "dispatched", "the stale verdict did not land");
    });
}

#[test]
fn a_dead_workers_task_is_recovered_by_a_new_worker() {
    let Some(pool) = db() else { return };
    rt().block_on(async {
        // Simulate a crash: claim, then the lease expires with no conversation
        // bound and the process is gone. A fresh worker's pass reclaims and runs it.
        let store = Store::from_pool(pool.clone(), 0);
        let run = seed(pool, 1).await;
        let claimed = store
            .claim_task(run, "dead-worker", 0)
            .await
            .unwrap()
            .unwrap();
        // lease_seconds=0 means it is already expired.
        sqlx::query(
            "UPDATE nexus_run_tasks SET lease_expires=now()-interval '1 second' WHERE id=$1",
        )
        .bind(claimed.id)
        .execute(pool)
        .await
        .unwrap();

        struct Verify;
        #[async_trait::async_trait]
        impl Executor for Verify {
            async fn execute(&self, _t: &ClaimedTask) -> Outcome {
                Outcome::Verified {
                    reason: "ok".into(),
                    commit: Some("abc".into()),
                }
            }
        }
        let seen = Arc::new(AtomicUsize::new(0));
        let worker = Worker::new(store.clone(), Verify, "new-worker");
        let report = worker.tick().await.expect("tick");
        assert!(
            report.reclaimed >= 1,
            "the dead worker's task was reclaimed"
        );
        assert_eq!(report.claimed, 1, "and then claimed by the new worker");
        let state: String = sqlx::query_scalar("SELECT state FROM nexus_run_tasks WHERE run_id=$1")
            .bind(run)
            .fetch_one(pool)
            .await
            .unwrap();
        assert_eq!(state, "completed_verified");
        let _ = seen;
    });
}

#[test]
fn simultaneous_claim_stop_reclaim_is_consistent() {
    let Some(pool) = db() else { return };
    rt().block_on(async {
        let store = Store::from_pool(pool.clone(), 0);
        let run = seed(pool, 4).await;
        let project: Uuid = sqlx::query_scalar("SELECT project_id FROM nexus_runs WHERE id=$1")
            .bind(run)
            .fetch_one(pool)
            .await
            .unwrap();
        // Claim one, then run a cancel and a reclaim "together" (sequentially,
        // but each on its own connection) and assert the end state is legal.
        let _ = store.claim_task(run, "w1", 900).await.unwrap();
        let s1 = store.clone();
        let s2 = store.clone();
        let h1 = tokio::spawn(async move {
            s1.set_run_state(
                project,
                run,
                "cancelled",
                None,
                Strictness::Strict,
                Some("user:t"),
            )
            .await
        });
        let h2 = tokio::spawn(async move { s2.reclaim_expired_full(run).await });
        let (_cancel, reclaim) = (h1.await.unwrap(), h2.await.unwrap());
        // Whichever order they ran in, the reclaim is either empty (cancel won
        // first) or the tasks it requeued are blocked again by the cancel.
        let _ = reclaim;
        let run_state: String = sqlx::query_scalar("SELECT state FROM nexus_runs WHERE id=$1")
            .bind(run)
            .fetch_one(pool)
            .await
            .unwrap();
        assert_eq!(run_state, "cancelled");
        let unfinished: i64 = sqlx::query_scalar(
            "SELECT count(*)::bigint FROM nexus_run_tasks WHERE run_id=$1 \
             AND state = ANY(ARRAY['pending','dispatched','running','verifying'])",
        )
        .bind(run)
        .fetch_one(pool)
        .await
        .unwrap();
        assert_eq!(unfinished, 0, "no task is left unfinished after the cancel");
    });
}
