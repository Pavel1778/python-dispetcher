# Single-VM Operations — NEXUS (verified 2026-11-19)

Audience: the one person who runs the one box. Strategy: PostgreSQL 17 +
application + lab worker on a single VM via `docker compose`; no Kubernetes, no
managed database requirement, no second node. Companion docs: quickstart in
`docs/SINGLE_VM.md`, network/proxy notes in `DEPLOY.md`, security findings in
`docs/security/SECURITY_AUDIT.md`, performance numbers in
`docs/performance/BASELINE.md`.

## 1. Configuration review (what compose actually does today)

Verified by reading and by CI/runtime evidence, not by re-opinion:

| Aspect | State | Evidence |
|---|---|---|
| Healthchecks | `db`: `pg_isready` (5s/10s retries); `app`: HTTP `/readyz` 10s interval, 20s start period; `worker`: none (stateless loop; restart policy covers death) | `docker-compose.yml`; readiness verified live (200) |
| Startup dependency | `app`/`worker` wait for `db` `service_healthy` | compose |
| Restart policy | `restart: unless-stopped` on `db`, `app`, `worker` | compose (added this increment) |
| Persistent volume | named volume `nexus-db` → `/var/lib/postgresql/data`; not published to the host | compose |
| Ports | app `8000:8000` only; PostgreSQL reachable only on the compose network | compose |
| Environment | `SUPABASE_DB_URL` overridden to the bundled DB; secrets via `.env` (git-ignored); dead `HOST`/`PORT` removed (the app takes flags, not env) | compose diff in this branch |
| Connection limits | psycopg_pool (`min_size=1` is not used; default sizing) with per-connection health check; PG container unconfigured (PG defaults) | `cloud_store.py` |
| Graceful shutdown | SIGTERM → `KeyboardInterrupt` path → `server.stop()` finishes the in-hand pass (LAB_STOP_GRACE 20 s); `docker stop` default timeout (10 s) is tighter — raise with `stop_grace_period: 30s` if runs are mid-flight (recommended, see §6) | `web_server.py` main() |
| Logging | app logs to stdout (JSON-less, scrubbed); `docker compose logs` | compose, `openhands_diagnostics` scrub filter |
| Backups | §5 | drilled |

Local execution limits: Docker is **not available in this sandbox**, so this
topology was validated as YAML + by CI/Render parity + by a live non-container
run of the exact image CMD (`python -m orchestration.web_server`) against a
fresh database. Container startup itself is CI-adjacent, not locally executed.

## 2. Resource budget (measured on the dev sandbox, I/O-bound profile)

| Resource | Idle | Notes |
|---|---|---|
| App VmRSS | **~52 MB** (9 threads) | measured 2026-11-19 on empty DB |
| Lab worker VmRSS (separate process) | ~50 MB (same interpreter profile) | not separately measured this pass; same codebase, same ballpark — flagged as an estimate |
| PostgreSQL container | ~30–80 MB baseline + `shared_buffers` (default 128 MB) | container default; no custom `postgresql.conf` — fine at this scale |
| Per-request overhead | small (handlers are sync; ThreadingHTTPServer) | — |
| Suite peak | not measured per-container; CI runner 4 GB class completes | — |

**Minimal configuration (LAN / small team):** 2 vCPU · 4 GB RAM · 40 GB SSD.
Expected steady state: < 1 GB used, CPU single-digit %. Suitable for occasional
autonomous runs (OpenHands does the heavy lifting remotely).

**Recommended (daily autonomous work):** 4 vCPU · 8 GB RAM · 80 GB SSD,
separate `pg_dump` cron + off-box copy. PostgreSQL `shared_buffers=512MB` only
if you measure pressure; do not tune blind.

Do not add containers "for architecture's sake" (no sidecar proxies, no local
cache tier). Every process here earns its keep.

## 3. HTTPS and reverse proxy (required before internet exposure)

Caddy is the least-ceremony option (automatic Let's Encrypt):

```
nexus.example.com {
    reverse_proxy 127.0.0.1:8000
    # Optional belt: only allow the office IP range
    # @office remote_ip 203.0.113.0/24
    # handle @office { reverse_proxy ... }
}
```

Set on the app (compose `environment:` or `.env`): `WEB_COOKIE_SECURE=1`
(session cookie marked `Secure` even if the proxy hides the proto), and
`TRUSTED_PROXY_HOPS=1` (default) — raise only for real additional proxies.
Without HTTPS the cookie is not `Secure` (plain-HTTP design for LAN use);
**never expose port 8000 plain-HTTP to the internet.**

Cookie policy today: `HttpOnly; SameSite=Strict` always; `Secure` with HTTPS or
the flag above.

## 4. Firewall and host hardening

- Allow inbound: 22 (your IP/VPN), 80+443 (Caddy). Nothing else.
- Key-only SSH, `fail2ban` optional, unattended-security-upgrades on.
- `.env` mode `600`, owner-only; never in images, never pasted into chat/logs
  (`docker compose config` prints secrets — treat its output as a secret too).
- Automatic updates: pin the deployment by git SHA/tag; rebuilds are explicit
  (`docker compose up -d --build`), never silent.

## 5. Backup and restore — tested procedure

**What was actually executed (2026-11-19, PostgreSQL 16.2 local):**

1. Populated database `nexus_arena` → `pg_dump -Fc` (client 16.2) → file.
2. `createdb nexus_restore_drill` → `pg_restore --no-owner --no-privileges`
   → exit 0.
3. Verified: **51 public tables**, **42 rows in `schema_migrations`** (0041
   included), indexes `idx_nexus_run_tasks_repo_branch` /
   `idx_nexus_executor_stops_repo_branch` present.
4. Drill DB **dropped**; dump file deleted. Also re-verified on a fresh
   cluster: all 42 migrations apply cleanly from zero; 0041 applies twice
   idempotently (NOTICE only).

The CI `tests/test_backup_restore.py` re-proves this on every CI run against a
*populated* scratch database (skipped only when client tools or a DSN are
absent — present in CI).

**Nightly backup (on the VM):**

```bash
docker compose exec db pg_dump -U nexus -Fc nexus > /var/backups/nexus/nexus-$(date +%F).dump
# prune older than 14 days
find /var/backups/nexus -name 'nexus-*.dump' -mtime +14 -delete
```

**Restore rehearsal (do this monthly; an unrehearsed restore is not a backup):**

```bash
docker compose stop app worker
docker compose exec -T db dropdb -U nexus --if-exists nexus_restore_test
docker compose exec -T db createdb -U nexus nexus_restore_test
docker compose exec -T db pg_restore -U nexus --no-owner --no-privileges \
    -d nexus_restore_test < /var/backups/nexus/nexus-YYYY-MM-DD.dump
docker compose exec db psql -U nexus -d nexus_restore_test -c '\dt | wc -l'   # expect 51
docker compose exec db psql -U nexus -d nexus_restore_test \
    -c 'select count(*) from schema_migrations'                              # expect 42
docker compose exec -T db dropdb -U nexus nexus_restore_test
docker compose start app worker
```

For a *real* disaster (VM disk gone): provision a new VM, install Docker,
`git clone` at the recorded SHA, restore the dump into the named volume,
`docker compose up -d --build`, verify `/readyz`, log in, spot-check a project.
RPO = your backup cadence (24 h with the nightly dump). RTO = hours, manual.
That is the honest price of a single VM — see §7.

## 6. Compose hardening deltas (recommended, not required)

```yaml
  app:
    stop_grace_period: 30s        # let an in-hand run pass finish (LAB_STOP_GRACE=20)
    deploy:
      resources:
        limits: { cpus: "2.0", memory: 1024M }
  worker:
    stop_grace_period: 30s
    deploy:
      resources:
        limits: { cpus: "2.0", memory: 1024M }
  db:
    deploy:
      resources:
        limits: { cpus: "2.0", memory: 2048M }
    shm_size: 256m                # if you ever raise shared_buffers
```

These are commented guidance; the checked-in compose intentionally stays
minimal. `restart: unless-stopped` is already in place, which is what recovers
the stack after a reboot.

## 7. Honest limits (single VM)

- **No HA.** VM dead = service down. No failover, no read replica, no PITR
  (logical dumps only). If this is unacceptable, the topology — not the app —
  must change (managed PG + external backup target), and that is a separate
  owner decision.
- **No zero-downtime deploys.** `up -d --build` replaces the app; requests in
  flight are retried by the browser/proxy.
- **PG 17 on the VM vs PG 16.2 evidence locally.** CI proves 17 on every run;
  the drill above ran on 16.2 (client+server). Accept or re-drill on 17 — it
  is one command.
- **Backup cadence is your RPO.** Off-box copy is *required* for disk-loss
  recovery (the named volume dies with the VM's disk).
- **Monitoring** is pull-based: an uptime check on `/readyz` + log review.
  There is no built-in alerting; wire one (e.g., a free uptime monitor hitting
  `/readyz`) — do not skip this.
