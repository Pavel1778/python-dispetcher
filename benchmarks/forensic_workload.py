#!/usr/bin/env python3
"""Python migration oracle: profile a dependent queue or time process recovery.

Run FROM the checkout being measured, with tools/with_disposable_postgres.sh.
Use the same harness by absolute path when measuring a different revision.
queue: storage only. restart: real worker/API/PG/signatures, socket doubles for
OpenHands, GitHub and planner. Neither case measures live provider throughput.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time

import psycopg
from psycopg.conninfo import conninfo_to_dict

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))


def private_database() -> str:
    """The restart fixture clears all tables. Do not accept a user DB, even on localhost."""
    dsn = os.environ.get('TEST_SUPABASE_DB_URL', '')
    options = conninfo_to_dict(dsn)
    host = options.get('host', '')
    if not (re.fullmatch(r'/tmp/nexus-audit\.[A-Za-z0-9]{8}', host)
            and options.get('dbname') == options.get('user') == 'nexus_audit'):
        raise SystemExit('use tools/with_disposable_postgres.sh; refusing other databases')
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT current_database(), current_user, inet_server_addr(), "
                           "current_setting('data_directory'), to_regclass('public.nexus_projects')").fetchone()
        if row != ('nexus_audit', 'nexus_audit', None, host + '/data', None):
            raise SystemExit('requires an empty, socket-only scratch cluster')
    return dsn


def elapsed_ms(t0):
    return (time.monotonic() - t0) * 1000


def queue_profile(dsn, runs, depth):
    from orchestration.cloud_store import PostgresStore
    from orchestration.nexus_lab_store import LabStore
    from orchestration.nexus_projects import ProjectService

    os.environ['NEXUS_LAB_MAX_ACTIVE_TASKS'] = '8'  # exercise real global and per-branch locks
    actor = 'bench:queue'
    store = PostgresStore(dsn)
    lab = LabStore(store)
    svc = ProjectService(store)

    def start(branch, count, chain=True):
        pid = svc.create(actor, branch)['id']
        goal, _ = svc.create_goal(actor, pid, branch)
        plan = [{'id': f't{i}', 'title': f'Fixture {i}',
                 'dependencies': [f't{i-1}'] if chain and i else []} for i in range(count)]
        return lab.start_run(actor, pid, goal['id'], plan=plan, repository='fixture/queue',
                             branch=branch, max_iterations=count)['id']

    try:
        # Other runs stay visible to the global branch guard. Don't force a plan.
        for i in range(4):
            start(f'background-{i}', 250, False)
        rid = start('query-probe', depth)
        ids = [start(f'dag-{i}', depth) for i in range(runs)]
        with store._tx() as cur:
            for table in ('nexus_run_tasks', 'nexus_runs', 'nexus_executor_stops'):
                cur.execute(f'ANALYZE {table}')
        calls = []
        candidate = []

        # Interpose only for this one grant, not for the timed concurrent runs.
        # Capture the real SELECT, rather than explaining a simplified lookalike.
        class TracedStore:
            @contextmanager
            def _tx(self):
                with store._tx() as cur:
                    class Cursor:
                        def execute(self, sql, params=None):
                            t0 = time.monotonic()
                            result = cur.execute(sql, params)
                            calls.append({'statement': ' '.join(sql.split()),
                                          'execute_ms': round(elapsed_ms(t0), 4)})
                            if 'FOR UPDATE SKIP LOCKED' in sql:
                                candidate.append((sql, params))
                            return result

                        def __getattr__(self, name):
                            return getattr(cur, name)
                    yield Cursor()

        grant = LabStore(TracedStore()).claim_task(rid, 'probe')
        assert grant and len(candidate) == 1
        lab.release_task(grant['id'], error='probe finished', requeue=True, lease_token=grant['lease_token'])
        with store._tx() as cur:
            sql, params = candidate[0]
            cur.execute('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + sql, params)
            plan = cur.fetchone()['QUERY PLAN'][0]

        barrier = threading.Barrier(runs, timeout=20)

        def finish_chain(item):
            num, run = item
            barrier.wait()
            claims = []
            t0 = time.monotonic()
            for i in range(depth):
                t = lab.claim_task(run, f'worker-{num}')
                if not t or t['task_key'] != f't{i}' or t['attempt'] != 1:
                    raise AssertionError('lost, repeated, or out-of-order DAG claim')
                result = lab.record_verification(t['id'], verdict='completed_no_changes',
                                                 evidence={'benchmark_fixture': True},
                                                 lease_token=t['lease_token'])
                assert result['state'] == 'completed_no_changes'
                claims.append(str(t['id']))
            assert lab.claim_task(run, 'extra') is None
            return claims, elapsed_ms(t0)

        t0 = time.monotonic()
        cpu = time.process_time()
        with ThreadPoolExecutor(max_workers=runs) as pool:
            completed = list(pool.map(finish_chain, enumerate(ids)))
        wall = time.monotonic() - t0
        cpu_s = time.process_time() - cpu
        all_ids = [task for claim_ids, _ in completed for task in claim_ids]
        assert len(set(all_ids)) == len(all_ids) == runs * depth
        return {'runs': runs, 'chain_depth': depth, 'background_tasks': 1000,
                'global_limit': 8, 'successful_fixture_settlements': len(all_ids),
                'wall_s': round(wall, 4), 'process_cpu_s': round(cpu_s, 4),
                'per_run_ms': [round(ms, 2) for _, ms in completed],
                'fixture_settlements_per_second': round(len(all_ids) / wall, 2),
                'claim_statement_count_excluding_begin_commit': len(calls),
                'one_claim_statements': calls, 'candidate_select_plan_after_release': plan}
    finally:
        store.close()


def restart_profile(repeat):
    import pytest
    from orchestration.evidence_trust import VERIFIED, EvidenceTrustEngine, Keyring, generate_signer, public_key_b64
    from orchestration.nexus_lab_store import LabStore
    from tests.test_lab_inline import HEAD, HEAD2, _finish, _start_run, _starts, _until
    from tests.test_lab_worker_process import Worker
    from tests.test_studio_api import studio_stack

    trials = []
    for _ in range(repeat):
        with pytest.MonkeyPatch.context() as env:
            signer = generate_signer('bench')
            ring = Keyring()
            ring.add('bench', public_key_b64(signer))
            verifier = EvidenceTrustEngine(ring)
            env.setenv('NEXUS_EVIDENCE_PRIVATE_KEY', base64.urlsafe_b64encode(
                signer.private_key.private_bytes_raw()).decode())
            env.setenv('NEXUS_EVIDENCE_KEYRING', 'bench:' + public_key_b64(signer))
            env.setenv('NEXUS_EVIDENCE_KEY_ID', 'bench')
            with studio_stack(env, lab_inline=False) as studio:
                lab = LabStore(studio.app.store)
                t0 = time.monotonic()
                pid, rid = _start_run(studio)

                def bound(key):
                    return next((t['conversation_id'] for t in lab.list_tasks_internal(rid)
                                 if t['task_key'] == key and t['conversation_id']), None)

                worker = Worker(studio, worker_id='before-crash')
                try:
                    first = _until(lambda: bound('audit'), what='first durable binding')
                    dispatch_ms = elapsed_ms(t0)
                    assert not bound('change') and len(_starts(studio)) == 1
                    assert worker.stop(sig=signal.SIGKILL) == -signal.SIGKILL
                    _finish(studio, first, f'Commit {HEAD}.')
                    restart = time.monotonic()
                    worker = Worker(studio, worker_id='after-crash')
                    child = _until(lambda: bound('change'), what='recovered worker to dispatch child')
                    resume_ms = elapsed_ms(restart)
                    _finish(studio, child, f'Commit {HEAD2}.')
                    _until(lambda: lab.run_row(rid)['state'] == 'completed', what='completed run')
                    rows = _until(lambda: (lambda rs: rs if len(rs) >= 3 else None)(
                        studio.app.store.evidence_for_project(pid)), what='both task signatures and acceptance')
                    wall = time.monotonic() - t0
                    assert len(rows) == 3 and verifier.index_integrity(list(reversed(rows)))['intact']
                    tasks = lab.list_tasks_internal(rid)
                    heads = {'audit': HEAD, 'change': HEAD2}
                    for t in tasks:
                        assert t['state'] == 'completed_verified' and t['attempt'] == 1
                        (rec,) = [r for r in rows if r['subject_id'] == str(t['id'])]
                        assert verifier.verify(rec, expected_commit=heads[t['task_key']]).verdict == VERIFIED
                    (decision,) = [r for r in rows if r['subject_id'] == str(rid)]
                    assert verifier.verify(decision).verdict == VERIFIED
                    assert decision['body']['detail']['decision'] == 'ACCEPTED'
                    assert len(tasks) == len(_starts(studio)) == 2
                    trials.append({'first_bound_ms': round(dispatch_ms, 2),
                                   'relaunch_to_child_bound_ms': round(resume_ms, 2),
                                   'request_to_signed_acceptance_s': round(wall, 4),
                                   'starts': 2, 'signature_checked_tasks': 2})
                finally:
                    worker.stop()
    return {'worker_poll_seconds': 0.3, 'observed_poll_seconds': 0.05, 'trials': trials,
            'fixture_verified_tasks_per_hour_extrapolated': round(
                3600 * sum(t['signature_checked_tasks'] for t in trials)
                / sum(t['request_to_signed_acceptance_s'] for t in trials), 1),
            'scope': 'two-task fixture DAG with SIGKILL; socket doubles, no live cloud time or project SelfQA'}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('case', choices=['queue', 'restart'])
    ap.add_argument('--runs', type=int, choices=range(1, 9), default=4)
    ap.add_argument('--depth', type=int, default=40)
    ap.add_argument('--repeat', type=int, default=3)
    ap.add_argument('--out', required=True, type=Path)
    args = ap.parse_args()
    if not (1 <= args.depth <= 500 and 1 <= args.repeat <= 20):
        ap.error('depth must be 1..500 and repeat 1..20')
    dsn = private_database()
    with psycopg.connect(dsn) as conn:
        pg_version = conn.info.server_version
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    out = {'implementation_sha': sha, 'python': sys.version.split()[0], 'postgres': pg_version,
           'case': args.case, 'results': (queue_profile(dsn, args.runs, args.depth)
                                         if args.case == 'queue' else restart_profile(args.repeat))}
    args.out.write_text(json.dumps(out, indent=2) + '\n', encoding='utf-8')
    print(args.out)


if __name__ == '__main__':
    main()
