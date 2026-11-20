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

To actually replace a live database (a real recovery), do it deliberately:

```bash
# 1. Stop the app so nothing writes during the swap
./scripts/nexus.sh stop

# 2. Restore into the real database (you are overwriting it on purpose)
pg_restore --clean --if-exists --no-owner -d "$SUPABASE_DB_URL" backups/<dump>

# 3. Start and confirm
./scripts/nexus.sh start
curl -fsS http://127.0.0.1:8080/readyz
```

`--clean` drops objects before recreating them, so this is destructive to the
current database. Take a fresh backup first if you are unsure.

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
