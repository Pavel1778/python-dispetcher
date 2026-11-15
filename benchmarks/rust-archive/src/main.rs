// Rust archive inspect benchmark (variant C of the archive pilot).
//
// Isolated research program: it is NOT the NEXUS runtime and shares nothing
// with it. It does the CPU-bound part of what archive_safety.open_zip does -
// read the zip central directory and decompress every member with a size cap -
// on the SAME zips the Python benchmark generates, and prints the elapsed ms as
// JSON so the two can be compared. It does NOT do the safety validation (that
// is a parity concern, not a speed one) and it does NOT touch PostgreSQL.
//
// The point: measure whether a Rust unpacker is materially faster than the
// Python one (which was ~15 ms for 1000 members). If it is not, Rust is not
// justified for this component.

use std::io::Read;
use std::time::Instant;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let path = std::env::args().nth(1).ok_or("usage: archive_bench <zip> [repeat]")?;
    let repeat: usize = std::env::args().nth(2).and_then(|s| s.parse().ok()).unwrap_or(20);
    let data = std::fs::read(&path)?;
    const MAX_ENTRY: u64 = 256 * 1024 * 1024;

    let mut times = Vec::with_capacity(repeat);
    let mut members = 0usize;
    let mut total_bytes: u64 = 0;
    for _ in 0..repeat {
        let t0 = Instant::now();
        let cursor = std::io::Cursor::new(&data);
        let mut zip = zip::ZipArchive::new(cursor)?;
        members = zip.len();
        total_bytes = 0;
        for i in 0..zip.len() {
            let mut f = zip.by_index(i)?;
            let mut buf = Vec::new();
            // Read with a running cap, exactly like the Python code.
            f.by_ref().take(MAX_ENTRY + 1).read_to_end(&mut buf)?;
            total_bytes += buf.len() as u64;
        }
        times.push(t0.elapsed().as_secs_f64() * 1000.0);
    }
    times.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let p50 = times[times.len() / 2];
    let out = serde_json::json!({
        "variant": "C_rust_zip_unpack",
        "members": members,
        "total_bytes": total_bytes,
        "repeat": repeat,
        "p50_ms": (p50 * 1000.0).round() / 1000.0,
        "min_ms": (times[0] * 1000.0).round() / 1000.0,
        "max_ms": (times[times.len() - 1] * 1000.0).round() / 1000.0,
    });
    println!("{}", serde_json::to_string_pretty(&out)?);
    Ok(())
}
