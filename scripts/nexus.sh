#!/usr/bin/env bash
# nexus — one command to run, stop, back up and restore NEXUS on Debian 13.
#
#   ./scripts/nexus.sh setup      check dependencies and configuration
#   ./scripts/nexus.sh start      start web + worker (systemd; PostgreSQL must be up)
#   ./scripts/nexus.sh stop       stop the services (data is kept)
#   ./scripts/nexus.sh restart    restart the services
#   ./scripts/nexus.sh status     services, health, and the local URL
#   ./scripts/nexus.sh logs       follow the web and worker logs
#   ./scripts/nexus.sh backup     dump the database and record a manifest
#   ./scripts/nexus.sh restore    restore a dump into a fresh test database
#   ./scripts/nexus.sh update     fast-forward, install dependencies, restart
#   ./scripts/nexus.sh tunnel     a temporary public URL (cloudflared quick tunnel)
#
# `stop` stops the services, not the database. `backup` rotates completed local
# archives; `restore` never writes to the live database — it uses a scratch
# database to prove the dump loads.
#
# During startup the web service must pass its database readiness probe before
# the script starts the worker. A refused systemd operation is an error.
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

# Check these before restart/update can take a working installation down.
prepare_start() {
  load_env
  command -v systemctl >/dev/null || die "systemctl is required"
  command -v curl >/dev/null || die "curl is required for the readiness check"
}

http_ok() {
  # Probe loopback directly. A redirect, a stalled connection or a partial
  # response with a 200 header is not a successful probe.
  local code
  code=$(curl -q --silent --noproxy '*' --max-time 2 --output /dev/null \
    --write-out '%{http_code}' "http://127.0.0.1:$PORT/$1") && [[ $code == 200 ]]
}

wait_ready() {
  local i
  for ((i=1; i<=30; i++)); do
    if http_ok readyz; then return 0; fi
    if ((i < 30)); then sleep 1; fi
  done
  return 1
}

cmd_start() {
  say "Starting NEXUS"
  systemctl start "$WEB_UNIT" || die "cannot start $WEB_UNIT — check: $0 logs"
  wait_ready || die "$WEB_UNIT did not pass /readyz on port $PORT; not starting $WORKER_UNIT — check: $0 logs"
  systemctl is-active --quiet "$WEB_UNIT" || die "$WEB_UNIT is not active; not starting $WORKER_UNIT"
  ok "$WEB_UNIT ready (database reachable)"
  systemctl start "$WORKER_UNIT" || die "cannot start $WORKER_UNIT — check: $0 logs"
  systemctl is-active --quiet "$WORKER_UNIT" || die "$WORKER_UNIT is not active — check: $0 logs"
  ok "$WORKER_UNIT active"
  say "Local URL: http://127.0.0.1:$PORT/"
}

cmd_stop() {
  local unit failed=()
  say "Stopping NEXUS (data is kept)"
  for unit in "$WORKER_UNIT" "$WEB_UNIT"; do
    systemctl stop "$unit" || failed+=("$unit")
  done
  if ((${#failed[@]})); then die "cannot stop: ${failed[*]} — check: $0 status"; fi
  ok "services stopped"
}

cmd_restart() { cmd_stop; cmd_start; }

cmd_status() {
  # Useful even before setup, but use the service's port when configured.
  if [[ -f "$ENV_FILE" ]]; then load_env; fi
  say "Services"
  for unit in "$WEB_UNIT" "$WORKER_UNIT"; do
    if systemctl is-active --quiet "$unit" 2>/dev/null; then ok "$unit active"; else warn "$unit not active"; fi
  done
  say "Health"
  if http_ok healthz; then ok "liveness"; else warn "liveness failed on http://127.0.0.1:$PORT/healthz"; fi
  if http_ok readyz; then ok "readiness (database reachable)"; else warn "readiness failed (database?)"; fi
  say "Local URL: http://127.0.0.1:$PORT/"
}

cmd_logs() {
  say "Following logs (Ctrl-C to stop)"
  journalctl -u "$WEB_UNIT" -u "$WORKER_UNIT" -f -n 50 2>/dev/null \
    || die "journalctl unavailable — read the service logs another way"
}

# Called while holding the backup-directory lock. Always retain the new dump,
# even if the machine clock went backwards or other files have future mtimes.
rotate_backups() (
  cd -- "$BACKUP_DIR"
  local newest=$1 old others=()
  for old in nexus-*.dump; do
    [[ $old != "$newest" && $old =~ ^nexus-[0-9]{8}T[0-9]{6}Z(-[0-9]+)?\.dump$ ]] || continue
    [[ -f $old && ! -L $old && -f $old.sha256 && ! -L $old.sha256 ]] || continue
    others+=("$old")
  done
  if ((${#others[@]} > 13)); then
    LC_ALL=C ls -t --zero -- "${others[@]}" | tail -z -n +14 | while IFS= read -r -d '' old; do
      rm -- "$old"
      rm -- "$old.sha256"
      warn "rotated out $old"
    done
  fi
)

# A skipped `mv -n` can exit zero. Confirm the staged regular file moved.
publish_backup_file() { mv -nT -- "$1" "$2" && [[ ! -e $1 ]]; }

cmd_backup() (
  umask 077
  load_env
  mkdir -p -- "$BACKUP_DIR"
  # A timer and a manual run may overlap. Lock the directory inode itself:
  # no lockfile to unlink or mistake for stale after a crash.
  exec 9<"$BACKUP_DIR"
  flock --exclusive 9 || die "cannot lock backup directory: $BACKUP_DIR"

  local stem dump name stage suffix=0
  stem="$BACKUP_DIR/nexus-$(date -u +%Y%m%dT%H%M%SZ)"
  dump="$stem.dump"
  while [[ -e $dump || -L $dump || -e $dump.sha256 || -L $dump.sha256 ]]; do
    ((++suffix))
    dump="$stem-$suffix.dump"
  done
  name=${dump##*/}
  stage=$(mktemp -d -- "$BACKUP_DIR/.nexus-backup.XXXXXXXX")
  trap 'rm -rf -- "$stage"' EXIT
  say "Backing up the database to $dump"
  pg_dump --format=custom --no-owner "$(db_url)" -f "$stage/$name"
  ( cd -- "$stage" && sha256sum -- "$name" > "$name.sha256" )
  # A visible .dump is the commit point. Both files are complete before the
  # first rename; staging on the same filesystem keeps each rename atomic.
  publish_backup_file "$stage/$name.sha256" "$dump.sha256" || die "cannot publish checksum for $dump"
  if ! publish_backup_file "$stage/$name" "$dump"; then
    rm -- "$dump.sha256"
    die "cannot publish $dump"
  fi
  ok "dump written ($(du -h -- "$dump" | cut -f1)) with a SHA-256"
  rotate_backups "$name"
  printf 'Test the backup with: %q restore %q\n' "$0" "$dump"
)

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
  say "Updating NEXUS"
  git pull --ff-only || die "git pull failed — resolve by hand, then re-run"
  python3 -m pip install -r requirements.txt >/dev/null || die "dependency installation failed; not restarting services"
  ok "dependencies updated"
  cmd_restart
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
    start)   prepare_start; cmd_start ;;
    stop)    cmd_stop ;;
    restart) prepare_start; cmd_restart ;;
    status)  cmd_status ;;
    logs)    cmd_logs ;;
    backup)  cmd_backup ;;
    restore) cmd_restore "$@" ;;
    update)  prepare_start; cmd_update ;;
    tunnel)  cmd_tunnel "$@" ;;
    -h|--help|help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//' ;;
    *) die "unknown command: $cmd (try: $0 help)" ;;
  esac
}

main "$@"
