//! Real-PostgreSQL tests for the Rust run-lifecycle module.
//!
//! Each asserts an *effect* on the database: a cancelled run blocks its
//! unfinished tasks and records a stop for the conversation at work; a failed
//! run does the same with a different outcome; `resume` on a blocked run only
//! moves when there is something to retry; a reclaim resets a durable task's
//! cycle; a stale fencing token cannot release or postpone.
//!
//! One dedicated database for the binary, migrated from the real
//! `migrations/`, exactly as `claim_parity.rs` does; each test builds its own
//! run, so tests never see each other's rows.

use nexus_store::lifecycle::{entered_from, Strictness};
use nexus_store::Store;
use sqlx::postgres::PgPoolOptions;
use sqlx::{Executor, PgPool};
use std::path::PathBuf;
use std::sync::OnceLock;
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
    static DB: OnceLock<PgPool> = OnceLock::new();
    Some(DB.get_or_init(|| {
        std::thread::spawn(|| rt().block_on(build_db()))
            .join()
            .expect("db thread")
    }))
}

async fn build_db() -> PgPool {
    let dsn = base_dsn().unwrap();
    let name = format!("nexus_rs_lc_{}", Uuid::new_v4().simple());
    let admin = PgPoolOptions::new()
        .max_connections(2)
        .connect(&with_dbname(&dsn, "postgres"))
        .await
        .expect("admin connect");
    let leftovers: Vec<String> =
        sqlx::query_scalar("SELECT datname FROM pg_database WHERE datname LIKE 'nexus_rs_lc_%'")
            .fetch_all(&admin)
            .await
            .unwrap_or_default();
    for old in leftovers {
        let _ = admin
            .execute(
                format!(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{old}'"
                )
                .as_str(),
            )
            .await;
        let _ = admin
            .execute(format!("DROP DATABASE IF EXISTS \"{old}\"").as_str())
            .await;
    }
    admin
        .execute(format!("CREATE DATABASE \"{name}\"").as_str())
        .await
        .expect("create db");
    let pool = PgPoolOptions::new()
        .max_connections(16)
        .connect(&with_dbname(&dsn, &name))
        .await
        .expect("connect test db");

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

async fn make_project(pool: &PgPool) -> Uuid {
    sqlx::query_scalar("INSERT INTO nexus_projects(name) VALUES($1) RETURNING id")
        .bind(format!("p-{}", Uuid::new_v4().simple()))
        .fetch_one(pool)
        .await
        .unwrap()
}

async fn make_run(pool: &PgPool, project: Uuid, policy: &str, state: &str) -> Uuid {
    let goal: Uuid = sqlx::query_scalar(
        "INSERT INTO nexus_goals(project_id, text, created_by) VALUES($1,'g','t') RETURNING id",
    )
    .bind(project)
    .fetch_one(pool)
    .await
    .unwrap();
    sqlx::query_scalar(
        "INSERT INTO nexus_runs(project_id, goal_id, state, policy, created_by) \
         VALUES($1,$2,$3,$4,'t') RETURNING id",
    )
    .bind(project)
    .bind(goal)
    .bind(state)
    .bind(policy)
    .fetch_one(pool)
    .await
    .unwrap()
}

async fn add_task(pool: &PgPool, run: Uuid, project: Uuid, key: &str, state: &str) -> Uuid {
    sqlx::query_scalar(
        "INSERT INTO nexus_run_tasks(run_id, project_id, task_key, title, state) \
         VALUES($1,$2,$3,$3,$4) RETURNING id",
    )
    .bind(run)
    .bind(project)
    .bind(key)
    .bind(state)
    .fetch_one(pool)
    .await
    .unwrap()
}

async fn state_of(pool: &PgPool, id: Uuid) -> String {
    sqlx::query_scalar("SELECT state FROM nexus_run_tasks WHERE id=$1")
        .bind(id)
        .fetch_one(pool)
        .await
        .unwrap()
}

#[test]
fn the_state_table_matches_python() {
    // The exact entries of `_RUN_ENTERED_FROM`. A change on either side that the
    // other does not follow fails here and in the Python parity test.
    assert_eq!(entered_from("running").unwrap(), &["paused", "blocked"]);
    assert_eq!(entered_from("paused").unwrap(), &["running"]);
    assert_eq!(entered_from("waiting_for_approval").unwrap(), &["running"]);
    assert_eq!(entered_from("blocked").unwrap(), &["running"]);
    assert_eq!(entered_from("completed").unwrap(), &["running"]);
    assert_eq!(entered_from("failed").unwrap(), &["running"]);
    assert_eq!(
        entered_from("cancelled").unwrap(),
        &["running", "paused", "waiting_for_approval", "blocked"]
    );
    assert!(entered_from("nonsense").is_none());
}

#[test]
fn cancel_blocks_unfinished_tasks_and_records_a_stop() {
    let Some(pool) = db() else {
        return;
    };
    rt().block_on(async {
        let store = Store::from_pool(pool.clone(), 0);
        let project = make_project(pool).await;
        let run = make_run(pool, project, "durable", "running").await;
        let conv = format!("conv-{}", Uuid::new_v4().simple());
        // One task at work (has a conversation), one pending.
        let at_work = add_task(pool, run, project, "a", "running").await;
        sqlx::query("UPDATE nexus_run_tasks SET conversation_id=$1 WHERE id=$2")
            .bind(&conv)
            .bind(at_work)
            .execute(pool)
            .await
            .unwrap();
        let pending = add_task(pool, run, project, "b", "pending").await;

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

        // Both unfinished tasks are blocked; a stop is owed for the conversation.
        assert_eq!(state_of(pool, at_work).await, "blocked");
        assert_eq!(state_of(pool, pending).await, "blocked");
        let stops: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM nexus_executor_stops WHERE run_id=$1 AND state='requested'",
        )
        .bind(run)
        .fetch_one(pool)
        .await
        .unwrap();
        assert_eq!(stops, 1, "the conversation at work is owed a stop");
        // The run is hidden and terminal.
        let (state, hidden): (String, bool) =
            sqlx::query_as("SELECT state, hidden_at IS NOT NULL FROM nexus_runs WHERE id=$1")
                .bind(run)
                .fetch_one(pool)
                .await
                .unwrap();
        assert_eq!(state, "cancelled");
        assert!(hidden, "a cancelled run leaves the active list");
    });
}

#[test]
fn an_illegal_transition_is_refused_when_strict_and_skipped_when_internal() {
    let Some(pool) = db() else {
        return;
    };
    rt().block_on(async {
        let store = Store::from_pool(pool.clone(), 0);
        let project = make_project(pool).await;
        let run = make_run(pool, project, "durable", "completed").await;
        // completed -> running is not in the table.
        let strict = store
            .set_run_state(project, run, "running", None, Strictness::Strict, None)
            .await;
        assert!(
            strict.is_err(),
            "a user request refuses an illegal transition"
        );
        let internal = store
            .set_run_state(project, run, "running", None, Strictness::Internal, None)
            .await
            .expect("internal does not error");
        assert!(internal.is_none(), "the scheduler sees None, not an error");
    });
}

#[test]
fn resume_of_a_blocked_run_needs_something_to_retry() {
    let Some(pool) = db() else {
        return;
    };
    rt().block_on(async {
        let store = Store::from_pool(pool.clone(), 0);
        let project = make_project(pool).await;
        let run = make_run(pool, project, "durable", "blocked").await;
        // A task that used all its attempts and did not verify.
        let t = add_task(pool, run, project, "a", "failed").await;
        sqlx::query("UPDATE nexus_run_tasks SET attempt=max_attempts WHERE id=$1")
            .bind(t)
            .execute(pool)
            .await
            .unwrap();

        // A durable run resets the cycle and requeues it.
        let resumed = store
            .set_run_state(project, run, "running", None, Strictness::Strict, None)
            .await
            .expect("resume with a retryable task");
        assert_eq!(resumed.unwrap().state, "running");
        let (state, cycle, attempt): (String, i32, i32) =
            sqlx::query_as("SELECT state, cycle, attempt FROM nexus_run_tasks WHERE id=$1")
                .bind(t)
                .fetch_one(pool)
                .await
                .unwrap();
        assert_eq!(state, "pending");
        assert_eq!(cycle, 1, "a durable run starts a new work cycle");
        assert_eq!(attempt, 0, "attempts are restored");

        // A blocked run with nothing to retry cannot be resumed.
        let run2 = make_run(pool, project, "bounded", "blocked").await;
        add_task(pool, run2, project, "x", "completed_verified").await;
        let refused = store
            .set_run_state(project, run2, "running", None, Strictness::Strict, None)
            .await;
        assert!(refused.is_err(), "nothing to retry: the resume is refused");
    });
}

#[test]
fn a_stale_token_cannot_release_or_postpone() {
    let Some(pool) = db() else {
        return;
    };
    rt().block_on(async {
        let store = Store::from_pool(pool.clone(), 0);
        let project = make_project(pool).await;
        let run = make_run(pool, project, "durable", "running").await;
        let t = add_task(pool, run, project, "a", "dispatched").await;
        // The claim handed out token 5.
        sqlx::query("UPDATE nexus_run_tasks SET lease_token=5, lease_owner='w1' WHERE id=$1")
            .bind(t)
            .execute(pool)
            .await
            .unwrap();

        // A stale holder (token 4) cannot release it: the state is unchanged.
        let stale = store
            .release_task(t, "boom", true, None, Some(4))
            .await
            .expect("release call");
        assert_eq!(stale.state, "dispatched", "a stale release is a no-op");

        // The real holder (token 5) can.
        let real = store
            .release_task(t, "boom", true, None, Some(5))
            .await
            .expect("release call");
        assert_eq!(real.state, "pending");
        assert!(real.lease_token > 5, "the token moved forward on release");
    });
}

#[test]
fn postpone_gives_back_the_attempt_and_the_iteration() {
    let Some(pool) = db() else {
        return;
    };
    rt().block_on(async {
        let store = Store::from_pool(pool.clone(), 0);
        let project = make_project(pool).await;
        let run = make_run(pool, project, "durable", "running").await;
        sqlx::query("UPDATE nexus_runs SET iteration=1, total_iterations=1 WHERE id=$1")
            .bind(run)
            .execute(pool)
            .await
            .unwrap();
        let t = add_task(pool, run, project, "a", "dispatched").await;
        sqlx::query("UPDATE nexus_run_tasks SET attempt=1 WHERE id=$1")
            .bind(t)
            .execute(pool)
            .await
            .unwrap();

        let after = store
            .postpone_task(
                t,
                &nexus_store::lifecycle::Postpone {
                    error: "429".into(),
                    seconds: 15.0,
                    ..Default::default()
                },
            )
            .await
            .expect("postpone");
        assert_eq!(after.state, "pending");
        assert_eq!(after.attempt, 0, "the attempt is given back");
        let (it, total): (i32, i32) =
            sqlx::query_as("SELECT iteration, total_iterations FROM nexus_runs WHERE id=$1")
                .bind(run)
                .fetch_one(pool)
                .await
                .unwrap();
        assert_eq!((it, total), (0, 0), "the iteration is given back too");
    });
}

#[test]
fn reclaim_resets_a_durable_task_cycle_after_max_attempts() {
    let Some(pool) = db() else {
        return;
    };
    rt().block_on(async {
        let store = Store::from_pool(pool.clone(), 0);
        let project = make_project(pool).await;
        let run = make_run(pool, project, "durable", "running").await;
        let t = add_task(pool, run, project, "a", "dispatched").await;
        // Expired lease, no conversation, attempts spent.
        sqlx::query(
            "UPDATE nexus_run_tasks SET attempt=max_attempts, lease_expires=now() - interval '1 second', \
               conversation_id=NULL WHERE id=$1",
        )
        .bind(t)
        .execute(pool)
        .await
        .unwrap();

        let reclaimed = store.reclaim_expired_full(run).await.expect("reclaim");
        assert_eq!(reclaimed.len(), 1);
        let row = &reclaimed[0];
        assert_eq!(row.state, "pending");
        assert_eq!(row.cycle, 1, "a durable run starts the next cycle");
        assert_eq!(row.attempt, 0, "attempts are restored");
    });
}

#[test]
fn reclaim_leaves_a_task_whose_conversation_exists() {
    let Some(pool) = db() else {
        return;
    };
    rt().block_on(async {
        let store = Store::from_pool(pool.clone(), 0);
        let project = make_project(pool).await;
        let run = make_run(pool, project, "durable", "running").await;
        let t = add_task(pool, run, project, "a", "running").await;
        // Expired lease, but a conversation is bound: not lost, so not reclaimed.
        sqlx::query(
            "UPDATE nexus_run_tasks SET lease_expires=now() - interval '1 second', \
               conversation_id='conv-x' WHERE id=$1",
        )
        .bind(t)
        .execute(pool)
        .await
        .unwrap();
        let reclaimed = store.reclaim_expired_full(run).await.expect("reclaim");
        assert!(reclaimed.is_empty(), "a bound conversation is not requeued");
        assert_eq!(state_of(pool, t).await, "running");
    });
}

#[test]
fn is_deadlock_recognises_a_real_postgres_deadlock() {
    let Some(pool) = db() else {
        return;
    };
    rt().block_on(async {
        // Two transactions acquire two rows in opposite order and deadlock.
        // PostgreSQL aborts one with SQLSTATE 40P01; is_deadlock must see it.
        let project = make_project(pool).await;
        let a = add_task(
            pool,
            make_run(pool, project, "durable", "running").await,
            project,
            "a",
            "pending",
        )
        .await;
        let b = add_task(
            pool,
            make_run(pool, project, "durable", "running").await,
            project,
            "b",
            "pending",
        )
        .await;

        let p1 = pool.clone();
        let p2 = pool.clone();
        let t1 = tokio::spawn(async move {
            let mut tx = p1.begin().await.unwrap();
            sqlx::query("SELECT 1 FROM nexus_run_tasks WHERE id=$1 FOR UPDATE")
                .bind(a)
                .execute(&mut *tx)
                .await
                .unwrap();
            tokio::time::sleep(std::time::Duration::from_millis(150)).await;
            let r = sqlx::query("SELECT 1 FROM nexus_run_tasks WHERE id=$1 FOR UPDATE")
                .bind(b)
                .execute(&mut *tx)
                .await;
            if let Err(e) = &r {
                let _ = tx.rollback().await;
                return Some(e.to_string());
            }
            let _ = tx.commit().await;
            None
        });
        let t2 = tokio::spawn(async move {
            let mut tx = p2.begin().await.unwrap();
            sqlx::query("SELECT 1 FROM nexus_run_tasks WHERE id=$1 FOR UPDATE")
                .bind(b)
                .execute(&mut *tx)
                .await
                .unwrap();
            tokio::time::sleep(std::time::Duration::from_millis(150)).await;
            let r = sqlx::query("SELECT 1 FROM nexus_run_tasks WHERE id=$1 FOR UPDATE")
                .bind(a)
                .execute(&mut *tx)
                .await;
            if let Err(e) = &r {
                let _ = tx.rollback().await;
                return Some(e.to_string());
            }
            let _ = tx.commit().await;
            None
        });
        let (r1, r2) = (t1.await.unwrap(), t2.await.unwrap());
        // Exactly one of them must have been chosen as the deadlock victim.
        let victim = r1.or(r2).expect("one transaction is the deadlock victim");
        assert!(victim.to_lowercase().contains("deadlock"), "got: {victim}");
    });
}

#[test]
fn a_cancel_racing_task_settles_always_ends_cancelled() {
    let Some(pool) = db() else {
        return;
    };
    rt().block_on(async {
        // Repeat cancel-vs-settle over many runs; the cancel must always land
        // (or be a legal no-op) and never surface an error. This is a
        // stress/consistency test: it does NOT deterministically force the
        // internal 40P01 deadlock, so it does not by itself prove the retry
        // (that is what `is_deadlock_recognises_a_real_postgres_deadlock` and
        // the retry loop are for). It does prove the operation is safe under a
        // concurrent settle.
        let store = Store::from_pool(pool.clone(), 0);
        for _ in 0..25 {
            let project = make_project(pool).await;
            let run = make_run(pool, project, "durable", "running").await;
            let mut tids = Vec::new();
            for i in 0..6 {
                tids.push(add_task(pool, run, project, &format!("t{i}"), "running").await);
            }
            // A "worker" that settles one task concurrently with the cancel.
            let s2 = store.clone();
            let settle_tid = tids[0];
            let worker = tokio::spawn(async move {
                s2.record_verification(
                    settle_tid,
                    "completed_verified",
                    serde_json::json!({"reason": "ok"}),
                    None,
                    None,
                )
                .await
            });
            let cancel = store
                .set_run_state(
                    project,
                    run,
                    "cancelled",
                    None,
                    Strictness::Strict,
                    Some("user:t"),
                )
                .await;
            let _ = worker.await.unwrap();
            // The cancel must have landed (or been a legal no-op), never a raw
            // deadlock error.
            match &cancel {
                Ok(_) => {}
                Err(e) => panic!("cancel surfaced an error under contention: {e}"),
            }
            let state: String = sqlx::query_scalar("SELECT state FROM nexus_runs WHERE id=$1")
                .bind(run)
                .fetch_one(pool)
                .await
                .unwrap();
            assert_eq!(state, "cancelled");
        }
    });
}
