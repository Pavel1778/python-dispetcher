//! Test-only process boundary for tests/test_rust_claim.py. No URL or secrets
//! on argv, no production mode. Exiting after grant simulates a lost worker.
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
use std::{env, error::Error, num::NonZeroU32, time::Duration};
use uuid::Uuid;

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), Box<dyn Error>> {
    let db = env::var("PGDATABASE")?;
    let host = env::var("PGHOST")?;
    if !(db
        .strip_prefix("nexus_test_")
        .is_some_and(|s| s.len() == 12 && s.bytes().all(|c| c.is_ascii_hexdigit()))
        && (host == "127.0.0.1" || host == "::1" || host == "localhost" || host.starts_with('/')))
    {
        return Err("requires the isolated local pytest database".into());
    }
    let args: Vec<_> = env::args().skip(1).collect();
    let [project, run, worker, seconds, limit] = args.as_slice() else {
        return Err(
            "usage: claim_probe PROJECT_UUID RUN_UUID WORKER LEASE_SECONDS ACTIVE_LIMIT".into(),
        );
    };
    let options = PgConnectOptions::new().database(&db).host(&host);
    let pool = PgPoolOptions::new()
        .max_connections(1)
        .acquire_timeout(Duration::from_secs(5))
        .connect_with(options)
        .await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    if actual != db {
        return Err("unexpected database; refusing claim".into());
    }
    let grant = nexus_storage::claim_task(
        &pool,
        Uuid::parse_str(project)?,
        Uuid::parse_str(run)?,
        worker,
        seconds.parse::<NonZeroU32>()?,
        limit.parse()?,
    )
    .await?;
    println!(
        "{}",
        serde_json::to_string(&grant.map(|g| serde_json::json!({
            "id": g.id.to_string(), "lease_token": g.lease_token
        })))?
    );
    pool.close().await;
    Ok(())
}
