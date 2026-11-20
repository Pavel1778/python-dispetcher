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
    let mut options = PgConnectOptions::new().database(&db).host(&host);
    if args.first().is_some_and(|a| a == "sample") {
        // Compare to the legacy driver's default (no statement cache).
        options = options.statement_cache_capacity(0);
    }
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
    let output = match args
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>()
        .as_slice()
    {
        ["claim", project, run, worker, seconds, limit] => {
            let grant = nexus_storage::claim_task(
                &pool,
                Uuid::parse_str(project)?,
                Uuid::parse_str(run)?,
                worker,
                seconds.parse::<NonZeroU32>()?,
                limit.parse()?,
            )
            .await?;
            serde_json::to_value(grant.map(|g| {
                serde_json::json!({
                    "id": g.id.to_string(), "lease_token": g.lease_token
                })
            }))?
        }
        ["sample", project, run, count] => {
            use std::{collections::HashSet, time::Instant};
            let project = Uuid::parse_str(project)?;
            let run = Uuid::parse_str(run)?;
            let count: usize = count.parse()?;
            if !(1..=500).contains(&count) { return Err("sample size must be 1..500".into()); }
            let mut seen = HashSet::with_capacity(count);
            let mut samples = Vec::with_capacity(count);
            let start = Instant::now();
            for _ in 0..count {
                let t = Instant::now();
                let g = nexus_storage::claim_task(&pool, project, run, "bench", NonZeroU32::new(900).unwrap(), 0)
                    .await?.ok_or("missing grant")?;
                samples.push(t.elapsed().as_nanos() as u64);
                if g.lease_token != 1 || !seen.insert(g.id) { return Err("bad grant".into()); }
            }
            serde_json::json!({"samples_ns": samples, "wall_ns": start.elapsed().as_nanos() as u64})
        }
        ["reclaim", project, run, batch] => {
            let ids = nexus_storage::reclaim_expired(
                &pool,
                Uuid::parse_str(project)?,
                Uuid::parse_str(run)?,
                batch.parse()?,
            )
            .await?;
            serde_json::to_value(ids.into_iter().map(|id| id.to_string()).collect::<Vec<_>>())?
        }
        _ => {
            return Err(
                "expected claim PROJECT RUN WORKER SECONDS LIMIT or reclaim PROJECT RUN BATCH or sample PROJECT RUN COUNT"
                    .into(),
            )
        }
    };
    println!("{output}");
    pool.close().await;
    Ok(())
}
