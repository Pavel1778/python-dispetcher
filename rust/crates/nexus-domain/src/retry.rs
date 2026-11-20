//! How long to wait before trying again, and how many times before waiting.
//!
//! This mirrors `orchestration/nexus_retry.py`, the whole policy as pure
//! functions. The numbers live in one place and are testable without a clock:
//!
//! * inside one **work cycle** a task gets a bounded number of attempts;
//! * when a cycle's attempts are used up the task **waits** and starts the next
//!   cycle with its attempts restored; the wait doubles from cycle to cycle,
//!   up to a ceiling;
//! * a start the service **refuses** (HTTP 429) is not an attempt; the wait is
//!   what the service asked for (`Retry-After`), else it grows with the number
//!   of refusals in a row;
//! * waits carry a small, deterministic spread (up to a tenth, derived from the
//!   task's id and cycle) so many tasks that failed together do not all come
//!   back in the same second - and so a test can still say exactly when.
//!
//! The environment is read in [`RetryPolicy::from_env`]; the arithmetic is pure
//! and takes its bounds as arguments, so a test never touches the process
//! environment.

use sha2::{Digest, Sha256};

pub const DEFAULT_BASE_SECONDS: u64 = 60;
pub const DEFAULT_MAX_SECONDS: u64 = 3600;
pub const REFUSED_BASE_SECONDS: u64 = 60;
pub const REFUSED_DAILY_SECONDS: u64 = 900;
pub const REFUSED_MIN_SECONDS: u64 = 30;
pub const REFUSED_MAX_SECONDS: u64 = 3600;

/// The resolved policy: the three numbers the module reads from the
/// environment, with the same defaults and the same clamps as Python.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RetryPolicy {
    /// The wait after the first exhausted cycle, in seconds.
    pub base_seconds: u64,
    /// The longest wait between cycles, in seconds (never below `base_seconds`).
    pub max_seconds: u64,
    /// The most cycles a task may go through, or 0 for no ceiling.
    pub max_cycles: u64,
}

impl Default for RetryPolicy {
    fn default() -> Self {
        RetryPolicy {
            base_seconds: DEFAULT_BASE_SECONDS,
            max_seconds: DEFAULT_MAX_SECONDS,
            max_cycles: 0,
        }
    }
}

impl RetryPolicy {
    /// Read the policy from the environment, exactly as `nexus_retry` does.
    pub fn from_env() -> Self {
        let base = int_env("NEXUS_RETRY_BASE_SECONDS", DEFAULT_BASE_SECONDS, 1, 86_400);
        let ceiling = int_env(
            "NEXUS_RETRY_MAX_SECONDS",
            DEFAULT_MAX_SECONDS,
            1,
            7 * 86_400,
        );
        RetryPolicy {
            base_seconds: base,
            max_seconds: ceiling.max(base),
            max_cycles: int_env("NEXUS_RETRY_MAX_CYCLES", 0, 0, 1_000_000),
        }
    }

    /// Seconds to wait before cycle number `cycle` begins (the first wait is
    /// before cycle 1): `base * 2 ** (cycle - 1)`, capped, plus the spread.
    pub fn cycle_wait(&self, cycle: i64, seed: &str) -> f64 {
        cycle_wait(cycle, seed, self.base_seconds, self.max_seconds)
    }

    /// Whether a task that has gone through `cycle` cycles may start another.
    /// A ceiling of 0 means no ceiling.
    pub fn cycles_left(&self, cycle: u64) -> bool {
        self.max_cycles == 0 || cycle < self.max_cycles
    }
}

/// `base * 2 ** (cycle - 1)`, capped at `ceiling`, plus the deterministic
/// spread.
pub fn cycle_wait(cycle: i64, seed: &str, base: u64, ceiling: u64) -> f64 {
    let steps = (cycle - 1).max(0);
    let exponent = steps.min(40) as u32;
    let raw = (base as f64) * (2f64.powi(exponent as i32));
    let wait = raw.min(ceiling as f64);
    if seed.is_empty() {
        wait.min(ceiling as f64)
    } else {
        let spread = spread(&format!("{seed}:{cycle}"));
        (wait * (1.0 + spread)).min(ceiling as f64)
    }
}

/// Seconds to wait after the service refused a start (HTTP 429).
///
/// `retry_after` is what the service asked for, kept between half a minute and
/// an hour. Otherwise the wait doubles with each refusal in a row: from a
/// minute, or from a quarter of an hour where the service named a daily limit,
/// up to an hour.
pub fn refused_wait(retry_after: Option<f64>, daily: bool, refusals: i64) -> f64 {
    if let Some(after) = retry_after {
        return after.clamp(REFUSED_MIN_SECONDS as f64, REFUSED_MAX_SECONDS as f64);
    }
    let first = if daily {
        REFUSED_DAILY_SECONDS
    } else {
        REFUSED_BASE_SECONDS
    };
    let exponent = refusals.clamp(0, 20) as i32;
    ((first as f64) * 2f64.powi(exponent)).min(REFUSED_MAX_SECONDS as f64)
}

/// A stable number in `[0, 0.1)` for `seed`, from the first four bytes of its
/// SHA-256 as a big-endian fraction. Mirrors `nexus_retry._spread`.
pub fn spread(seed: &str) -> f64 {
    let digest = Sha256::digest(seed.as_bytes());
    let n = u32::from_be_bytes([digest[0], digest[1], digest[2], digest[3]]);
    (n as f64 / 2f64.powi(32)) * 0.1
}

/// `seconds` in the words a status line uses (`nexus_retry.span`).
pub fn span(seconds: f64) -> String {
    if seconds >= 5400.0 {
        let h = seconds / 3600.0;
        format!("{h:.1} h").replace(".0 h", " h")
    } else if seconds >= 90.0 {
        format!("{} min", (seconds / 60.0).round() as i64)
    } else {
        format!("{} s", seconds.round() as i64)
    }
}

/// `int(os.environ.get(name))` with a default and a clamp; a junk value falls
/// back to the default, exactly as `_int_env` does.
fn int_env(name: &str, default: u64, least: u64, most: u64) -> u64 {
    let raw = std::env::var(name).unwrap_or_default();
    let raw = raw.trim();
    let value = if raw.is_empty() {
        default
    } else {
        raw.parse::<u64>().unwrap_or(default)
    };
    value.clamp(least, most)
}
