//! `nexus-api` binary: serve the Rust API on a port.
//!
//! A thin entry point so the parity test (and an operator) can start the Rust
//! API against a database and compare it with the Python server.
//!
//! **This server has no authentication.** It trusts an `X-Nexus-Principal`
//! request header, which any client can set. It is therefore a parity-test /
//! read-only comparison tool, NOT a production API. To make that impossible to
//! forget, it refuses to bind a non-loopback address unless
//! `NEXUS_API_TRUST_PRINCIPAL_HEADER=1` is set *and* the bind address is
//! loopback; a public bind is always refused. Python remains the authenticated
//! production API.
//!
//! Configuration: `NEXUS_DB_URL`, `NEXUS_API_ADDR` (default `127.0.0.1:8099`).

use nexus_api::{router, AppState};
use nexus_store::Store;

fn is_loopback(addr: &str) -> bool {
    let host = addr.rsplit_once(':').map(|(h, _)| h).unwrap_or(addr);
    matches!(host, "127.0.0.1" | "localhost" | "::1" | "[::1]")
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let dsn = std::env::var("NEXUS_DB_URL").map_err(|_| "set NEXUS_DB_URL")?;
    let addr = std::env::var("NEXUS_API_ADDR").unwrap_or_else(|_| "127.0.0.1:8099".into());
    // The principal header is trusted only in an explicit, loopback-only mode.
    // Any other configuration is refused: an unauthenticated API must never be
    // exposed, and a future auth layer must replace this header, not sit beside
    // it.
    let trust = std::env::var("NEXUS_API_TRUST_PRINCIPAL_HEADER")
        .map(|v| v == "1")
        .unwrap_or(false);
    if !trust {
        eprintln!(
            "refusing to start: nexus-api has no authentication. Set \
             NEXUS_API_TRUST_PRINCIPAL_HEADER=1 to run it as a loopback-only \
             parity tool; production uses the Python API."
        );
        std::process::exit(2);
    }
    if !is_loopback(&addr) {
        eprintln!(
            "refusing to bind {addr}: nexus-api trusts the X-Nexus-Principal \
             header and must only be bound to loopback."
        );
        std::process::exit(2);
    }
    let store = Store::connect(&dsn, 8).await?;
    let app = router(AppState { store });
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    println!("nexus-api listening on {addr} (loopback parity tool; NOT production auth)");
    axum::serve(listener, app).await?;
    Ok(())
}
