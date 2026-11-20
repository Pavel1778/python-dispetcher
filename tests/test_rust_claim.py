"""SQLx ↔ psycopg interoperability on the *same* disposable PostgreSQL.

The child process exits after every Rust claim. No fake repository, provider,
OpenHands key or external conversation is needed for this storage contract.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import threading

import pytest
from psycopg.conninfo import conninfo_to_dict

from orchestration.cloud_store import PostgresStore
from orchestration.nexus_lab_store import LabStore
from orchestration.nexus_projects import ProjectService

DSN = os.environ.get('TEST_SUPABASE_DB_URL')
PROBE = os.environ.get('NEXUS_CLAIM_PROBE')
pytestmark = pytest.mark.skipif(not DSN or not PROBE, reason="requires SQLx claim probe and isolated PostgreSQL")


@pytest.fixture
def queue(monkeypatch):
    from tests._test_db import reset_schema

    assert os.environ.get('NEXUS_TEST_DB_ISOLATED') == '1'
    reset_schema(DSN)
    monkeypatch.setenv('NEXUS_LAB_MAX_ACTIVE_TASKS', '8')
    store = PostgresStore(DSN)
    try:
        yield store, LabStore(store)
    finally:
        store.close()


def start(store, lab, tasks, repo='', **options):
    service = ProjectService(store)
    pid = str(service.create('user:test', 'Queue')['id'])
    goal, _ = service.create_goal('user:test', pid, 'Test claim contract')
    run = lab.start_run('user:test', pid, goal['id'], repository=repo, branch='main',
                        plan=tasks, **options)
    return pid, run['id']


def probe(*args):
    # tests._test_db produces libpq conninfo; never place a password on argv.
    params = conninfo_to_dict(DSN)
    env = {'PATH': os.environ['PATH']}
    for key, var in (('dbname', 'PGDATABASE'), ('host', 'PGHOST'), ('port', 'PGPORT'),
                     ('user', 'PGUSER'), ('password', 'PGPASSWORD')):
        if key in params:
            env[var] = params[key]
    child = subprocess.run([PROBE, *map(str, args)],
                           env=env, capture_output=True, text=True, timeout=30, check=True)
    return json.loads(child.stdout)


def rust_claim(pid, rid, worker='rust', limit=8):
    return probe('claim', pid, rid, worker, 900, limit)


def test_rust_claim_survives_process_exit_and_python_recovery(queue):
    store, lab = queue
    pid, rid = start(store, lab, [{'id': 'a', 'title': 'A'},
                                  {'id': 'b', 'title': 'B', 'dependencies': ['a']}],
                     policy='durable', max_attempts_per_task=1)
    grant = rust_claim(pid, rid)
    first = lab.get_task('user:test', pid, grant['id'])
    assert (first['task_key'], first['lease_owner'], first['lease_token'], first['attempt']) == (
        'a', 'rust', grant['lease_token'], 1)
    assert lab.claim_task(rid, 'python') is None and rust_claim(pid, rid) is None
    with store._tx() as cur:
        cur.execute("UPDATE nexus_run_tasks SET lease_expires=now()-interval '1 second' WHERE id=%s", (grant['id'],))
    (revoked,) = lab.reclaim_expired(rid)
    assert revoked['lease_token'] > grant['lease_token']
    renewed = lab.claim_task(rid, 'python')
    assert str(renewed['id']) == grant['id']
    stale = lab.record_verification(renewed['id'], verdict='completed_verified', evidence={},
                                    lease_token=grant['lease_token'])
    assert stale['state'] == 'dispatched'
    lab.record_verification(renewed['id'], verdict='completed_verified', evidence={},
                            lease_token=renewed['lease_token'])
    next_task = rust_claim(pid, rid)
    assert lab.get_task('user:test', pid, next_task['id'])['task_key'] == 'b'


def test_mixed_workers_exhaust_queue_once(queue, monkeypatch):
    store, lab = queue
    monkeypatch.setenv('NEXUS_LAB_MAX_ACTIVE_TASKS', '0')
    pid, rid = start(store, lab, [{'id': f't{i}', 'title': 'work'} for i in range(12)])
    expected = {str(t['id']) for t in lab.list_tasks_internal(rid)}
    barrier = threading.Barrier(6, timeout=20)

    def drain(worker):
        claims = []
        for _ in range(2):
            barrier.wait()
            t = (lab.claim_task(rid, f'py-{worker}') if worker % 2 else
                 rust_claim(pid, rid, worker=f'rs-{worker}', limit=0))
            assert t is not None
            claims.append(str(t['id']))
        return claims

    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = [i for batch in pool.map(drain, range(6)) for i in batch]
    assert set(ids) == expected and len(ids) == len(expected) == 12
    assert lab.claim_task(rid, "python") is None and rust_claim(pid, rid, limit=0) is None
    with store._tx() as cur:
        cur.execute('SELECT iteration,total_iterations FROM nexus_runs WHERE id=%s', (rid,))
        assert cur.fetchone() == {'iteration': 12, 'total_iterations': 12}


def test_cross_project_branch_and_capacity_are_global(queue, monkeypatch):
    store, lab = queue
    one = [{'id': 't', 'title': 'write'}]
    pa, a = start(store, lab, one, repo='org/same')
    pb, b = start(store, lab, one, repo='org/same')
    assert rust_claim(pb, a) is None  # right run, wrong project
    # Separate run locks: only the shared branch lock prevents two writers.
    monkeypatch.setenv('NEXUS_LAB_MAX_ACTIVE_TASKS', '0')
    with ThreadPoolExecutor(max_workers=2) as pool:
        rs = pool.submit(rust_claim, pa, a, 'rust', 0)
        py = pool.submit(lab.claim_task, b, 'python')
        got = [rs.result(), py.result()]
    assert sum(t is not None for t in got) == 1
    claimed = next(t for t in got if t)
    other_pid, other_run = (pb, b) if got[0] else (pa, a)
    assert rust_claim(other_pid, other_run, limit=0) is None
    pc, c = start(store, lab, one, repo='org/other')
    assert rust_claim(pc, c, limit=1) is None   # independent branch but global cap
    row = lab.get_task_internal(claimed['id'])
    lab.request_stop(row, 'stopping-conversation', reason='switch')
    lab.release_task(row['id'], error='requeue', requeue=True, lease_token=row['lease_token'])
    assert rust_claim(other_pid, other_run) is None  # durable requested stop
    lab.stop_result(lab.stop_for('stopping-conversation')['id'], confirmed=True)
    assert rust_claim(other_pid, other_run) is not None


def test_failed_task_update_rolls_back_the_run_budget(queue):
    store, lab = queue
    pid, rid = start(store, lab, [{'id': 't', 'title': 'Overflow guard'}])
    # A real DB error late in the transaction, after iteration was incremented.
    with store._tx() as cur:
        cur.execute('UPDATE nexus_run_tasks SET lease_token=9223372036854775807 WHERE run_id=%s', (rid,))
    with pytest.raises(subprocess.CalledProcessError):
        rust_claim(pid, rid)
    with store._tx() as cur:
        cur.execute('SELECT iteration,total_iterations FROM nexus_runs WHERE id=%s', (rid,))
        assert cur.fetchone() == {'iteration': 0, 'total_iterations': 0}
    (task,) = lab.list_tasks_internal(rid)
    assert (task['state'], task['lease_owner'], task['attempt']) == ('pending', None, 0)


@pytest.mark.parametrize(('policy', 'budget'), [('bounded', 1), ('durable', 1),
                                               ('bounded', 2), ('durable', 2)])
def test_expired_attempts_match_committed_python_state(queue, policy, budget):
    store, lab = queue
    fields = ('state', 'lease_token', 'lease_expires', 'lease_owner', 'attempt', 'cycle',
              'tried_accounts', 'evidence', 'last_error', 'pending_start')

    def fixture(recover):
        pid, rid = start(store, lab, [{'id': 'a', 'title': 'A'}],
                         policy=policy, max_attempts_per_task=budget)
        task = lab.claim_task(rid, 'worker')
        assert lab.record_start(task['id'], start={'start_task_id': 'accepted'}, lease_token=task['lease_token'])
        with store._tx() as cur:
            cur.execute("UPDATE nexus_run_tasks SET lease_expires=now()-interval '1 second', "
                        "tried_accounts='[\"fixture\"]'::jsonb WHERE id=%s", (task['id'],))
        assert recover(pid, rid) == [str(task['id'])]
        assert not recover(pid, rid) and not lab.pending_starts(rid)
        row = lab.get_task_internal(task['id'])
        assert row['lease_token'] > task['lease_token'] and row['lease_owner'] is None
        late = lab.mark_running(task['id'], conversation_id='late-' + str(rid),
                                lease_token=task['lease_token'])
        assert late['conversation_id'] is None and lab.stop_for('late-' + str(rid))['state'] == 'requested'
        return {f: row[f] for f in fields}

    expected = fixture(lambda _, rid: [str(t['id']) for t in lab.reclaim_expired(rid)])
    observed = fixture(lambda pid, rid: probe('reclaim', pid, rid, 4))
    assert observed == expected


def test_sweep_is_bounded_scoped_and_never_requeues_bound_conversation(queue):
    store, lab = queue
    pid, rid = start(store, lab, [{'id': f't{i}', 'title': 'T'} for i in range(5)])
    other, _ = start(store, lab, [{'id': 'other', 'title': 'T'}])
    tasks = [lab.claim_task(rid, 'python') for _ in range(5)]
    bound, live, *lost = tasks
    lab.mark_running(bound['id'], conversation_id='in-cloud', lease_token=bound['lease_token'])
    with store._tx() as cur:
        cur.execute("UPDATE nexus_run_tasks SET lease_expires=now()-interval '1 second' "
                    "WHERE run_id=%s AND id<>%s", (rid, live['id']))
    assert not probe('reclaim', other, rid, 20)
    first = probe('reclaim', pid, rid, 2)
    second = probe('reclaim', pid, rid, 2)
    assert len(first) == 2 and len(second) == 1 and not probe('reclaim', pid, rid, 2)
    assert set(first + second) == {str(t['id']) for t in lost}
    assert lab.get_task_internal(bound['id'])['conversation_id'] == 'in-cloud'
    assert lab.get_task_internal(live['id'])['lease_token'] == live['lease_token']
