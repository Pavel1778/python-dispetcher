"""Additive upgrade, live lease, old reader, role checks and dump/restore. Private cluster ONLY."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import psycopg
from psycopg.conninfo import make_conninfo

sys.path.insert(0, str(Path.cwd()))
from benchmarks.forensic_workload import private_database  # noqa: E402
from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.execution_profiles import ExecutionProfiles  # noqa: E402
from reports.nexus3.dispatch.upgrade_probe import catalog, counters, marker  # noqa: E402

BASE = "20eb8bffc3e89f08e24522aa98dba0e62fc6e070"
MIG = "0044_execution_profiles.sql"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("base", type=Path)
    ap.add_argument("out", type=Path)
    args = ap.parse_args()
    assert (args.base / ".git-archive-commit").read_text().strip() == BASE
    dsn = private_database()
    with psycopg.connect(dsn, autocommit=True) as c:
        for role in ("anon", "authenticated"):
            c.execute("CREATE ROLE " + role + " NOLOGIN")
        c.execute("CREATE DATABASE nexus_restore_probe")
    old_reader = "import os; from orchestration.cloud_store import PostgresStore; from orchestration.nexus_projects import ProjectService; from orchestration.nexus_lab_store import LabStore; s=PostgresStore(os.environ['TEST_SUPABASE_DB_URL']); lab=LabStore(s); projects=ProjectService(s);"
    subprocess.run(
        [
            sys.executable,
            "-c",
            old_reader
            + "p=projects.create('audit','upgrade')['id'];g=projects.create_goal('audit',p,'seed')[0]['id'];r=lab.start_run('audit',p,g,plan=[{'id':'one','title':'one'}])['id'];t=lab.claim_task(r,'prior',dispatch_intent=True);assert t['_intent_id'];s.close()",
        ],
        cwd=args.base,
        check=True,
    )
    store = PostgresStore(dsn, auto_migrate=False)
    try:
        before = marker(store)
        old = catalog(dsn)
        assert (
            store.apply_migrations() == [MIG]
            and store.apply_migrations() == []
            and store.verify_schema() == []
        )
        new = catalog(dsn)
        assert new["migrations"] == {**old["migrations"], MIG: new["migrations"][MIG]}
        assert new["tables"].keys() - old["tables"].keys() == {
            "nexus_execution_profiles"
        }
        assert all(new["tables"][n] == tbl for n, tbl in old["tables"].items())
        assert new["tables"]["nexus_execution_profiles"]["rls"]
        with store._tx() as cur:
            cur.execute(
                "SELECT rolname,has_table_privilege(oid,'nexus_execution_profiles','SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS any_grant FROM pg_roles WHERE rolname IN ('anon','authenticated') ORDER BY 1"
            )
            roles = list(cur.fetchall())
        assert len(roles) == 2 and not any(r["any_grant"] for r in roles)
        svc = ExecutionProfiles(store)
        # A synthetic HANDLE only; no runtime credential or external URL is contacted.
        desired = dict(
            provider="test",
            model="test/restore",
            base_url="https://model.example/v1",
            key_ref="b" * 32,
            params={},
            agent_profile_id="00000000-0000-4000-8000-000000000002",
            connection_id=None,
        )
        row = svc.save("audit", desired, operator=True)
        svc.observe(row, "saved", "restore probe only")
        state = svc.effective()
        assert marker(store) == before
        subprocess.run(
            [
                sys.executable,
                "-c",
                old_reader
                + "assert s.verify_schema()==[] and projects.list('audit')[0]['name']=='upgrade';s.close()",
            ],
            cwd=args.base,
            check=True,
        )
        target = make_conninfo(dsn, dbname="nexus_restore_probe")
        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / "database.dump"
            subprocess.run(
                ["pg_dump", dsn, "--format=c", "--file", str(dump)], check=True
            )
            subprocess.run(
                ["pg_restore", "--exit-on-error", "--dbname", target, str(dump)],
                check=True,
            )
            size, digest = (
                dump.stat().st_size,
                hashlib.sha256(dump.read_bytes()).hexdigest(),
            )
        restored = PostgresStore(target, auto_migrate=False)
        try:
            assert (
                ExecutionProfiles(restored).effective() == state
                and marker(restored) == before
                and restored.verify_schema() == []
            )
        finally:
            restored.close()
        data = dict(
            base=BASE,
            candidate=subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            postgres=new["postgres"],
            before=counters(old),
            after=counters(new),
            new_table=new["tables"]["nexus_execution_profiles"],
            roles=roles,
            migration_sha256=new["migrations"][MIG],
            repeat_apply=[],
            existing_catalog_and_journal_grant_preserved=True,
            old_reader_passed=True,
            restore_equal=True,
            dump_bytes=size,
            ephemeral_dump_sha256=digest,
        )
        args.out.write_text(json.dumps(data, indent=2) + "\n")
    finally:
        store.close()


if __name__ == "__main__":
    main()
