# Backup and restore

The database is the product's memory: projects, runs, tasks, evidence, chat and
settings all live in PostgreSQL. A backup that has never been restored is not a
backup. This document is the policy and the drill.

## What to back up

| Thing | Where | How |
|---|---|---|
| The database | PostgreSQL | `pg_dump --format=custom` (the script does this) |
| Stored secrets | in the database, encrypted with `NEXUS_SECRET_KEY` | backed up with the database **only if** you also keep `NEXUS_SECRET_KEY` safe |
| `NEXUS_SECRET_KEY` | your environment / secret store | keep a copy **separately** from the dump; without it, stored secrets cannot be decrypted |
| The `.env` | the host | back up out of band; it holds `AUTH_*`, `WEB_AUTH_TOKEN`, provider keys |
| Uploaded files / workspaces | in the database (bytes are rows) | covered by the database dump |

Git history lives on GitHub; a task's work is a commit on a branch, so the
repository is not in the database backup.

## Backing up

```bash
./scripts/nexus.sh backup
```

This writes `backups/nexus-<UTC timestamp>.dump` (custom format) and a matching
`.sha256`. It keeps the last 14 and rotates older **NEXUS** dumps only — it never
touches anything else in that directory.

Schedule it with a timer:

```bash
# /etc/systemd/system/nexus-backup.service
#   [Service]
#   Type=oneshot
#   WorkingDirectory=/opt/nexus
#   ExecStart=/opt/nexus/scripts/nexus.sh backup
# /etc/systemd/system/nexus-backup.timer
#   [Timer]
#   OnCalendar=daily
#   Persistent=true
sudo systemctl enable --now nexus-backup.timer
```

Copy the dumps off the machine (to another disk, object storage, or a second
host). A backup that lives only on the machine it protects is not a backup.

## Restoring

The script **never** restores over the live database. It restores into a fresh
scratch database, checks the tables load, then drops the scratch database:

```bash
./scripts/nexus.sh restore backups/nexus-20261119T000000Z.dump
```

`restore` verifies the selected file against its adjacent SHA-256 when present;
for an external dump without a sidecar it explicitly warns. A checksum is not a
signature: keep a trusted copy of the digest separately. The script checks
`current_database()` against the database it just created **before** it runs
`pg_restore`. Use a `pg_dump` at least as new as the server, and restore only
trusted dumps (restoring SQL is privileged).

For a real recovery **do not clean or overwrite the live database**. Restore to
a new database/instance with the required roles and grants, without starting
any workers or contacting providers. Keep the original read-only. Check the
full migration filenames and checksums, project membership, task/lease state,
execution history, file bytes and evidence signatures with the separately
backed-up trusted public keys. Verify encrypted accounts with the separately
held encryption key, without printing their values. `pg_dump` of one database
does not back up cluster roles, host files or Git repositories.

Switch endpoints only after owner acceptance, a write freeze, reconciliation of
remote conversations/branch locks, and a final consistent backup. Never run
both sets of workers during a switch. Before new writes, rollback can select
the untouched original; after writes, first freeze and reconcile the delta.
Rolling back a binary is not a reason to discard committed database history.

## The automated drill

`tests/test_backup_restore.py` runs a real `pg_dump`/`pg_restore` into a fresh
database and asserts the schema guard is satisfied and the data survived. It is
the automated proof that a dump restores. `tests/test_self_hosting.py` proves the
`nexus.sh backup`/`restore` path leaves the live database untouched.

## Retention and integrity

- Every dump carries a SHA-256 next to it; `sha256sum -c <dump>.sha256` before
  you rely on a restore.
- Keep at least a week of daily dumps and one monthly.
- Test a restore monthly. A restore that fails when you need it is the failure
  that costs the most.
- Keep `NEXUS_SECRET_KEY` and the `.env` in a separate backup location; a dump
  without the key restores rows you cannot decrypt.
