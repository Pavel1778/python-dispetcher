# NEXUS 3.0 — Backup and recovery verification plan

The requirement forbids deleting user data, history, projects, repositories or
evidence, and forbids destructive migrations. This is how that is guaranteed and
*proved*, not merely promised.

## 1. Before any change: a restore point

The repository already ships safe restore points (`orchestration/restore_point.py`,
`nexus restore-point create`). A restore point is a **git bundle of every ref**
plus a JSON record (HEAD, branch, dirty state, SHA-256) under `.restore-points/`
(gitignored, never pushed). It is non-destructive: it only reads the repo and
writes new files.

```bash
python -m orchestration.cli restore-point create --notes "pre NEXUS 3.0"
python -m orchestration.cli restore-point list
python -m orchestration.cli restore-point verify <id>     # re-checks SHA-256 + bundle
```

`restore` clones the bundle into a scratch directory to *prove* it comes back;
it never resets the working tree.

## 2. Before any migration: a database dump

The database is the single source of truth (50 tables). Every schema change in
NEXUS 3.0 is **additive** (a new column with a default, a new table, a new
index) and goes in a **new** migration file; no existing table or column is
renamed or dropped. Before applying, a dump is taken:

```bash
pg_dump "$SUPABASE_DB_URL" --format=custom --file=backup-$(date +%F).dump
```

Restore is proven by loading into a **scratch** database and running
`verify_schema()` against it — the existing drill `tests/test_backup_restore.py`
does exactly this with a real `pg_dump`/`pg_restore` into a fresh database, and
it must stay green.

## 3. The migration runner refuses an edited, applied file

`PostgresStore.apply_migrations` records each file it applies. It **raises
`MigrationError`** if an already-applied file's body has changed — the history
cannot claim one thing while the schema is another. A *new* file still applies.
This is the guarantee that a NEXUS 3.0 migration is always a new file, never an
edit of `0001`–`0042`.

## 4. Recovery of the running system

- **Store reconnect:** `PostgresStore` replaces a dropped connection on
  evidence (a `poll` finds pending input, then a `SELECT 1`), raises
  `StoreUnavailable` (→ HTTP 503, never the driver's text) with a 2 s back-off,
  and never retries a statement that may already have run.
- **Worker survives a database blip:** `LabWorker.run_forever` catches
  `StoreUnavailable`, logs and retries on the next pass; registration is retried
  until it sticks. (`tests/test_planner_worker.py::test_the_worker_loop_outlives_a_transient_database_error`)
- **Lease + fencing recovery:** an expired lease is reclaimed with a **fresh**
  `lease_token`; a stale worker's fenced write matches no row. Proven by
  `tests/test_race_protection.py` and the Rust `a_stale_token_cannot_bind_a_conversation` case.
- **Start hand-over:** an accepted start is on the row (`pending_start`), so a
  worker replaced mid-start is finished by the next pass, not lost.

## 5. What NEXUS 3.0 adds to this plan

- The Rust claim crate is held to the **same** recovery rules by the parity
  tests: `reclaim_expired` moves the token forward, `mark_running` is fenced.
  A recovery drill for the Rust path is `tests/test_rust_claim_parity.py` plus
  `rust/crates/nexus-store/tests/claim_parity.rs`.
- No migration in NEXUS 3.0 drops or renames. A column the store reads is added
  to `REQUIRED_COLUMNS` in `cloud_store.py`, and the schema guard checks it.

## 6. Verification checklist (run before declaring any phase done)

- [ ] `nexus restore-point create` taken and `verify` passes.
- [ ] `pg_dump` taken; `tests/test_backup_restore.py` green (real restore).
- [ ] `pytest tests/` green (no test removed, no coverage lost).
- [ ] `ruff check .` clean.
- [ ] Any new migration is a new file; `apply_migrations` has not re-run an
      edited one.
- [ ] `REQUIRED_COLUMNS` names any column the store newly reads.
