#!/usr/bin/env bash
# Run a test or benchmark which is allowed to reset its schema. Do not accept a
# DSN: even a database on localhost can contain somebody's work. Some tests
# also create/drop cluster-wide roles, so a fresh database alone isn't enough.
set -euo pipefail
if (($# == 0)); then echo "usage: $0 COMMAND [ARG ...]" >&2; exit 2; fi
for tool in initdb pg_ctl createdb; do
    command -v "$tool" >/dev/null || { echo "missing $tool (put PostgreSQL's bin directory on PATH)" >&2; exit 2; }
done
original_umask=$(umask)
umask 077
tmp=$(mktemp -d /tmp/nexus-audit.XXXXXXXX)
mkdir "$tmp/home"
# No .pgpass, PGSERVICE, DB URLs, real provider credentials or git identity.
clean=(env -i "PATH=$PATH" "HOME=$tmp/home" LANG=C.UTF-8 TZ=UTC)
cleanup() {
    if [[ -f "$tmp/data/postmaster.pid" ]]; then
        "${clean[@]}" pg_ctl -D "$tmp/data" -w -m fast stop >/dev/null || {
            echo "Could not stop scratch cluster; keeping $tmp" >&2; return;
        }
    fi
    rm -rf -- "$tmp"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# UTF8 encoding with plain C LC_CTYPE would leave Cyrillic uppercase in lower().
"${clean[@]}" initdb --locale=C.UTF-8 --encoding=UTF8 --auth-local=trust --auth-host=reject \
    -U nexus_audit -D "$tmp/data" >/dev/null
# Only a socket inside a mode-0700 directory, no listening TCP port. The same
# port number in two such directories designates two different clusters.
printf "\nlisten_addresses = ''\nunix_socket_directories = '%s'\nport = 5432\n" "$tmp" >> "$tmp/data/postgresql.conf"
"${clean[@]}" pg_ctl -D "$tmp/data" -l "$tmp/postgres.log" -w start >/dev/null
"${clean[@]}" createdb -h "$tmp" -p 5432 -U nexus_audit nexus_audit
# Pass only local tool locations needed by the browser and parity suites.
for var in E2E_CHROMIUM_EXECUTABLE E2E_CHROMIUM_LD_LIBRARY_PATH FONTCONFIG_PATH NEXUS_RUST_BIN NEXUS_CLAIM_PROBE; do
    if [[ -n ${!var:-} ]]; then clean+=("$var=${!var}"); fi
done
# Do not change subprocess build semantics (dpkg-deb requires 0755 control dirs).
umask "$original_umask"
"${clean[@]}" "PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}" \
    "TEST_SUPABASE_DB_URL=postgresql:///nexus_audit?user=nexus_audit&host=$tmp&port=5432" \
    NEXUS_ALLOW_PRIVATE_TARGETS=1 NEXUS_LAB_INLINE=0 "$@"
