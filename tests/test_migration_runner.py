"""Migration runner integrity: the ten failure modes the audit named.

Each test drives the real runner against a real PostgreSQL database in a
throw-away schema, so what is asserted is the behaviour of ``apply_migrations``,
not a mock of it. The cases map one-to-one onto the audit's list:

1. a migration whose SQL succeeds but whose effect is absent is not recorded;
2. a failing migration is visible, not marked applied;
3. migrations after a failure do not run;
4. the error names the exact migration;
5. a changed body for an already-applied migration is refused, not re-run —
   whether or not the manifest was updated along with the file;
6. a duplicate number is refused (the ``0011`` case), with both files named;
7. an applied history that skips a step is refused;
8. a clean bootstrap applies everything and is idempotent;
9. an upgrade of a database from the ``main`` schema converges and verifies;
10. the manifest itself tracks the files it claims to describe — their bodies
    and, file by file, everything they add to the schema — and the runner
    refuses a body the manifest did not review.

Tests 1-7 use a temporary directory with synthetic migrations so the real
``migrations/`` directory is never mutated, and every test runs in its own
schema, so the tests that do apply the real migrations cannot disturb each
other or ``public``.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import uuid

import psycopg
import pytest

from orchestration import migration_runner as runner
from orchestration.cloud_store import PostgresStore
from orchestration.migration_runner import MigrationError

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database"
)

REPO_MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations"


@pytest.fixture
def schema_dsn():
    """A private schema per test, so tests cannot disturb each other or ``public``."""
    name = "mig_" + uuid.uuid4().hex[:12]
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{name}"')
    dsn = DSN + f"&options=-csearch_path%3D{name}" if "?" in DSN else \
        DSN + f"?options=-csearch_path%3D{name}"
    yield name, dsn
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA "{name}" CASCADE')


@pytest.fixture
def tmp_migrations(tmp_path):
    """A scratch migration directory the test can shape freely."""
    yield tmp_path


def write_manifest(base: pathlib.Path, *, columns=None, tables=None,
                   indexes=None, rls=None,
                   files: dict[str, str] | None = None) -> None:
    """Write a manifest whose checksums match the files currently on disk."""
    files = files if files is not None else {
        p.name: runner.sha256_text(p.read_text()) for p in sorted(base.glob("*.sql"))
    }
    (base / runner.MANIFEST_NAME).write_text(json.dumps({
        "files": files, "columns": columns or {}, "table_columns": tables or {},
        "indexes": indexes or {}, "rls": rls or {},
    }), encoding="utf-8")


def add_migration(base: pathlib.Path, name: str, sql: str) -> None:
    (base / name).write_text(sql, encoding="utf-8")


def migrate(dsn: str, base: pathlib.Path) -> list[str]:
    store = PostgresStore(dsn, auto_migrate=False)
    try:
        return store.apply_migrations(str(base))
    finally:
        store.close()


def recorded(dsn: str) -> dict[str, str]:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT filename, checksum FROM schema_migrations")
        return {name: cs for name, cs in cur.fetchall()}


# ------------------------------------------------------ 1. effect verification
def test_a_migration_whose_effect_is_absent_is_not_recorded(schema_dsn, tmp_migrations):
    """SQL that "succeeds" without producing its declared schema is a failure.

    ``CREATE TABLE IF NOT EXISTS`` against a pre-existing table of the wrong
    shape is the exact case: the statement succeeds, the schema is wrong, and the
    old runner recorded it as applied. The table here exists but lacks the
    declared column.
    """
    _, dsn = schema_dsn
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE widgets (id int)")
        conn.commit()

    add_migration(tmp_migrations, "0001_widgets.sql",
                  "CREATE TABLE IF NOT EXISTS widgets (id int, name text);")
    write_manifest(tmp_migrations,
                   tables={"0001_widgets.sql": [["widgets", ["id", "name"]]]})

    with pytest.raises(MigrationError) as exc:
        migrate(dsn, tmp_migrations)
    assert exc.value.kind == "effect_missing"
    assert exc.value.filename == "0001_widgets.sql"
    assert "name" in str(exc.value)
    assert recorded(dsn) == {}, "a migration with a missing effect must not be recorded"


def test_a_declared_column_is_verified_independently_of_its_table(schema_dsn, tmp_migrations):
    """An ``ALTER TABLE ... ADD COLUMN`` that silently did nothing is caught."""
    _, dsn = schema_dsn
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE t (id int)")
        conn.commit()
    # The SQL is a no-op on purpose: it adds a column to a different table.
    add_migration(tmp_migrations, "0001_add.sql",
                  "CREATE TABLE other (id int);")
    write_manifest(tmp_migrations, columns={"0001_add.sql": [["t", "label"]]})
    with pytest.raises(MigrationError) as exc:
        migrate(dsn, tmp_migrations)
    assert exc.value.kind == "effect_missing"
    assert exc.value.filename == "0001_add.sql"


# ------------------------------------------- 2-4. failure is visible and named
def test_a_failing_migration_is_visible_not_recorded_and_blocks_later_ones(
        schema_dsn, tmp_migrations):
    """A broken step: reported by name, absent from history, and nothing follows it."""
    _, dsn = schema_dsn
    add_migration(tmp_migrations, "0001_ok.sql", "CREATE TABLE a (id int);")
    add_migration(tmp_migrations, "0002_broken.sql", "CREATE TABLE b (id int;")  # syntax
    add_migration(tmp_migrations, "0003_later.sql", "CREATE TABLE c (id int);")
    write_manifest(tmp_migrations)

    with pytest.raises(MigrationError) as exc:
        migrate(dsn, tmp_migrations)
    assert exc.value.filename == "0002_broken.sql"
    assert exc.value.kind == "apply_failed"

    hist = recorded(dsn)
    assert "0001_ok.sql" in hist
    assert "0002_broken.sql" not in hist, "a failed migration must never be recorded"
    assert "0003_later.sql" not in hist, "nothing after a failure may run"
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('c')")
        assert cur.fetchone()[0] is None


def test_a_partial_file_rolls_back_completely(schema_dsn, tmp_migrations):
    """Half a file's statements are not left behind: the file commits as a unit."""
    _, dsn = schema_dsn
    # Contains its own BEGIN/COMMIT, like the reviewed SQL, and then fails: the
    # nested COMMIT must not make the first half permanent.
    add_migration(tmp_migrations, "0001_partial.sql", """
BEGIN;
CREATE TABLE first_half (id int);
COMMIT;
CREATE TABLE second_half (id int,;
""")
    write_manifest(tmp_migrations)
    with pytest.raises(MigrationError):
        migrate(dsn, tmp_migrations)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('first_half')")
        assert cur.fetchone()[0] is None, "the file did not commit as a unit"


def test_a_restart_after_a_failure_resumes_at_the_broken_file(schema_dsn, tmp_migrations):
    """The repair path: fix the file, re-run, and only the remaining steps apply."""
    _, dsn = schema_dsn
    add_migration(tmp_migrations, "0001_ok.sql", "CREATE TABLE a (id int);")
    add_migration(tmp_migrations, "0002_broken.sql", "CREATE TABLE b (id int;")
    write_manifest(tmp_migrations)
    with pytest.raises(MigrationError):
        migrate(dsn, tmp_migrations)

    add_migration(tmp_migrations, "0002_broken.sql", "CREATE TABLE b (id int);")
    write_manifest(tmp_migrations)  # checksum updated for the fixed body
    assert migrate(dsn, tmp_migrations) == ["0002_broken.sql"]
    assert set(recorded(dsn)) == {"0001_ok.sql", "0002_broken.sql"}


# --------------------------------------------------- 5. checksum drift refusal
def test_editing_an_applied_migration_is_refused_not_re_executed(
        schema_dsn, tmp_migrations):
    """The audit's silent re-run: same name, different body, recorded as applied."""
    _, dsn = schema_dsn
    add_migration(tmp_migrations, "0001_table.sql", "CREATE TABLE t (id int);")
    write_manifest(tmp_migrations)
    assert migrate(dsn, tmp_migrations) == ["0001_table.sql"]

    # Someone edits the applied file and updates the manifest to match.
    add_migration(tmp_migrations, "0001_table.sql",
                  "CREATE TABLE t (id int, extra text);")
    write_manifest(tmp_migrations)
    with pytest.raises(MigrationError) as exc:
        migrate(dsn, tmp_migrations)
    assert exc.value.kind == "checksum_changed"
    assert exc.value.filename == "0001_table.sql"
    # Nothing ran: the column the edited body would add is still absent.
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='t' AND table_schema=current_schema()"
        )
        assert {r[0] for r in cur.fetchall()} == {"id"}


def test_editing_an_applied_migration_without_touching_the_manifest_is_refused(
        schema_dsn, tmp_migrations):
    """The edit nobody reviews: the file changes and the manifest does not.

    This is the shape of the real incident — ``0011`` rewritten in place on
    ``main`` — because whoever edits an applied migration is not thinking about a
    manifest. Both bodies are idempotent on purpose, like the real migrations: the
    edited file *would* run cleanly, so only a checksum gate can stop it. Without
    one the recorded history still equals the manifest, the apply loop re-executes
    the new body and overwrites the recorded checksum, and nothing reports it.
    """
    _, dsn = schema_dsn
    add_migration(tmp_migrations, "0001_table.sql",
                  "CREATE TABLE IF NOT EXISTS t (id int);")
    write_manifest(tmp_migrations)
    assert migrate(dsn, tmp_migrations) == ["0001_table.sql"]
    before = recorded(dsn)

    # The applied file is edited; the manifest is left exactly as reviewed.
    add_migration(tmp_migrations, "0001_table.sql",
                  "CREATE TABLE IF NOT EXISTS t (id int);\n"
                  "ALTER TABLE t ADD COLUMN IF NOT EXISTS extra text;")
    with pytest.raises(MigrationError) as exc:
        migrate(dsn, tmp_migrations)
    assert exc.value.kind == "manifest_mismatch"
    assert exc.value.filename == "0001_table.sql"
    # Nothing ran, and the history was not rewritten to match the edit.
    assert recorded(dsn) == before
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='t' AND table_schema=current_schema()"
        )
        assert {r[0] for r in cur.fetchall()} == {"id"}


def test_a_body_the_manifest_did_not_review_is_refused_on_an_empty_database(
        schema_dsn, tmp_migrations):
    """With no history to compare against, the manifest checksum is the only gate.

    The refusal happens in pre-flight, so the database is left untouched: not even
    the ``schema_migrations`` bookkeeping table is created.
    """
    _, dsn = schema_dsn
    add_migration(tmp_migrations, "0001_table.sql", "CREATE TABLE t (id int);")
    write_manifest(tmp_migrations)
    add_migration(tmp_migrations, "0001_table.sql",
                  "CREATE TABLE t (id int, extra text);")
    with pytest.raises(MigrationError) as exc:
        migrate(dsn, tmp_migrations)
    assert exc.value.kind == "manifest_mismatch"
    assert exc.value.filename == "0001_table.sql"
    assert set(exc.value.detail["files"]) == {"0001_table.sql"}
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('t'), to_regclass('schema_migrations')")
        assert cur.fetchone() == (None, None)


def test_an_unlisted_migration_file_is_refused(schema_dsn, tmp_migrations):
    """An unreviewed file cannot be applied just by dropping it in the directory."""
    _, dsn = schema_dsn
    add_migration(tmp_migrations, "0001_ok.sql", "CREATE TABLE a (id int);")
    write_manifest(tmp_migrations)
    add_migration(tmp_migrations, "0002_sneaky.sql", "CREATE TABLE b (id int);")
    with pytest.raises(MigrationError) as exc:
        migrate(dsn, tmp_migrations)
    assert exc.value.kind == "unlisted_file"
    assert exc.value.filename == "0002_sneaky.sql"


def test_a_listed_but_missing_migration_is_refused(schema_dsn, tmp_migrations):
    """A lost step is reported, not silently skipped."""
    _, dsn = schema_dsn
    add_migration(tmp_migrations, "0001_ok.sql", "CREATE TABLE a (id int);")
    files = {p.name: runner.sha256_text(p.read_text())
             for p in sorted(tmp_migrations.glob("*.sql"))}
    files["0002_vanished.sql"] = "0" * 64
    write_manifest(tmp_migrations, files=files)
    with pytest.raises(MigrationError) as exc:
        migrate(dsn, tmp_migrations)
    assert exc.value.kind == "missing_file"
    assert exc.value.filename == "0002_vanished.sql"


# ------------------------------------------------------------- 6. duplicates
def test_a_duplicate_migration_number_is_refused(schema_dsn, tmp_migrations):
    """The PR17/main collision: two different files both numbered 0011."""
    _, dsn = schema_dsn
    add_migration(tmp_migrations, "0001_a.sql", "CREATE TABLE a (id int);")
    add_migration(tmp_migrations, "0011_nexus_files.sql", "CREATE TABLE f (id int);")
    add_migration(tmp_migrations, "0011_file_lab.sql", "CREATE TABLE l (id int);")
    write_manifest(tmp_migrations)
    with pytest.raises(MigrationError) as exc:
        migrate(dsn, tmp_migrations)
    assert exc.value.kind == "duplicate_number"
    assert "0011" in str(exc.value)
    assert "0011_file_lab.sql" in str(exc.value) or "0011_nexus_files.sql" in str(exc.value)
    # Pre-flight runs before any SQL, so not even the bookkeeping table exists:
    # the refusal leaves the database byte-for-byte as it was.
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('schema_migrations')")
        assert cur.fetchone()[0] is None


def test_a_bad_filename_is_refused(schema_dsn, tmp_migrations):
    """Ordering must not depend on sorting surprises."""
    _, dsn = schema_dsn
    add_migration(tmp_migrations, "1_init.sql", "CREATE TABLE a (id int);")
    write_manifest(tmp_migrations)
    with pytest.raises(MigrationError) as exc:
        migrate(dsn, tmp_migrations)
    assert exc.value.kind == "bad_filename"


# -------------------------------------------------- 7. missing predecessors
def test_history_that_skips_a_step_is_refused(schema_dsn, tmp_migrations):
    """A later migration on top of a missing one is not trusted.

    This is the partially-provisioned environment the audit found: the database
    records ``0003`` because someone ran files by hand, and has never run
    ``0002``.
    """
    _, dsn = schema_dsn
    add_migration(tmp_migrations, "0001_a.sql", "CREATE TABLE a (id int);")
    add_migration(tmp_migrations, "0002_b.sql", "CREATE TABLE b (id int);")
    add_migration(tmp_migrations, "0003_c.sql", "CREATE TABLE c (id int);")
    write_manifest(tmp_migrations)
    migrate(dsn, tmp_migrations)  # clean history first

    # Simulate the gap: 0002 was never really applied.
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM schema_migrations WHERE filename='0002_b.sql'")
        conn.commit()

    with pytest.raises(MigrationError) as exc:
        migrate(dsn, tmp_migrations)
    assert exc.value.kind == "missing_predecessor"
    assert exc.value.filename == "0002_b.sql"


def test_an_unknown_recorded_migration_is_refused(schema_dsn, tmp_migrations):
    """A database from another lineage cannot be migrated into this one."""
    _, dsn = schema_dsn
    add_migration(tmp_migrations, "0001_a.sql", "CREATE TABLE a (id int);")
    write_manifest(tmp_migrations)
    migrate(dsn, tmp_migrations)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO schema_migrations(filename, checksum) "
                    "VALUES ('0099_from_elsewhere.sql', 'x')")
        conn.commit()
    with pytest.raises(MigrationError) as exc:
        migrate(dsn, tmp_migrations)
    assert exc.value.kind == "unknown_history"


# ------------------------------------------- 8. clean bootstrap and idempotence
def test_a_clean_bootstrap_applies_the_real_migrations_then_is_a_no_op(schema_dsn):
    """The production tree, end to end, in an empty schema."""
    _, dsn = schema_dsn
    first = migrate(dsn, REPO_MIGRATIONS)
    assert len(first) == len(list(REPO_MIGRATIONS.glob("*.sql")))
    assert first[0].startswith("0001_") and first[-1].startswith("0020_")
    assert migrate(dsn, REPO_MIGRATIONS) == [], "a second run must be a no-op"
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('nexus_files')")
        assert cur.fetchone()[0] is not None


def test_the_manifest_describes_the_files_it_is_shipped_with():
    """The manifest's checksums and the reviewed SQL must agree.

    This is the coupling that makes the other tests meaningful: if someone edits a
    migration and forgets the manifest, the runner refuses to start with
    ``manifest_mismatch`` (see
    ``test_editing_an_applied_migration_without_touching_the_manifest_is_refused``).
    Failing here, in CI, names the file before a deploy does.
    """
    manifest = runner.load_manifest(REPO_MIGRATIONS)
    stale = [
        name for name, checksum in manifest["files"].items()
        if not (REPO_MIGRATIONS / name).is_file()
        or runner.sha256_text((REPO_MIGRATIONS / name).read_text()) != checksum
    ]
    assert not stale, (
        "migrations/0000_manifest.json is out of date for: " + ", ".join(sorted(stale))
    )
    missing = sorted(
        p.name for p in REPO_MIGRATIONS.glob("*.sql") if p.name not in manifest["files"]
    )
    assert not missing, "unlisted migrations: " + ", ".join(missing)


def test_every_real_migration_declares_a_verifiable_effect():
    """A migration with no declared effect cannot be verified, so it must not exist.

    This forces the manifest to stay a description of reality rather than a list
    of filenames: new SQL has to say what it creates.
    """
    manifest = runner.load_manifest(REPO_MIGRATIONS)
    columns = manifest.get("columns") or {}
    tables = manifest.get("table_columns") or {}
    verified = (set(columns) | set(tables)
                | set(manifest.get("indexes") or {})
                | set(manifest.get("rls") or {}))
    unverified = sorted(set(manifest["files"]) - verified)
    assert not unverified, (
        "migrations without a declared effect (add them to columns/table_columns): "
        + ", ".join(unverified)
    )


def schema_additions(cur) -> dict[str, set]:
    """What a migration can add that the manifest is able to declare.

    Read from the catalog, not parsed out of SQL. Indexes that back a primary-key
    or unique constraint are left out: they come with their table and carry
    generated names, so the manifest declares the table's columns instead.
    """
    cur.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema()"
    )
    columns = {f"{table}.{column}" for table, column in cur.fetchall()}
    cur.execute(
        "SELECT ic.relname FROM pg_class ic WHERE ic.relkind = 'i' "
        "AND ic.relnamespace = current_schema()::regnamespace AND NOT EXISTS ("
        "  SELECT 1 FROM pg_constraint con WHERE con.conindid = ic.oid"
        "  AND con.contype IN ('p', 'u', 'x'))"
    )
    indexes = {name for (name,) in cur.fetchall()}
    cur.execute(
        "SELECT relname FROM pg_class WHERE relkind = 'r' AND relrowsecurity "
        "AND relnamespace = current_schema()::regnamespace"
    )
    rls = {name for (name,) in cur.fetchall()}
    return {"column": columns, "index": indexes, "rls": rls}


def test_each_real_migration_declares_exactly_what_it_adds(schema_dsn):
    """File by file, the manifest's effects equal what the SQL does to the schema.

    The runner can only verify what the manifest declares, so an effect the
    manifest leaves out is an effect nobody checks — and nothing else notices the
    omission, because a missing declaration cannot fail a bootstrap. That is how
    eight columns went undeclared: four ``ALTER TABLE`` statements add several
    columns each (``0002``, ``0003``, ``0005``, ``0006``) and only the first
    column of each had been written down.

    Each file is executed on its own and the catalog is compared before and
    after, so the expectation comes from PostgreSQL rather than from reading SQL.
    Both directions are asserted: an addition that is not declared is unverified,
    and a declaration the file does not produce is a check that proves nothing
    (it would pass because an earlier file already created the object).
    """
    _, dsn = schema_dsn
    manifest = runner.load_manifest(REPO_MIGRATIONS)
    problems: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        before = schema_additions(cur)
        for migration in runner.discover(REPO_MIGRATIONS, manifest):
            cur.execute(migration.path.read_text(encoding="utf-8"))
            after = schema_additions(cur)
            declared = {
                "column": {f"{table}.{column}" for table, column in migration.columns}
                | {f"{table}.{column}"
                   for table, columns in migration.tables.items() for column in columns},
                "index": set(migration.indexes),
                "rls": set(migration.rls),
            }
            for kind in ("column", "index", "rls"):
                added = after[kind] - before[kind]
                problems += [f"{migration.name}: adds {kind} {item}, which the manifest "
                             "does not declare" for item in sorted(added - declared[kind])]
                problems += [f"{migration.name}: declares {kind} {item}, which this file "
                             "does not add" for item in sorted(declared[kind] - added)]
            before = after
    assert not problems, (
        "migrations/0000_manifest.json does not describe the migrations:\n  "
        + "\n  ".join(problems)
    )


# ---------------------------------------- 9. upgrade from an existing main schema
def apply_like_the_previous_runner(dsn: str, base: pathlib.Path) -> None:
    """Write a database the way ``apply_migrations`` did before this module.

    Every ``*.sql`` in name order, executed and recorded as ``sha256(text)``, with
    no manifest, no history check and no effect check. Every database that
    predates the runner was written like this, so it is the honest starting point
    for an upgrade test; calling the new runner twice is not. The checksum is
    computed with ``hashlib`` here rather than through the runner on purpose: if
    the runner ever hashed differently, existing databases would stop matching
    and this is where it has to show.
    """
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  filename text PRIMARY KEY,"
            "  checksum text NOT NULL,"
            "  applied_at timestamptz NOT NULL DEFAULT now())"
        )
        for path in sorted(base.glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_migrations(filename, checksum) VALUES (%s, %s)",
                (path.name, hashlib.sha256(sql.encode("utf-8")).hexdigest()),
            )


def test_upgrading_a_main_schema_database_converges(schema_dsn):
    """A database written by the previous runner upgrades without re-running.

    The main-schema database is reproduced the way it exists in the wild: every
    migration executed and recorded by the previous runner's algorithm, with no
    manifest involved. Then the server starts on this code. The upgrade must
    recognise the history, run nothing, rewrite nothing, and report the schema
    valid.
    """
    _, dsn = schema_dsn
    apply_like_the_previous_runner(dsn, REPO_MIGRATIONS)
    before = recorded(dsn)
    assert len(before) == len(list(REPO_MIGRATIONS.glob("*.sql")))
    # Restart on this code: a new store runs apply_migrations in its constructor.
    store = PostgresStore(dsn)
    try:
        assert store.apply_migrations() == [], "the upgrade must not re-execute anything"
        missing = store.verify_schema()
    finally:
        store.close()
    assert missing == [], f"schema verification after upgrade: {missing}"
    assert recorded(dsn) == before


# ------------------------------------ effects that tables and columns cannot see
def test_a_missing_index_is_caught(schema_dsn, tmp_migrations):
    """P0's workspace exclusivity rests on an index, so an absent one is fatal.

    A runner that only checked tables would report this database healthy while
    the invariant it depends on was missing — exactly the gap this case closes.
    """
    _, dsn = schema_dsn
    add_migration(tmp_migrations, "0001_plain.sql", "CREATE TABLE t (id int);")
    write_manifest(tmp_migrations, indexes={"0001_plain.sql": ["uq_t_expected"]})
    with pytest.raises(MigrationError) as exc:
        migrate(dsn, tmp_migrations)
    assert exc.value.kind == "effect_missing"
    assert "uq_t_expected" in str(exc.value)


def test_a_declared_index_is_satisfied_when_it_exists(schema_dsn, tmp_migrations):
    """The positive half: the same declaration passes once the index is created."""
    _, dsn = schema_dsn
    add_migration(tmp_migrations, "0001_indexed.sql",
                  "CREATE TABLE t (id int, k text);\n"
                  "CREATE UNIQUE INDEX IF NOT EXISTS uq_t_k ON t (k);")
    write_manifest(tmp_migrations, indexes={"0001_indexed.sql": ["uq_t_k"]})
    assert migrate(dsn, tmp_migrations) == ["0001_indexed.sql"]


def test_rls_not_enabled_is_caught(schema_dsn, tmp_migrations):
    """A migration that was supposed to lock a table down but did not."""
    _, dsn = schema_dsn
    add_migration(tmp_migrations, "0001_plain.sql", "CREATE TABLE secrets (id int);")
    write_manifest(tmp_migrations, rls={"0001_plain.sql": ["secrets"]})
    with pytest.raises(MigrationError) as exc:
        migrate(dsn, tmp_migrations)
    assert exc.value.kind == "effect_missing"
    assert "secrets" in str(exc.value)


def test_real_index_and_rls_effects_are_verified_on_bootstrap(schema_dsn):
    """On the real tree: 0015's index and 0016's RLS are both post-conditions."""
    _, dsn = schema_dsn
    migrate(dsn, REPO_MIGRATIONS)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() "
                    "AND indexname = 'uq_tasks_workspace_active'")
        assert cur.fetchone() is not None, "0015's index is a declared effect"
        cur.execute("SELECT relname, relrowsecurity FROM pg_class "
                    "WHERE relname IN ('conversation_routes','cloud_sandboxes') "
                    "AND relnamespace = current_schema()::regnamespace ORDER BY 1")
        flags = dict(cur.fetchall())
        assert flags == {"cloud_sandboxes": True, "conversation_routes": True}, flags
