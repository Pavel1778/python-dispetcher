# Running the whole thing on one virtual machine

One VM, one command, everything on it: PostgreSQL 17, the NEXUS web
control plane and the autonomous lab worker. No external database service,
no separate host, no orchestration platform.

This uses `docker-compose.yml` (already in the repository) and the root
`Dockerfile`. It is the simplest topology that exists: **run exactly one app
instance** — NEXUS advances autonomous runs on an internal thread, so two
replicas would race.

## What you need

- A VM with 2 vCPU / 4 GB RAM (the free tier of most clouds works; NEXUS is
  I/O-bound — the database and the OpenHands conversations dominate the
  runtime, not your CPU).
- Linux with Docker Engine + the Compose plugin
  (`sudo apt install docker.io docker-compose-v2` on Debian/Ubuntu, or the
  Docker convenience script).
- A public DNS name + HTTPS if the box is reachable from the internet —
  see [Hardening](#hardening). For a LAN/VPN-only box, plain HTTP is fine.

## Quickstart

```bash
git clone https://github.com/Pavel1778/python-dispetcher.git && cd python-dispetcher
cp .env.example .env
```

Fill in `.env` — only these are required to boot:

| Variable | How to get it |
|---|---|
| `AUTH_USERNAME` | already `owner` in `.env.example` — change it if you want |
| `AUTH_PASSWORD_HASH` | `python -m orchestration.web_auth hash` (prints `pbkdf2_sha256$…`) |
| `AUTH_SESSION_SECRET` | `python -m orchestration.web_auth secret` |
| `WEB_AUTH_TOKEN` | `python -m orchestration.web_auth secret` (any long random string works) |
| `SUPABASE_DB_URL` | leave as-is — compose overrides it to the bundled PostgreSQL |
| `OPENHANDS_API_KEY` | your OpenHands Cloud key |
| `GITHUB_TOKEN` | optional; read access is enough for repo discovery |

Compose interpolates `$` in `env_file` values: the hash contains `$`, so
double each of them (`$$`) inside `.env` (details in the compose file header).

```bash
docker compose up -d --build
docker compose ps          # db healthy, app healthy, worker running
curl http://localhost:8000/readyz   # {"status":"ready","database":true}
```

Open `http://<vm-ip>:8000` and log in. The schema applies itself on first
connect (idempotent, recorded in `schema_migrations`). The bundled database
is named `nexus`, lives on the named volume `nexus-db`, and is **not** exposed
to the host.

## Everyday operations

```bash
docker compose logs -f app        # what the service is doing
docker compose restart app        # pick up config changes
docker compose up -d --build      # deploy a new revision (rebuild from source)
docker compose down               # stop everything (data survives)
docker compose down -v            # stop and DELETE the database volume — deliberate
```

Full operations playbook (resource budget, tested restore rehearsal, Caddy/HTTPS,
firewall, RPO/RTO): [docs/deployment/SINGLE_VM_OPERATIONS.md](deployment/SINGLE_VM_OPERATIONS.md).

### Backups

The database is one volume. Nightly logical dump from the db container:

```bash
docker compose exec db pg_dump -U nexus nexus > backup-$(date +%F).sql
```

Restore: `docker compose exec -T db psql -U nexus nexus < backup-YYYY-MM-DD.sql`
(stop the app first: `docker compose stop app worker`). For the full
backup/restore drill, see `docs/OPERATIONS.md` and `orchestration/restore_point.py`.

### Resource hygiene

Per-task history grows forever (by design — it is the audit trail). Two
things keep the box fast over months of use:

- migration `0041` (applied automatically) indexes the claim path;
- the archive/purge retention tools (`nexus` CLI, "restore points", and the
  project purge paths) remove what you no longer need. See `docs/OPERATIONS.md`.

## Hardening

- **Firewall:** only 22 and 8000 (or 443) open; the database port is never
  published by compose.
- **HTTPS:** put the app behind any reverse proxy (Caddy, nginx, Traefik) or
  a tunnel; the session cookie is `Secure` automatically on HTTPS and
  `HttpOnly` + `SameSite=Strict` always. Without HTTPS, the cookie is not
  marked `Secure` (plain-HTTP limitation at the HTTP layer, by design, so
  LAN setups work) — do not expose a plain-HTTP instance to the internet.
- **Secrets:** they are in `.env` (git-ignored), never in the image, never
  logged, never returned by the API. `docker compose config` output leaks
  them — do not paste it anywhere.
- **SSH:** key-only, and the repo checked out read-only for the `app` user if
  you want belt and braces.

## Honest limits

- One instance, one database, one VM — no HA, no failover. If the VM dies,
  the service is down until it (or a restore) comes back.
- `orjson`-style native accelerators are not used; the hot path is SQL +
  I/O bound (see `docs/PERFORMANCE.md`).
- Android/Windows builds and the browser-E2E suite are not part of this
  topology — it is the server, not the packaging pipeline.
- Free cloud VMs sleep or are preempted; autonomous runs make no progress
  while the box is off. Use a small paid VM for unattended runs.
