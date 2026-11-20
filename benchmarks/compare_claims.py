#!/usr/bin/env python3
"""Measure *committed grants*, not completed jobs, on clones of one PG fixture.

Build the release claim_probe example, export NEXUS_CLAIM_PROBE, then run via
with_disposable_postgres.sh. CI may also use its ephemeral local PG service.
No production DSN, schema resets, Rust toolchain/stubs or network in the drivers.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@contextmanager
def clone(admin, template):
    name = 'nexus_test_' + secrets.token_hex(6)
    admin.execute(sql.SQL('CREATE DATABASE {} TEMPLATE {}').format(sql.Identifier(name), sql.Identifier(template)))
    try:
        yield name
    finally:
        # Never terminate other sessions or drop a DB we did not create.
        admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))


def python_sample(run, count):
    from orchestration.cloud_store import PostgresStore
    from orchestration.nexus_lab_store import LabStore

    if not (re.fullmatch(r'nexus_test_[0-9a-f]{12}', os.environ['PGDATABASE']) and 1 <= count <= 500
            and (os.environ['PGHOST'] == '127.0.0.1' or os.environ['PGHOST'].startswith('/tmp/nexus-audit.'))):
        raise SystemExit('isolated local comparison database required')
    store = PostgresStore(make_conninfo(dbname=os.environ['PGDATABASE']), auto_migrate=False)
    try:
        lab = LabStore(store)
        with store._tx() as cur:
            cur.execute('SELECT current_database() AS db')
            assert cur.fetchone()['db'] == os.environ['PGDATABASE']
        seen = set()
        samples = []
        start = time.monotonic_ns()
        for _ in range(count):
            t = time.monotonic_ns()
            grant = lab.claim_task(run, 'bench', lease_seconds=900)
            samples.append(time.monotonic_ns() - t)
            assert grant and grant['lease_token'] == 1 and grant['id'] not in seen
            seen.add(grant['id'])
        print(json.dumps({'wall_ns': time.monotonic_ns() - start, 'samples_ns': samples}))
    finally:
        store.close()


def seed(dsn, count):
    from orchestration.cloud_store import PostgresStore
    from orchestration.nexus_lab_store import LabStore
    from orchestration.nexus_projects import ProjectService

    store = PostgresStore(dsn)
    try:
        projects = ProjectService(store)
        pid = projects.create('bench', 'Grant comparison')['id']
        goal, _ = projects.create_goal('bench', pid, 'Measure')
        rid = LabStore(store).start_run('bench', pid, goal['id'], max_iterations=count,
                                        plan=[{'id': str(i), 'title': 'work'} for i in range(count)])['id']
        with store._tx() as cur:
            cur.execute('ANALYZE nexus_runs, nexus_run_tasks, nexus_executor_stops')
        return str(pid), str(rid)
    finally:
        store.close()


def compare(dsn, probe, count, rounds):
    params = conninfo_to_dict(dsn)
    env = {'PATH': os.environ['PATH'], 'LANG': 'C.UTF-8', 'NEXUS_LAB_MAX_ACTIVE_TASKS': '0',
           'NEXUS_DB_POOL_MIN': '1', 'NEXUS_DB_POOL_MAX': '1'}
    for key in ('user', 'port', 'host', 'password'):
        if key in params:
            env['PG' + key.upper()] = params[key]
    results = []
    with psycopg.connect(make_conninfo(dsn, dbname='postgres'), autocommit=True) as admin, \
            clone(admin, 'template0') as template, tempfile.TemporaryDirectory() as folder:
        pid, rid = seed(make_conninfo(dsn, dbname=template), count)
        for i in range(rounds):
            for driver in (('python', 'rust') if i % 2 == 0 else ('rust', 'python')):
                with clone(admin, template) as db:
                    command = ([sys.executable, __file__, 'python-sample', rid, str(count)] if driver == 'python'
                               else [probe, 'sample', pid, rid, str(count)])
                    metrics = Path(folder, 'time')
                    child = subprocess.run(['/usr/bin/time', '-f', '%U %S %M', '-o', str(metrics), *command],
                                           env={**env, 'PGDATABASE': db}, capture_output=True,
                                           text=True, timeout=90, check=True)
                    raw = json.loads(child.stdout)
                    s = sorted(raw['samples_ns'])
                    assert len(s) == count
                    # Check persisted rows independently, not just the driver's return value.
                    with psycopg.connect(make_conninfo(dsn, dbname=db)) as conn:
                        assert conn.execute("SELECT count(*) FROM nexus_run_tasks WHERE run_id=%s AND "
                                            "state='dispatched' AND attempt=1 AND lease_owner='bench' "
                                            "AND lease_token=1 AND lease_expires IS NOT NULL", (rid,)).fetchone() == (count,)
                        assert conn.execute('SELECT iteration,total_iterations FROM nexus_runs WHERE id=%s',
                                            (rid,)).fetchone() == (count, count)
                    user, system, rss = metrics.read_text().split()
                    results.append({'round': i + 1, 'driver': driver, 'grants': count,
                                    'grants_s': round(count * 1e9 / raw['wall_ns'], 1),
                                    **{f'p{p}_ms': round(s[(count * p + 99) // 100 - 1] / 1e6, 3)
                                       for p in (50, 95, 99)}, 'child_cpu_user_s': float(user),
                                    'child_cpu_system_s': float(system), 'child_max_rss_kib': int(rss)})
        version = admin.info.server_version
    return {'head': subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
            'pr_head': os.environ.get('COMPARISON_HEAD'), 'postgres': version, 'python': sys.version.split()[0],
            'dataset': 'same cloned UUIDs; independent tasks, empty repo, no global active cap; one warm connection, '
                       'statement caches off; SQL transaction only. Rust returns id/token, Python full task row.',
            'resources': 'GNU time of whole fresh driver process including startup/teardown, not PG or the host',
            'trials': results}


def main():
    if sys.argv[1:2] == ['python-sample']:
        return python_sample(sys.argv[2], int(sys.argv[3]))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--tasks', type=int, default=200)
    ap.add_argument('--rounds', type=int, default=4)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args()
    if not (1 <= args.tasks <= 500 and args.rounds in (2, 4, 6, 8)):
        ap.error('tasks 1..500; even rounds 2..8')
    probe = os.environ.get('NEXUS_CLAIM_PROBE')
    if not probe or not os.access(probe, os.X_OK):
        ap.error('NEXUS_CLAIM_PROBE must name a built executable')
    dsn = os.environ.get('TEST_SUPABASE_DB_URL', '')
    params = conninfo_to_dict(dsn)
    if not (os.getenv('GITHUB_ACTIONS') == 'true' and os.getenv('RUNNER_ENVIRONMENT') == 'github-hosted'
            and params.get('host') == '127.0.0.1' and params.get('dbname') == 'orchestrator'):
        from forensic_workload import private_database
        dsn = private_database()
    report = compare(dsn, probe, args.tasks, args.rounds)
    args.out.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, separators=(',', ':')))


if __name__ == '__main__':
    main()
