# CI / RELEASE READINESS

**Date:** 2026-11-20 · **Status: IMPROVED — one real gap closed, rest verified**

## 1. What the workflow does (verified by reading `.github/workflows/tests.yml`)

Two jobs:

### `rust` job

| Step | Command |
| --- | --- |
| Install Rust | `rustup toolchain install stable --profile minimal` |
| Build | `cargo build --workspace --release --locked` + `cargo build -p nexus-storage --example claim_probe --release --locked` |
| **fmt and clippy (NEW)** | `cargo fmt --all --check` + `cargo clippy --workspace --all-targets -- -D warnings` |
| Tests | `cargo test --workspace --locked` |
| Python/Rust parity | exports `NEXUS_CLAIM_PROBE`, asserts both binaries are executable, runs the parity and claim suites |

### `test` job

- PostgreSQL **17** service container;
- `TEST_SUPABASE_DB_URL` pointed at it;
- Chromium downloaded and probed once (`tools/ci_browser_probe.py`);
- pytest with annotations for the summary line, failures and skip reasons.

Postgres 17 matches the version the audit used. `--locked` is used for the
build and the tests, so a `Cargo.lock` drift fails CI rather than being papered
over.

## 2. The gap this audit found, and closed

**CI never ran `cargo fmt --check` or `cargo clippy`.** That is why two real
defects were sitting on `main` while every workflow was green:

| Defect on `main` | Would CI have caught it? |
| --- | --- |
| `cargo fmt --all --check` failed (`nexus-cli/src/main.rs`) | **no** — the command was never run |
| `clippy::manual_clamp` in `nexus-domain/src/retry.rs:112` | **no** |
| `clippy::manual_range_contains` in `nexus-domain/tests/domain.rs:166` | **no** |

Both are now fixed in the code, and the **`Rust fmt and clippy` step** makes a
recurrence fail CI. The step is added with `-D warnings`, so a *new* lint fails
the build rather than scrolling past as a warning.

## 3. What a green run on `main` still does not prove

The brief is explicit that "a successful workflow on `main` does not prove a
candidate branch is green". Recorded here as a standing rule:

- CI status is quoted **per SHA**, never per repository;
- a local pass is not a CI pass (the audit saw 2070 local vs 2111 CI on the same
  commit, because CI has Chromium and the claim probe);
- the Rust job runs on `ubuntu-latest`; Windows and macOS are **not** tested, so
  cross-platform build behaviour is **NOT VERIFIED**.

## 4. Release readiness

| Item | Status |
| --- | --- |
| Python tests in CI | **yes** (with annotations) |
| Rust build `--locked` | **yes** |
| Rust fmt + clippy `-D warnings` | **added by this audit** |
| Rust tests `--locked` | **yes** |
| PostgreSQL 17 in CI | **yes** |
| Parity + claim suites with the probe | **yes** |
| Migration tests | **yes** (`tests/test_migration*.py`, `test_schema_bootstrap.py`) |
| Security checks | **partial** — `tests/test_security_checks.py`, `test_route_auth.py`; no dependency audit (`cargo audit`) |
| Cross-platform | **NOT VERIFIED** |
| Release checklist / rollback | documented in `DEPLOY.md`; **not exercised in this audit** |
| Feature flags / runtime selection | `OPENHANDS_TRANSPORT`; Python remains authoritative |

## 5. Recommended additions (not done here)

1. **`cargo audit`** (or `cargo deny`) in the Rust job — the audit did not run a
   dependency vulnerability scan, so that is an open item, not a passed one.
2. **A CI job for the new concurrency suite** at widths 16 and 64 (it takes
   ~10 s; the 128 case stays opt-in).
3. **`cargo fmt`/`clippy` on the `test` job too**, so a branch that only touches
   Python still validates the Rust it shares.

These are safe, small additions; none was applied in this session beyond the
fmt/clippy step, to keep the change reviewable.
