# Migration integrity manifest

`schema_migrations` records *what ran*, not *what should have run*. That is enough
to skip a file twice, and not enough to notice that the file changed, that a
number was reused, or that a step in the middle never happened.

`orchestration/migration_runner.py` reads this file as the reviewed statement of
what the tree is supposed to contain, and refuses to migrate a database that
disagrees with it. The checksums here are the *reviewed* ones: changing a
migration without updating its entry here fails the run (`manifest_mismatch`,
before any SQL) with a message naming the file, instead of silently re-executing
a different body under an old name.

## What each key means

| key | meaning |
| --- | --- |
| `files` | Every migration that must exist, in name order. The value is the SHA-256 of the file's UTF-8 text. An unlisted `*.sql` file is rejected (an unreviewed migration), a listed file that is missing is rejected (a lost step), and a file whose text does not hash to its value is rejected (an unreviewed body) — on an empty database as well as on one that already applied the old body. |
| `columns` | `"file": [["table", "column"], ...]` — columns a file *adds*. After the file runs, the runner requires each one to exist. This is how "the SQL succeeded but did not do what it says" is caught. |
| `table_columns` | `"file": [["table", [column, ...]], ...]` — tables a file creates, with the columns it declares. After the file runs, the table and all of those columns must exist. |
| `indexes` | `"file": ["index_name", ...]` — indexes a file creates. Verified via `pg_indexes`, because task-level workspace exclusivity rests on `uq_tasks_workspace_active` (migration `0015`) and a run that silently skipped it would leave a database that looks healthy and is not. |
| `rls` | `"file": ["table", ...]` — tables whose row level security a file enables. Verified via `pg_class.relrowsecurity`, because `0016` exists solely to stop a sandbox session key being readable through PostgREST and has no table or column of its own to check. |

## Why the baseline is declared rather than probed

The migrations that predate this file are already applied in live
databases. Probing them at run time ("create the table, then see whether the
column was there before") cannot distinguish a fresh database from a legacy one
that is missing a step — the distinction this manifest exists to make. The
baseline was produced by parsing the checked-in SQL, not by trusting the live
schema, and it is reviewed as a diff like any other file.

## Adding a migration

1. Add `migrations/00NN_name.sql`, uniquely numbered, after the highest number.
2. Add its path to `files` with the SHA-256 of the file.
3. Add what it creates to `columns` / `table_columns` / `indexes` / `rls`, so the
   runner can verify the effect. The runner itself applies and records a file
   with no declared effect without verifying anything, which is why
   `tests/test_migration_runner.py::test_every_real_migration_declares_a_verifiable_effect`
   fails the build for one.
4. Apply it. `apply_migrations` will refuse if the recorded history in the target
   database does not match this manifest (see
   `orchestration/migration_runner.py`, `MigrationError`).

Do **not** edit or renumber a migration that has already been applied anywhere:
that is a new migration. The runner refuses either way rather than re-running the
edited file: `manifest_mismatch` if only the file was changed, and
`checksum_changed` — on every database that applied the old body — if the
manifest was updated to match.
