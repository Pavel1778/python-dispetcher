// Rust DB concurrency benchmark prototype (variant C).
//
// Isolated research program: it is NOT the NEXUS backend and shares nothing
// with the runtime. It runs the SAME workload as the Python benchmark
// (benchmarks/db_concurrency_bench.py) against the SAME PostgreSQL 17:
//
//   SELECT count(*) FROM nexus_conversation_messages WHERE conversation_id = $1
//
// It uses a sqlx async connection pool (the Rust/SQLx variant the migration
// proposes) and measures throughput and per-query latency at the same
// concurrency levels. Output is JSON on stdout so it can be compared with the
// Python variants by the same summariser.

use std::time::Instant;

use sqlx::postgres::PgPoolOptions;
use sqlx::Row;

const SQL: &str = "SELECT count(*) AS n FROM nexus_conversation_messages WHERE conversation_id = $1";

fn percentile(sorted: &[f64], p: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let idx = ((p * (sorted.len() as f64 - 1.0)).round() as usize).min(sorted.len() - 1);
    (sorted[idx] * 1000.0).round() / 1000.0
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let dsn = std::env::var("TEST_SUPABASE_DB_URL")
        .map_err(|_| "set TEST_SUPABASE_DB_URL")?;
    let per_worker: usize = std::env::var("PER_WORKER")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(40);
    let levels: Vec<usize> = vec![1, 5, 10, 25, 50, 100];

    let pool = PgPoolOptions::new()
        .max_connections(100)
        .min_connections(1)
        .acquire_timeout(std::time::Duration::from_secs(10))
        .connect(&dsn)
        .await?;

    let cid: uuid::Uuid = sqlx::query("SELECT id FROM nexus_conversations LIMIT 1")
        .fetch_one(&pool)
        .await?
        .try_get("id")?;

    // Warm-up.
    for _ in 0..20 {
        let _ = sqlx::query(SQL).bind(cid).fetch_one(&pool).await?;
    }

    let mut results = serde_json::Map::new();
    for &conc in &levels {
        let total_ops = conc * per_worker;
        let started = Instant::now();
        let mut handles = Vec::with_capacity(conc);
        for _ in 0..conc {
            let pool = pool.clone();
            handles.push(tokio::spawn(async move {
                let mut lat = Vec::with_capacity(per_worker);
                for _ in 0..per_worker {
                    let t0 = Instant::now();
                    if sqlx::query(SQL).bind(cid).fetch_one(&pool).await.is_ok() {
                        lat.push(t0.elapsed().as_secs_f64() * 1000.0);
                    }
                }
                lat
            }));
        }
        let mut all: Vec<f64> = Vec::with_capacity(total_ops);
        for h in handles {
            if let Ok(mut v) = h.await {
                all.append(&mut v);
            }
        }
        let wall = started.elapsed().as_secs_f64();
        all.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let mean = if all.is_empty() { 0.0 } else { all.iter().sum::<f64>() / all.len() as f64 };
        let entry = serde_json::json!({
            "concurrency": conc,
            "total_ops": total_ops,
            "wall_s": (wall * 10000.0).round() / 10000.0,
            "throughput_ops_s": ((total_ops as f64 / wall) * 10.0).round() / 10.0,
            "errors": total_ops - all.len(),
            "latency_ms": {
                "n": all.len(),
                "p50": percentile(&all, 0.50),
                "p90": percentile(&all, 0.90),
                "p95": percentile(&all, 0.95),
                "p99": percentile(&all, 0.99),
                "mean": (mean * 1000.0).round() / 1000.0,
                "max": all.last().map(|v| (v * 1000.0).round() / 1000.0).unwrap_or(0.0),
            }
        });
        results.insert(conc.to_string(), entry);
    }

    let out = serde_json::json!({ "variant": "C_rust_sqlx_async_pool", "levels": results });
    println!("{}", serde_json::to_string_pretty(&out)?);
    Ok(())
}
