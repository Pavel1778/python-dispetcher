# Self-hosting NEXUS on Debian 13

Run NEXUS on one machine you own. No Render, no external database, no
Kubernetes. This is the supported production topology for a small team: one
PostgreSQL 17, one NEXUS web process, one lab worker.

## Target machine

- Debian 13, x86-64. A 2 vCPU / 4 GB box is comfortable; NEXUS is I/O-bound
  (the database and the OpenHands conversations dominate), not CPU-bound.
- PostgreSQL 17 installed locally (`sudo apt install postgresql`).
- Python 3.11+ (`python3 --version`), `git`, `psql`, `curl`.
- Optional: `cloudflared` for a public URL (see `docs/CLOUDFLARE_TUNNEL.md`).

## One-time setup

```bash
# 1. Get the code
git clone https://github.com/Pavel1778/python-dispetcher.git
cd python-dispetcher

# 2. Python dependencies
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 3. A database
sudo -u postgres createuser --superuser nexus
sudo -u postgres createdb -O nexus nexus
# (the app applies migrations/ itself on first connect)

# 4. Configuration
cp .env.example .env
# Fill in .env — see the required variables below.

# 5. Check everything
./scripts/nexus.sh setup

# 6. Install the systemd units and start
sudo cp packaging/systemd/nexus-lab-worker.service /etc/systemd/system/
# The web unit is generated from your .env; see packaging/systemd/README.md.
sudo systemctl daemon-reload
./scripts/nexus.sh start
```

## Required environment (the minimum to boot)

| Variable | What it is |
|---|---|
| `SUPABASE_DB_URL` | PostgreSQL DSN, e.g. `postgresql://nexus:pw@127.0.0.1:5432/nexus` |
| `WEB_AUTH_TOKEN` | bearer token for machine clients (the CLI, MCP) |
| `AUTH_USERNAME` | the browser login name (single-owner model) |
| `AUTH_PASSWORD_HASH` | `python -m orchestration.web_auth hash` of your password |
| `AUTH_SESSION_SECRET` | a long random string, **separate** from the Cloud key |
| `NEXUS_SECRET_KEY` | Fernet key for encrypting stored secrets (GitHub PATs, keys) |
| `OPENHANDS_API_KEY` | the OpenHands key work runs under (or add accounts in the UI) |

The full list, with defaults and the module that reads each, is
`docs/ENVIRONMENT.md`. `.env.example` is the template.

## Day-to-day

```bash
./scripts/nexus.sh status     # services + /healthz + /readyz + the local URL
./scripts/nexus.sh logs       # follow the web and worker logs
./scripts/nexus.sh restart    # after a config change
./scripts/nexus.sh backup     # pg_dump + SHA-256 into ./backups
./scripts/nexus.sh restore <dump>   # prove a dump loads (into a scratch DB)
./scripts/nexus.sh update     # git pull, pip install, restart
```

The local URL is `http://127.0.0.1:8080/` (set `PORT` to change it).

## Exposure

By default the services bind to loopback and are **not** reachable from the
internet. To expose NEXUS:

- **Temporary:** `./scripts/nexus.sh tunnel` prints a `*.trycloudflare.com` URL
  that changes on every start. Good for a demo.
- **Stable:** a named Cloudflare Tunnel on your own domain — see
  `docs/CLOUDFLARE_TUNNEL.md`.

Never publish PostgreSQL, the worker, SSH or the Docker socket. Only the web
process is exposed, and only through the tunnel/proxy.

## What "one command" means, and its limits

`./scripts/nexus.sh start` starts the systemd units and **waits for
`/healthz`** before it reports success. It will not print READY for a service
that failed to bind. If a dependency is missing it says which. It never deletes
user data: `stop` stops the services, `backup` only adds a file, and `restore`
works on a scratch database and drops only that scratch database.

## See also

- `docs/OPERATIONS.md` — running, diagnosing, recovering.
- `docs/BACKUP_AND_RESTORE.md` — the backup policy and the restore drill.
- `docs/ENVIRONMENT.md` — every variable.
- `docs/CLOUDFLARE_TUNNEL.md` — the two tunnel modes.
- `docs/SINGLE_VM.md` — the Docker Compose variant of the same topology.
