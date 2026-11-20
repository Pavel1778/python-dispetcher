"""Replay the Phase 2 schema upgrade, only on a fresh private test cluster.

From the candidate checkout, under tools/with_disposable_postgres.sh:
    python reports/nexus3/dispatch/upgrade_probe.py /path/to/base-archive /tmp/upgrade.json
The base is a trusted git archive of the #54 merge; see README.md.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

import psycopg

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
BASE = 'e866666fb69a13e0edc0c7670b9f56e3561b34c0'
NEW = '0043_lab_dispatch.sql'


def catalog(dsn):
    return json.loads(subprocess.check_output(
        ['psql', dsn, '-XqAt', '-v', 'ON_ERROR_STOP=1', '-f', 'reports/nexus3/catalog.sql'], text=True))


def counters(c):
    return {k: sum(t[k] for t in c['tables'].values()) for k in ('columns', 'indexes', 'foreign_keys')} | {
        'tables': len(c['tables']), 'migrations': len(c['migrations'])}


def marker(store):
    with store._tx() as cur:
        cur.execute('SELECT t.id,t.state,t.attempt,t.lease_token,t.lease_owner,t.lease_expires,t.pending_start, '
                    'r.iteration,r.total_iterations FROM nexus_run_tasks t JOIN nexus_runs r ON r.id=t.run_id')
        (row,) = cur.fetchall()
        return row


def main():
    from benchmarks.forensic_workload import private_database
    from orchestration.cloud_store import PostgresStore
    from orchestration.nexus_lab_store import LabStore

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('base', type=Path)
    ap.add_argument('out', type=Path)
    args = ap.parse_args()
    if (args.base / '.git-archive-commit').read_text().strip() != BASE:
        ap.error('expected a verified archive of the #54 merge')
    dsn = private_database()  # also refuses a nonempty DB or a supplied TCP host
    with psycopg.connect(dsn) as conn:
        for role in ('anon', 'authenticated'):
            conn.execute('CREATE ROLE ' + role + ' NOLOGIN')
    rid = subprocess.check_output([sys.executable, '-c', '''
import os
from orchestration.cloud_store import PostgresStore
from orchestration.nexus_projects import ProjectService
from orchestration.nexus_lab_store import LabStore
s = PostgresStore(os.environ['TEST_SUPABASE_DB_URL'])
try:
    svc, lab = ProjectService(s), LabStore(s)
    p = svc.create('audit', 'upgrade')['id']
    g = svc.create_goal('audit', p, 'test receipt')[0]['id']
    r = lab.start_run('audit', p, g, plan=[{'id':'one','title':'write'}])['id']
    t = lab.claim_task(r, 'legacy')
    assert lab.record_start(t['id'], start={'start_task_id':'receipt-on-old-binary','kind':'start'},
                            lease_token=t['lease_token'])
    with s._tx() as c:
        c.execute("UPDATE nexus_run_tasks SET lease_expires=now()-interval '1 minute' WHERE id=%s", (t['id'],))
    print(r)
finally:
    s.close()
'''], text=True, cwd=args.base).strip()
    store = PostgresStore(dsn, auto_migrate=False)
    try:
        before, old = marker(store), catalog(dsn)
        ref = json.loads((ROOT / 'reports/nexus3/evidence/catalog.json').read_text())
        assert old['migrations'] == ref['migrations'] and old['tables'] == ref['tables']
        assert store.apply_migrations() == [NEW] and store.apply_migrations() == []
        assert store.verify_schema() == []
        new = catalog(dsn)
        assert new['migrations'] == {**old['migrations'], NEW: new['migrations'][NEW]}
        assert all(new['tables'][n] == t for n, t in old['tables'].items())
        added = sorted(new['tables'].keys() - old['tables'].keys())
        assert added == ['nexus_dispatch_intents', 'nexus_run_attestations_due', 'nexus_task_attestations_due']
        with store._tx() as cur:
            cur.execute("SELECT r.rolname AS role,c.relname AS rel, "
                        "has_table_privilege(r.oid,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') "
                        "AS any_privilege FROM pg_roles r CROSS JOIN pg_class c "
                        "WHERE r.rolname IN ('anon','authenticated') AND c.relname=ANY(%s) ORDER BY 1,2", (added,))
            acl = cur.fetchall()
        assert all(new['tables'][t]['rls'] for t in added) and len(acl) == 6 and not any(r['any_privilege'] for r in acl)
        lab = LabStore(store)
        assert not lab.reclaim_expired(rid) and lab.claim_task(rid, 'contender') is None
        legacy, = lab.pending_starts(rid, legacy_only=True)
        assert legacy['pending_start']['start_task_id'] == 'receipt-on-old-binary' and marker(store) == before
    finally:
        store.close()
    result = {'base': BASE, 'candidate': subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip(),
              'postgres': new['postgres'], 'base_counts': counters(old), 'candidate_counts': counters(new),
              'base_catalog_matches_reference': True, 'existing_table_shapes_and_migration_hashes_unchanged': True,
              'first_apply': [NEW], 'second_apply': [], 'verify_schema_missing': [], 'migration_sha256': new['migrations'][NEW],
              'new_tables': {t: new['tables'][t] for t in added}, 'role_checks': acl,
              'old_expired_pending_start': {'seen_by_recovery': True, 'reclaimed': False, 'second_grant': False,
                                           'state_lease_attempt_budget_and_receipt_unchanged': True}}
    with args.out.open('x') as out:
        json.dump(result, out, indent=2)
        out.write('\n')


if __name__ == '__main__':
    main()
