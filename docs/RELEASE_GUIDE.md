# NEXUS Release Guide

How to build, verify, release, roll back and upgrade NEXUS. It is written so a
release can be reproduced from a clean checkout. **Nothing here is performed
automatically**: the requirement forbids merging to `main`, tagging or deploying
without the owner's explicit authorisation, and this guide does not.

## 1. Before a release

Checklist, in order. Each item names its command or file.

1. **The branch is green.**
   ```bash
   TEST_SUPABASE_DB_URL=postgresql://user:pw@127.0.0.1:5432/db \
     python -m pytest tests/ -q -p no:cacheprovider
   python -m ruff check .
   ```
2. **The Rust core is green and parity holds.**
   ```bash
   cd rust && cargo test && cargo build --release && cd ..
   TEST_SUPABASE_DB_URL=... python -m pytest tests/test_rust_parity.py -q
   ```
3. **The browser suite is green** (Chromium):
   ```bash
   E2E_CHROMIUM_EXECUTABLE=/usr/bin/chromium \
     python -m pytest tests/test_browser_e2e.py -q
   ```
4. **The database restores.** `tests/test_backup_restore.py` is the drill; run
   it, and run a real `pg_dump`/`pg_restore` into a fresh database once.
5. **No secret is in the tree.** `git grep -nE 'sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}'`
   returns nothing; no `.env` is committed.
6. **The docs match.** `docs/ENVIRONMENT.md` lists every variable the code
   reads; the migration list in `DEPLOY.md` matches `migrations/`.

## 2. Build the artifacts

| Artifact | Command | Verified how |
|---|---|---|
| Python service | `pip install -r requirements.txt` | the test suite |
| Rust CLI | `cd rust && cargo build --release` | `cargo test` |
| Debian package | `packaging/debian/build-deb.sh` | `dpkg-deb --info/--contents`, a real `dpkg -i`/`-r` |
| Android APK | `packaging/android/build-apk.sh` | `apksigner verify`, `aapt2 dump badging` |
| Windows `.exe` | `packaging/windows/build-exe.sh` | PE header + `.rsrc` inspection |
| PWA | served from `orchestration/pwa/` | browser E2E |

The APK and the `.exe` are **build-verified, not runtime-verified** here (no
emulator, no Windows host). A release must state which artifacts were run.

## 3. Versioning and tagging

- Semantic version; the tag is `vX.Y.Z`.
- `CHANGELOG.md` is updated in the same commit as the version.
- **Do not** create a tag or push it without the owner's authorisation. The
  release commit is prepared on the integration branch; the merge to `main` and
  the tag are the owner's.

## 4. Deployment (VPS, the initial target)

The requirement names VPS as the initial production target. One shape:

```bash
# 1. The database
createdb nexus
psql "$SUPABASE_DB_URL" -f migrations/0001_init.sql   # then every file, in order
# (the process also applies them on first connect unless AUTO_MIGRATE=false)

# 2. The web service (systemd)
#    packaging/systemd/ has the units; the lab worker is a separate unit
systemctl enable --now nexus-web nexus-lab-worker

# 3. The reverse proxy terminates TLS and forwards to 127.0.0.1:$PORT
#    /healthz is public; everything else needs a session or a bearer.
```

Required environment (full list in `docs/ENVIRONMENT.md`):
`SUPABASE_DB_URL`, `WEB_AUTH_TOKEN`, `AUTH_USERNAME`, `AUTH_PASSWORD_HASH`,
`AUTH_SESSION_SECRET`, `NEXUS_SECRET_KEY`, and the provider/OpenHands/GitHub
keys the deployment uses.

## 5. Post-deploy verification

```bash
curl -fsS https://host/healthz                 # {"status": "ok"}
curl -fsS https://host/readyz                  # the database round-trip
# sign in, then: GET /api/system  -> build.commit is the deployed SHA
#                GET /api/openhands/diagnostics  -> each check's status
```

A deploy is confirmed by `build.commit` (`RENDER_GIT_COMMIT` or the equivalent),
not by the absence of errors.

## 6. Rollback

1. **Application**: redeploy the previous commit/tag. The database schema is
   forward-only (migrations are additive), so an older app runs against a newer
   schema for any change that only *added* a column or table. A change that
   removed or renamed something needs a forward fix, not a rollback.
2. **Database**: restore the last `pg_dump`. `tests/test_backup_restore.py`
   proves a dump restores; keep a recent dump before a schema change.
3. **A stuck run**: cancel it (`POST .../runs/{id}/cancel`); nothing is deleted,
   the transcript is the record. Resume after a fix starts new conversations.

## 7. Upgrade

1. Back up (`pg_dump`).
2. Deploy the new commit; migrations apply on first connect (idempotent).
3. Watch `/readyz` and `/api/openhands/diagnostics`.
4. If `NEXUS_LAB_INLINE` changed, restart the worker unit too.

An upgrade never rewrites history: `apply_migrations` refuses an **edited**
already-applied file (`MigrationError`), so a release cannot silently change the
schema under its own name.

## 8. What a release must state

- The exact commit SHA and the branch it was cut from.
- The test counts (Python, Rust, parity, browser) with what was skipped and why.
- Which artifacts were built and which were **run**.
- The external blockers (a second OpenHands key, a GitHub provider token, an
  emulator/Windows host) and what each one prevented.
- What was not verified live.

This is the shape of `NEXUS_2_0_FINAL_ACCEPTANCE_REPORT.md`; a release writes the
same thing for its own commit.
