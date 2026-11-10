#!/usr/bin/env bash
#
# Reproducible build / lint / test evidence for the NEXUS control plane.
#
# This is the single command that produces the numbers in
# docs/GROUND-TRUTH-BUILD-LINT-TEST.md. It runs the real tools (no mocks of the
# toolchain), prints every command it runs, and ends with a machine-checkable
# summary block. It never hardcodes an expected count: whatever the run yields
# is the result.
#
# Usage:
#   TEST_SUPABASE_DB_URL="postgresql://user:pw@127.0.0.1:5432/db" \
#     tools/verify_build_lint_test.sh
#
# Without TEST_SUPABASE_DB_URL the DB-backed modules skip cleanly and the run is
# NOT a full verification -- the script says so in the summary.
#
# The pinned tool versions live in requirements-dev.txt (pytest, ruff). Install
# them with:  pip install -r requirements.txt -r requirements-dev.txt

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 2

PYTHON="${PYTHON:-python3}"
OUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/nexus-verify.XXXXXX")"
PYTEST_LOG="$OUT_DIR/pytest.log"
WHEEL_DIR="$OUT_DIR/wheel"

hr() { printf '%s\n' "------------------------------------------------------------"; }
say() { printf '%s\n' "$*"; }

say "NEXUS build / lint / test verification"
say "repo:   $REPO_ROOT"
say "output: $OUT_DIR"
hr

# ---- 0. Toolchain and environment -----------------------------------------
say "STEP 0  toolchain"
say "  python:       $($PYTHON --version 2>&1)"
say "  pytest:       $($PYTHON -m pytest --version 2>&1 | head -1)"
say "  ruff:         $($PYTHON -m ruff --version 2>&1 | head -1)"
say "  psycopg:      $($PYTHON -c 'import psycopg; print(psycopg.__version__)' 2>&1)"
say "  cryptography: $($PYTHON -c 'import cryptography; print(cryptography.__version__)' 2>&1)"
if command -v psql >/dev/null 2>&1; then
  say "  psql:         $(psql --version 2>&1)"
fi
if [ -n "${TEST_SUPABASE_DB_URL:-}" ]; then
  say "  DSN:          set (TEST_SUPABASE_DB_URL)"
else
  say "  DSN:          UNSET -- DB-backed modules will skip; not a full run"
fi
hr

# ---- 1. Exact revision under test -----------------------------------------
say "STEP 1  revision"
say "  HEAD:     $(git rev-parse HEAD 2>&1)"
say "  describe: $(git describe --tags --always --dirty 2>&1)"
say "  subject:  $(git --no-pager log -1 --format=%s 2>&1)"
say "  dirty:    $(git status --porcelain | wc -l | tr -d ' ') changed path(s)"
hr

# ---- 2. Build the distribution --------------------------------------------
say "STEP 2  build (python -m pip wheel . --no-deps)"
mkdir -p "$WHEEL_DIR"
if $PYTHON -m pip wheel . --no-deps -w "$WHEEL_DIR" >"$OUT_DIR/build.log" 2>&1; then
  BUILD_RC=0
  WHEEL="$(ls "$WHEEL_DIR"/*.whl 2>/dev/null | head -1)"
  say "  result: OK  ($(basename "${WHEEL:-none}"))"
else
  BUILD_RC=1
  say "  result: FAILED"
  tail -5 "$OUT_DIR/build.log" | sed 's/^/    /'
fi
hr

# ---- 3. Lint ---------------------------------------------------------------
say "STEP 3  lint ($PYTHON -m ruff check .)"
$PYTHON -m ruff check . >"$OUT_DIR/ruff.log" 2>&1
RUFF_RC=$?
tail -3 "$OUT_DIR/ruff.log" | sed 's/^/  /'
say "  exit code: $RUFF_RC"
hr

# ---- 4. Test suite ---------------------------------------------------------
say "STEP 4  tests ($PYTHON -m pytest tests/ -q -p no:cacheprovider -rs)"
say "  collecting..."
COLLECTED="$($PYTHON -m pytest tests/ -q -p no:cacheprovider --collect-only 2>/dev/null | grep -c '::')"
say "  collected: $COLLECTED test(s)"
say "  running (this takes several minutes)..."
$PYTHON -m pytest tests/ -q -p no:cacheprovider -rs >"$PYTEST_LOG" 2>&1
PYTEST_RC=$?
SUMMARY="$(grep -E '^[0-9]+ (passed|failed)' "$PYTEST_LOG" | tail -1)"
say "  summary: ${SUMMARY:-<no summary line>}"
say "  exit code: $PYTEST_RC"
say "  skip reasons:"
grep -E '^SKIPPED' "$PYTEST_LOG" | sed 's/^/    /'
hr

# ---- Summary block ---------------------------------------------------------
say "SUMMARY"
say "  HEAD=$(git rev-parse HEAD 2>/dev/null)"
say "  build_rc=$BUILD_RC lint_rc=$RUFF_RC test_rc=$PYTEST_RC"
say "  collected=$COLLECTED"
say "  result=${SUMMARY:-<none>}"
say "  full pytest log: $PYTEST_LOG"
if [ -z "${TEST_SUPABASE_DB_URL:-}" ]; then
  say "  WARNING: TEST_SUPABASE_DB_URL was unset -- DB tests skipped, not full."
fi
hr
say "Reproduce by re-running this script at the same commit."

FAILED=0
[ "$BUILD_RC" -ne 0 ] && FAILED=1
[ "$RUFF_RC" -ne 0 ] && FAILED=1
[ "$PYTEST_RC" -ne 0 ] && FAILED=1
exit "$FAILED"
