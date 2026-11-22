//! Database restart / reconnection behaviour of the Rust store (Phase 4).
//!
//! The Python `PostgresStore` explicitly replaces a dropped connection. SQLx's
//! pool is expected to do the same via its default `test_before_acquire`: a
//! dead connection is discarded on acquire and a fresh one opened. This proves
//! it against a real PostgreSQL by terminating the store's own backends
//! server-side, then showing the next query still succeeds and the data is
//! intact - so a database restart or a pooler recycling its clients does not
//! wedge the worker.

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
            .worker_threads(2)
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

async fn build_db() -> (PgPool, String) {
    let dsn = base_dsn().unwrap();
    let name = format!("nexus_rs_rc_{}", Uuid::new_v4().simple());
    let admin = PgPoolOptions::new()
        .max_connections(2)
        .connect(&nexus_store::to_sqlx_url(&with_dbname(&dsn, "postgres")))
        .await
        .expect("admin");
    admin
        .execute(format!("CREATE DATABASE \"{name}\"").as_str())
        .await
        .expect("create");
    let pool = PgPoolOptions::new()
        .max_connections(4)
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
    (pool, name)
}

#[test]
fn the_store_recovers_after_its_connections_are_killed() {
    if base_dsn().is_none() {
        return;
    }
    rt().block_on(async {
        let (pool, dbname) = build_db().await;
        let store = Store::from_pool(pool.clone(), 0);

        // A project that must survive the reconnect.
        let project: Uuid =
            sqlx::query_scalar("INSERT INTO nexus_projects(name) VALUES('survivor') RETURNING id")
                .fetch_one(&pool)
                .await
                .expect("seed project");

        // Kill this database's backends server-side (a database restart or a
        // pooler recycling clients looks exactly like this to the pool).
        let admin = PgPoolOptions::new()
            .max_connections(1)
            .connect(&nexus_store::to_sqlx_url(&with_dbname(
                &base_dsn().unwrap(),
                "postgres",
            )))
            .await
            .expect("admin");
        sqlx::query(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity \
             WHERE datname = $1 AND pid <> pg_backend_pid()",
        )
        .bind(&dbname)
        .execute(&admin)
        .await
        .expect("terminate");

        // The next query must still work: SQLx discards the dead connection and
        // opens a fresh one. Give it a moment for the server to notice.
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        let count: i64 = sqlx::query_scalar("SELECT count(*)::bigint FROM nexus_projects")
            .fetch_one(&pool)
            .await
            .expect("a query after the kill must succeed (reconnect)");
        assert_eq!(count, 1, "the seeded project survived the reconnect");

        // And the higher-level store call works too.
        let runs = store
            .active_run_ids(10)
            .await
            .expect("store call after reconnect");
        assert!(runs.is_empty());

        // The project id is unchanged (no data loss).
        let name: String = sqlx::query_scalar("SELECT name FROM nexus_projects WHERE id=$1")
            .bind(project)
            .fetch_one(&pool)
            .await
            .expect("the same project is still readable");
        assert_eq!(name, "survivor");
    });
}
