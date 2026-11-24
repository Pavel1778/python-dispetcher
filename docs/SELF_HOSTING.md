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

# 6. Install the worker unit (adjust its /opt/nexus paths first) and your web
# unit described below. The operator needs permission to control both units.
sudo install -m 0644 packaging/systemd/nexus-lab-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
./scripts/nexus.sh start
```

The checkout supplies the worker unit; it does **not** generate a web unit.
Provide `nexus-web.service` with the same `WorkingDirectory` and `EnvironmentFile`
as the worker. For an installation in `/opt/nexus`, its `ExecStart` can be
`/opt/nexus/.venv/bin/python -m orchestration.web_server --host 127.0.0.1 --port 8080`.
Run the units under an appropriate unprivileged service account. If using other
unit names, export `NEXUS_WEB_UNIT` and `NEXUS_WORKER_UNIT` in the shell that
runs the script. PostgreSQL must already be running; the script does not start
or stop it.

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

The local URL is `http://127.0.0.1:8080/`. For a different port, set `PORT`
in the trusted shell-compatible `.env` and pass that port to the web unit's
`--port`. `status` reports both probes; it is diagnostic, not a startup gate.

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

- `start` starts web first. Before requesting the worker it requires a direct
  HTTP **200 from `/readyz`** on loopback and an active web unit. A live process
  (`/healthz`), a redirect or a `503` does not pass. It tries up to 30 times,
  with a two-second timeout per request and one second between attempts.
- Success also requires the worker unit to start and be active. A worker failure
  is a nonzero exit, not a silent fallback to the web process. Web is left up
  for diagnosis; use `status` and `logs`. These checks do not prove a worker
  heartbeat or a provider's availability. If you intentionally run web alone,
  manage just the web unit rather than using this two-unit command.
- `stop` requests both stops, worker first, even if the first fails. It names
  any unit systemd refused to stop and exits nonzero. `restart` checks that the
  environment file and probe tools are available before stopping anything; a
  failed stop prevents the start phase.
- `update` restarts only after **both** the fast-forward pull and dependency
  install succeed. If `pip` fails, no units are stopped or restarted; the
  checkout may already have advanced. Fix the install in the service's virtual
  environment before retrying. There is no automatic code or package rollback.

None of these service commands deletes database data. `backup` writes a dump
and checksum; `restore` tests a dump in a scratch database.

## See also

- `docs/OPERATIONS.md` — running, diagnosing, recovering.
- `docs/BACKUP_AND_RESTORE.md` — the backup policy and the restore drill.
- `docs/ENVIRONMENT.md` — every variable.
- `docs/CLOUDFLARE_TUNNEL.md` — the two tunnel modes.
- `docs/SINGLE_VM.md` — the Docker Compose variant of the same topology.
