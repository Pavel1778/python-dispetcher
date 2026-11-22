//! Real-PostgreSQL tests for the Rust claim transaction.
//!
//! These are not mocks: each test connects to a real PostgreSQL, applies the
//! real `migrations/*.sql`, builds a real run and real run tasks, and asserts
//! on what the database actually holds afterwards. The concurrency test starts
//! N Tokio tasks that claim together and proves each one took a *distinct*
//! task - the property the Python `FOR UPDATE SKIP LOCKED` claim exists to
//! provide.
//!
//! Isolation: one dedicated database for the whole test binary, created once
//! and migrated once into `public` (the same model the Python runner uses).
//! Tests never share a run, so they cannot see each other's claims. A scratch
//! *schema* inside a shared database is wrong here: the migrations look a
//! constraint up by table name across every schema, so a leftover schema from
//! an earlier run would break the next one.
//!
//! They skip cleanly when no database is configured (`TEST_SUPABASE_DB_URL` /
//! `SUPABASE_DB_URL` unset), so an environment without PostgreSQL reports a
//! skip rather than a false pass.

use nexus_store::Store;
use sqlx::postgres::PgPoolOptions;
use sqlx::{Executor, PgPool};
use std::path::PathBuf;
use std::sync::OnceLock;
use uuid::Uuid;

/// A dedicated multi-thread runtime that outlives every test.
///
/// The pool's background connections are bound to the runtime that created
/// them. `` gives each test its own runtime, so a pool created in
/// test A's runtime is dead when A ends. Creating the pool once on a runtime
/// that lives for the whole binary lets every test share it.
fn rt() -> &'static tokio::runtime::Runtime {
    static RT: OnceLock<tokio::runtime::Runtime> = OnceLock::new();
    RT.get_or_init(|| {
        tokio::runtime::Builder::new_multi_thread()
            .worker_threads(4)
            .enable_all()
            .build()
            .expect("build shared runtime")
    })
}

fn base_dsn() -> Option<String> {
    std::env::var("TEST_SUPABASE_DB_URL")
        .ok()
        .or_else(|| std::env::var("SUPABASE_DB_URL").ok())
        .filter(|s| !s.trim().is_empty())
}

/// Replace the database name in a libpq URL / DSN.
fn with_dbname(dsn: &str, name: &str) -> String {
    if dsn.contains("://") {
        let (head, tail) = match dsn.split_once('?') {
            Some((h, t)) => (h, Some(t)),
            None => (dsn, None),
        };
        let slash = head.rfind('/').expect("db name in URL");
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

/// The one migrated database for this test process, created on first use.
///
/// The pool is built on a dedicated OS thread driving [`rt`], the runtime that
/// lives for the whole binary. Building it *here* (not on a `#[test]`
/// thread) keeps its background tasks on the long-lived runtime, so the pool
/// survives every individual test's runtime.
fn db() -> Option<&'static PgPool> {
    base_dsn()?;
    static DB: OnceLock<PgPool> = OnceLock::new();
    Some(DB.get_or_init(|| {
        std::thread::spawn(|| rt().block_on(build_db()))
            .join()
            .expect("db init thread")
    }))
}

async fn build_db() -> PgPool {
    let dsn = base_dsn().unwrap();
    let name = format!("nexus_rs_test_{}", Uuid::new_v4().simple());
    let admin = with_dbname(&dsn, "postgres");
    let admin_pool = PgPoolOptions::new()
        .max_connections(2)
        .connect(&admin)
        .await
        .expect("connect to postgres maintenance database");

    let leftovers: Vec<String> =
        sqlx::query_scalar("SELECT datname FROM pg_database WHERE datname LIKE 'nexus_rs_test_%'")
            .fetch_all(&admin_pool)
            .await
            .unwrap_or_default();
    for old in leftovers {
        let _ = admin_pool
            .execute(
                format!(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{old}'"
                )
                .as_str(),
            )
            .await;
        let _ = admin_pool
            .execute(format!("DROP DATABASE IF EXISTS \"{old}\"").as_str())
            .await;
    }

    admin_pool
        .execute(format!("CREATE DATABASE \"{name}\"").as_str())
        .await
        .expect("create test database");

    let pool = PgPoolOptions::new()
        .max_connections(16)
        .connect(&with_dbname(&dsn, &name))
        .await
        .expect("connect to test database");

    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../migrations");
    let mut files: Vec<PathBuf> = std::fs::read_dir(&dir)
        .expect("migrations dir")
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().map(|x| x == "sql").unwrap_or(false))
        .collect();
    files.sort();
    for f in files {
        let sql = std::fs::read_to_string(&f).expect("read migration");
        pool.execute(sql.as_str())
            .await
            .unwrap_or_else(|e| panic!("migration {} failed: {e}", f.display()));
    }
    pool
}

/// Build a project, a run in `running`, and return `(project, run)`.
async fn make_run(pool: &PgPool, durable: bool) -> (Uuid, Uuid) {
    let project: Uuid =
        sqlx::query_scalar("INSERT INTO nexus_projects(name) VALUES($1) RETURNING id")
            .bind(format!("p-{}", Uuid::new_v4().simple()))
            .fetch_one(pool)
            .await
            .expect("project");
    let goal: Uuid = sqlx::query_scalar(
        "INSERT INTO nexus_goals(project_id, text, created_by) \
         VALUES($1, 'g', 'test') RETURNING id",
    )
    .bind(project)
    .fetch_one(pool)
    .await
    .expect("goal");
    let policy = if durable { "durable" } else { "bounded" };
    let run: Uuid = sqlx::query_scalar(
        "INSERT INTO nexus_runs(project_id, goal_id, state, policy, created_by) \
         VALUES($1, $2, 'running', $3, 'test') RETURNING id",
    )
    .bind(project)
    .bind(goal)
    .bind(policy)
    .fetch_one(pool)
    .await
    .expect("run");
    (project, run)
}

async fn add_task(
    pool: &PgPool,
    run: Uuid,
    project: Uuid,
    key: &str,
    deps: serde_json::Value,
    repository: &str,
    branch: &str,
) -> Uuid {
    sqlx::query_scalar(
        "INSERT INTO nexus_run_tasks(run_id, project_id, task_key, title, dependencies, \
         repository, branch) VALUES($1,$2,$3,$3,$4,$5,$6) RETURNING id",
    )
    .bind(run)
    .bind(project)
    .bind(key)
    .bind(deps)
    .bind(repository)
    .bind(branch)
    .fetch_one(pool)
    .await
    .expect("task")
}

#[test]
fn claims_the_oldest_due_task_and_fences_the_token() {
    rt().block_on(async {
        let Some(pool) = db() else {
            eprintln!("skip: no TEST_SUPABASE_DB_URL");
            return;
        };
        let store = Store::from_pool(pool.clone(), 0);
        let (project, run) = make_run(pool, true).await;
        let t1 = add_task(pool, run, project, "t1", serde_json::json!([]), "", "").await;
        let _t2 = add_task(pool, run, project, "t2", serde_json::json!(["t1"]), "", "").await;

        let claimed = store.claim_task(run, "w1", 900).await.expect("claim");
        let claimed = claimed.expect("a task was claimable");
        assert_eq!(claimed.id, t1, "the oldest task is taken first");
        assert_eq!(claimed.state, "dispatched");
        assert_eq!(claimed.attempt, 1);
        assert_eq!(claimed.lease_owner.as_deref(), Some("w1"));
        assert!(claimed.lease_token >= 1);

        assert!(store
            .claim_task(run, "w2", 900)
            .await
            .expect("claim")
            .is_none());

        assert!(store
            .holds_lease(claimed.id, claimed.lease_token)
            .await
            .unwrap());
        assert!(!store
            .holds_lease(claimed.id, claimed.lease_token - 1)
            .await
            .unwrap());

        let ok = store
            .mark_running(claimed.id, "conv-1", claimed.lease_token)
            .await
            .expect("mark_running");
        assert_eq!(ok.unwrap().state, "running");
    });
}
#[test]
fn a_stale_token_cannot_bind_a_conversation() {
    rt().block_on(async {
        let Some(pool) = db() else {
            return;
        };
        let store = Store::from_pool(pool.clone(), 0);
        let (project, run) = make_run(pool, true).await;
        let _ = add_task(pool, run, project, "t1", serde_json::json!([]), "", "").await;
        let claimed = store.claim_task(run, "w1", 900).await.unwrap().unwrap();

        // The lease must actually be expired for a reclaim to take it back.
        sqlx::query(
            "UPDATE nexus_run_tasks SET lease_expires = now() - interval '1 second' WHERE id=$1",
        )
        .bind(claimed.id)
        .execute(pool)
        .await
        .unwrap();
        assert_eq!(
            store.reclaim_expired(run).await.unwrap(),
            1,
            "the expired task is reclaimed"
        );
        let now = store.claim_task(run, "w2", 900).await.unwrap().unwrap();
        assert!(
            now.lease_token > claimed.lease_token,
            "the token moved forward"
        );

        let after = store
            .mark_running(claimed.id, "stale-conv", claimed.lease_token)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(after.lease_owner.as_deref(), Some("w2"));
        assert_ne!(after.conversation_id.as_deref(), Some("stale-conv"));
    });
}
#[test]
fn a_busy_branch_is_not_claimed_twice() {
    rt().block_on(async {
        let Some(pool) = db() else {
            return;
        };
        let store = Store::from_pool(pool.clone(), 0);
        let (project, run) = make_run(pool, true).await;
        let repo = format!("o/r-{}", Uuid::new_v4().simple());
        let a = add_task(
            pool,
            run,
            project,
            "a",
            serde_json::json!([]),
            &repo,
            "main",
        )
        .await;
        let _b = add_task(
            pool,
            run,
            project,
            "b",
            serde_json::json!([]),
            &repo,
            "main",
        )
        .await;

        let first = store.claim_task(run, "w1", 900).await.unwrap().unwrap();
        assert_eq!(first.id, a);
        assert!(
            store.claim_task(run, "w2", 900).await.unwrap().is_none(),
            "a branch with a task at work is not handed to a second task"
        );
    });
}
#[test]
fn concurrent_claims_take_distinct_tasks() {
    rt().block_on(async {
        let Some(pool) = db() else {
            return;
        };
        let store = Store::from_pool(pool.clone(), 0);
        let (project, run) = make_run(pool, true).await;
        for i in 0..10 {
            add_task(
                pool,
                run,
                project,
                &format!("t{i}"),
                serde_json::json!([]),
                "",
                "",
            )
            .await;
        }

        let mut handles = Vec::new();
        for w in 0..10 {
            let s = store.clone();
            handles.push(tokio::spawn(async move {
                s.claim_task(run, &format!("w{w}"), 900).await
            }));
        }
        let mut ids = Vec::new();
        for h in handles {
            if let Some(t) = h.await.unwrap().expect("claim call") {
                ids.push(t.id);
            }
        }
        ids.sort();
        ids.dedup();
        assert_eq!(
            ids.len(),
            10,
            "ten workers took ten distinct tasks, no duplicate"
        );

        assert!(store.claim_task(run, "w11", 900).await.unwrap().is_none());
    });
}
#[test]
fn a_paused_run_accepts_no_claim() {
    rt().block_on(async {
        let Some(pool) = db() else {
            return;
        };
        let store = Store::from_pool(pool.clone(), 0);
        let (project, run) = make_run(pool, true).await;
        add_task(pool, run, project, "t1", serde_json::json!([]), "", "").await;

        sqlx::query("UPDATE nexus_runs SET resume_at = now() + interval '1 hour' WHERE id=$1")
            .bind(run)
            .execute(pool)
            .await
            .unwrap();
        assert!(
            store.claim_task(run, "w1", 900).await.unwrap().is_none(),
            "a pausing run is not claimed from"
        );
    });
}

// ---------------------------------------------------------------- drift guard
//
// These predicates are read from `orchestration/sql/*.sql` via `include_str!`
// so the Rust and Python stores cannot disagree on what is claimable. This
// crate once carried its own copies and drifted: the copies predated
// `nexus_dispatch_intents` (migration 0043), so they would re-claim a task
// whose dispatch was in doubt. The guard below fails if that regresses.

#[test]
fn the_claim_predicates_are_the_shared_sql_files_not_a_local_copy() {
    // `TASK_DUE` and `BRANCH_FREE` must carry the dispatch-intent guard: a task
    // whose dispatch is `sending`/`uncertain`/`accepted`/`identified` is NOT
    // claimable. A hand-maintained copy silently lacked this.
    assert!(
        nexus_store::TASK_DUE.contains("nexus_dispatch_intents"),
        "TASK_DUE must include the nexus_dispatch_intents guard (shared task_due.sql)"
    );
    assert!(
        nexus_store::BRANCH_FREE.contains("nexus_dispatch_intents"),
        "BRANCH_FREE must include the nexus_dispatch_intents guard (shared branch_free.sql)"
    );
    // And they are exactly the shared files' bytes, not a paraphrase.
    assert_eq!(
        nexus_store::TASK_DUE,
        include_str!("../../../../orchestration/sql/task_due.sql")
    );
    assert_eq!(
        nexus_store::BRANCH_FREE,
        include_str!("../../../../orchestration/sql/branch_free.sql")
    );
}
