#!/usr/bin/env bash
# nexus — one command to run, stop, back up and restore NEXUS on Debian 13.
#
#   ./scripts/nexus.sh setup      check dependencies and configuration
#   ./scripts/nexus.sh start      start PostgreSQL + web + worker (systemd)
#   ./scripts/nexus.sh stop       stop the services (data is kept)
#   ./scripts/nexus.sh restart    restart the services
#   ./scripts/nexus.sh status     services, health, and the local URL
#   ./scripts/nexus.sh logs       follow the web and worker logs
#   ./scripts/nexus.sh backup     dump the database and record a manifest
#   ./scripts/nexus.sh restore    restore a dump into a fresh test database
#   ./scripts/nexus.sh update     pull, migrate, restart
#   ./scripts/nexus.sh tunnel     a temporary public URL (cloudflared quick tunnel)
#
# Every command is safe to run twice. Nothing here deletes user data: `stop`
# stops the services and `backup` only adds a file. `restore` never touches the
# live database - it restores into a scratch database and proves the dump loads.
#
# This is a thin, honest wrapper. It does not start NEXUS unless the health check
# answers; it does not report READY on a service that failed to bind.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENV_FILE="${NEXUS_ENV_FILE:-$ROOT/.env}"
BACKUP_DIR="${NEXUS_BACKUP_DIR:-$ROOT/backups}"
WEB_UNIT="${NEXUS_WEB_UNIT:-nexus-web}"
WORKER_UNIT="${NEXUS_WORKER_UNIT:-nexus-lab-worker}"
PORT="${PORT:-8080}"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

load_env() {
  [ -f "$ENV_FILE" ] || die "no $ENV_FILE — copy .env.example to .env and fill it in"
  # Export only the variables the file sets; never echo a value.
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
}

db_url() {
  # The scripts read the same variable the service does.
  printf '%s' "${SUPABASE_DB_URL:-${NEXUS_DB_URL:-}}"
}

# Replace the database name in a DSN, whether it is a URL
# (postgresql://u:p@h:5432/db) or libpq key=value (host=h dbname=db ...).
with_dbname() {
  # A URL query may override the path's dbname; libpq conninfo values may be
  # quoted. A regexp substitution cannot safely choose a restore target.
  # Keep input off the Python command line and never echo parser errors/DSNs.
  printf '%s' "$1" | python3 -c '
import sys
from psycopg.conninfo import make_conninfo
try:
    sys.stdout.write(make_conninfo(sys.stdin.read(), dbname=sys.argv[1]))
except Exception:
    sys.exit("invalid PostgreSQL connection parameters")
' "$2"
}

# The DSN pointing at the maintenance database (for CREATE/DROP DATABASE).
admin_url() {
  with_dbname "$(db_url)" postgres
}

cmd_setup() {
  say "Checking dependencies"
  command -v python3 >/dev/null || die "python3 is required"
  ok "python3 $(python3 --version 2>&1 | awk '{print $2}')"
  command -v psql >/dev/null || die "psql (PostgreSQL client) is required"
  ok "psql $(psql --version | awk '{print $3}')"
  command -v systemctl >/dev/null || warn "systemctl not found — systemd commands will not work here"
  python3 -c 'import psycopg' 2>/dev/null && ok "psycopg installed" \
    || warn "psycopg missing — run: pip install -r requirements.txt"

  say "Checking configuration"
  load_env
  local missing=()
  for var in SUPABASE_DB_URL WEB_AUTH_TOKEN AUTH_USERNAME AUTH_PASSWORD_HASH AUTH_SESSION_SECRET; do
    [ -n "${!var:-}" ] || missing+=("$var")
  done
  if [ ${#missing[@]} -gt 0 ]; then
    die "missing in $ENV_FILE: ${missing[*]}"
  fi
  ok "required variables are set"
  [ -n "${OPENHANDS_API_KEY:-}" ] && ok "OPENHANDS_API_KEY present" || warn "no OPENHANDS_API_KEY — runs cannot dispatch"

  say "Checking the database"
  if psql "$(db_url)" -tAc 'select 1' >/dev/null 2>&1; then
    ok "database reachable"
  else
    warn "database not reachable yet — start PostgreSQL, then re-run: $0 status"
  fi
  say "Setup checks complete."
}

cmd_start() {
  load_env
  say "Starting NEXUS"
  systemctl start "$WEB_UNIT" 2>/dev/null || die "cannot start $WEB_UNIT (is it installed? see packaging/systemd/)"
  systemctl start "$WORKER_UNIT" 2>/dev/null || warn "$WORKER_UNIT did not start (the web server also advances runs)"
  wait_healthy || die "the web service did not become healthy — check: $0 logs"
  cmd_status
}

wait_healthy() {
  local i
  for i in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
      ok "health check answered"
      return 0
    fi
    sleep 1
  done
  return 1
}

cmd_stop() {
  say "Stopping NEXUS (data is kept)"
  systemctl stop "$WORKER_UNIT" 2>/dev/null || true
  systemctl stop "$WEB_UNIT" 2>/dev/null || true
  ok "services stopped"
}

cmd_restart() { cmd_stop; cmd_start; }

cmd_status() {
  say "Services"
  for unit in "$WEB_UNIT" "$WORKER_UNIT"; do
    if systemctl is-active --quiet "$unit" 2>/dev/null; then ok "$unit active"; else warn "$unit not active"; fi
  done
  say "Health"
  if curl -fsS "http://127.0.0.1:$PORT/healthz" 2>/dev/null; then
    echo; ok "liveness"
  else
    warn "no answer on http://127.0.0.1:$PORT/healthz"
  fi
  if curl -fsS "http://127.0.0.1:$PORT/readyz" >/dev/null 2>&1; then ok "readiness (database reachable)"; else warn "readiness failed (database?)"; fi
  say "Local URL: http://127.0.0.1:$PORT/"
}

cmd_logs() {
  say "Following logs (Ctrl-C to stop)"
  journalctl -u "$WEB_UNIT" -u "$WORKER_UNIT" -f -n 50 2>/dev/null \
    || die "journalctl unavailable — read the service logs another way"
}

cmd_backup() {
  umask 077
  load_env
  mkdir -p "$BACKUP_DIR"
  local stamp dump
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  dump="$BACKUP_DIR/nexus-$stamp.dump"
  say "Backing up the database to $dump"
  pg_dump --format=custom --no-owner "$(db_url)" -f "$dump"
  ( cd "$BACKUP_DIR" && sha256sum "$(basename "$dump")" > "$(basename "$dump").sha256" )
  ok "dump written ($(du -h "$dump" | cut -f1)) with a SHA-256"
  # Keep the last 14; rotate only our own dumps, never anything else.
  ls -1t "$BACKUP_DIR"/nexus-*.dump 2>/dev/null | tail -n +15 | while read -r old; do
    rm -f "$old" "$old.sha256"; warn "rotated out $(basename "$old")"
  done
  say "Restore is proven by: $0 restore $dump"
}

cmd_restore() {
  load_env
  local dump="${1:-}"
  [ -n "$dump" ] || die "usage: $0 restore <dump-file>"
  [ -f "$dump" ] || die "no such dump: $dump"
  if [ -f "$dump.sha256" ]; then
    # Verify the selected dump, not a different pathname from the sidecar.
    local expected actual
    read -r expected _ < "$dump.sha256" || die "invalid dump checksum file"
    actual="$(sha256sum < "$dump")"
    [[ "$expected" =~ ^[a-f0-9]{64}$ && "$expected" = "${actual%% *}" ]] || die "dump checksum mismatch"
  else
    warn "no checksum sidecar: testing an unverified external dump"
  fi
  local testdb="nexus_restore_test_$(date +%s)"
  local admin test_url
  admin="$(admin_url)"
  say "Restoring $dump into a fresh database $testdb (the live database is untouched)"
  psql -X "$admin" -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$testdb\""
  test_url="$(with_dbname "$(db_url)" "$testdb")"
  [ "$(psql -X "$test_url" -v ON_ERROR_STOP=1 -Atc 'select current_database()')" = "$testdb" ] \
    || die "scratch database identity mismatch; refusing restore"
  if pg_restore --no-owner --exit-on-error -d "$test_url" "$dump"; then
    local n
    n="$(psql -X "$test_url" -tAc 'select count(*) from information_schema.tables where table_schema=current_schema()')"
    ok "restored; $n tables present in the scratch database"
    ok "the dump loads — the live database was not modified"
  else
    warn "restore reported errors; the scratch database $testdb was kept for inspection"
    return 1
  fi
  psql -X "$admin" -c "DROP DATABASE \"$testdb\"" >/dev/null 2>&1 || warn "could not drop $testdb"
}

cmd_update() {
  load_env
  say "Updating NEXUS"
  git pull --ff-only || die "git pull failed — resolve by hand, then re-run"
  python3 -m pip install -r requirements.txt >/dev/null && ok "dependencies updated"
  cmd_restart
  say "Migrations apply on first connect; /readyz confirms the database."
}

cmd_tunnel() {
  command -v cloudflared >/dev/null || die "cloudflared not installed — see docs/CLOUDFLARE_TUNNEL.md"
  local target="${1:-${NEXUS_TUNNEL_CONFIG:-}}"
  if [ -n "$target" ]; then
    if [ -f "$target" ]; then
      say "Starting named Cloudflare Tunnel from config: $target"
      exec cloudflared tunnel --no-autoupdate --config "$target" run
    fi
    say "Starting named Cloudflare Tunnel: $target"
    exec cloudflared tunnel --no-autoupdate run "$target"
  fi
  if [ -n "${NEXUS_TUNNEL_NAME:-}" ]; then
    say "Starting named Cloudflare Tunnel: $NEXUS_TUNNEL_NAME"
    exec cloudflared tunnel --no-autoupdate run "$NEXUS_TUNNEL_NAME"
  fi
  say "Starting a temporary public URL (trycloudflare.com). It changes on every start."
  say "Press Ctrl-C to stop. The URL is printed below by cloudflared."
  cloudflared tunnel --url "http://127.0.0.1:$PORT"
}

main() {
  local cmd="${1:-status}"; shift || true
  case "$cmd" in
    setup)   cmd_setup ;;
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_restart ;;
    status)  cmd_status ;;
    logs)    cmd_logs ;;
    backup)  cmd_backup ;;
    restore) cmd_restore "$@" ;;
    update)  cmd_update ;;
    tunnel)  cmd_tunnel "$@" ;;
    -h|--help|help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//' ;;
    *) die "unknown command: $cmd (try: $0 help)" ;;
  esac
}

main "$@"
