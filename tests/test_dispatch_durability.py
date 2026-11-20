"""Provider-observed starts, real SIGKILL, PostgreSQL, independently signed DAG."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import signal
import subprocess
import sys
from threading import Barrier

import pytest

from orchestration.cloud_client import CloudClient
from orchestration.evidence_trust import EvidenceTrustEngine, Keyring, VERIFIED
from orchestration.lab_worker import active_run_ids
from orchestration.nexus_lab import OpenHandsExecutor
from orchestration.nexus_lab_store import LabStore
from tests.fake_cloud import FakeCloudServer
from tests.test_durable_runs import env, scheduler, sql, start  # noqa: F401
from tests.test_lab_evidence import _signer_env
from tests.test_lab_inline import HEAD, HEAD2, _finish, _start_run, _starts, _until
from tests.test_lab_worker_process import Worker, _worker_env
from tests.test_start_handover import _bound, _rows
from tests.test_studio_api import DSN, studio_stack

pytestmark = pytest.mark.skipif(not DSN, reason='requires isolated PostgreSQL')


def _verify_dag(studio, project, rid):
    lab = LabStore(studio.app.store)
    _until(lambda: lab.run_row(rid)['state'] == 'completed', what='completed DAG')
    rows = _until(lambda: (lambda records: records if len(records) >= 3 else None)(
        studio.app.store.evidence_for_project(project)), what='task and run signatures')
    engine = EvidenceTrustEngine(Keyring.from_env())
    assert len(rows) == 3 and engine.index_integrity(list(reversed(rows)))['intact']
    expected = {'audit': HEAD, 'change': HEAD2}
    for task in lab.list_tasks_internal(rid):
        assert task['state'] == 'completed_verified'
        (record,) = [r for r in rows if r['subject_id'] == str(task['id'])]
        assert engine.verify(record, expected_commit=expected[task['task_key']]).verdict == VERIFIED
        assert record['body']['detail']['conversation_id'] == task['conversation_id']
    (run,) = [r for r in rows if r['subject_id'] == str(rid)]
    assert engine.verify(run).verdict == VERIFIED and run['body']['detail']['decision'] == 'ACCEPTED'
    assert _rows(studio, "SELECT state FROM nexus_dispatch_intents ORDER BY created_at") == [('bound',)] * 2
    assert _rows(studio, "SELECT outcome FROM nexus_task_executions") == [('completed_verified',)] * 2
    assert len(_starts(studio)) == 2  # not just uniqueness on a local DB column
    bodies = studio.cloud.cloud.start_bodies
    assert len(bodies) == len({b['tags']['nexusdispatch'] for b in bodies.values()}) == 2
    assert all(b['initial_message']['run'] for b in bodies.values())
    assert all(sum(e.get('llm_message', {}).get('role') == 'user' for e in studio.cloud.cloud.events[c]) == 1
               for c in bodies)


@pytest.mark.parametrize(('window', 'state', 'starts'), [
    ('before_intent_commit', None, 0), ('prepared_before_send', 'prepared', 0),
    ('accepted_before_read', 'sending', 1), ('id_before_receipt', 'sending', 1),
    ('receipt_before_bind', 'identified', 1), ('bind_before_running', 'identified', 1),
    ('verdict_before_signature', 'bound', 1), ('signature_before_ack', 'bound', 1),
    ('terminal_before_acceptance', 'bound', 2),
])
def test_worker_crash_and_two_replacements_finish_one_signed_dag(monkeypatch, window, state, starts):
    _signer_env(monkeypatch)
    with studio_stack(monkeypatch, lab_inline=False) as studio, ExitStack() as cleanup:
        project, rid = _start_run(studio)
        p = subprocess.Popen([sys.executable, '-m', 'tests.dispatch_crash_worker', window], env=_worker_env(studio))
        cleanup.callback(p.wait)
        cleanup.callback(p.kill)
        verdict_window = window in ('verdict_before_signature', 'signature_before_ack', 'terminal_before_acceptance')
        if verdict_window:
            _finish(studio, _bound(studio, 'audit')['conversation_id'], f'Done. Commit {HEAD}.')
            if starts == 2:
                _finish(studio, _bound(studio, 'change')['conversation_id'], f'Done. Commit {HEAD2}.')
        assert p.wait(timeout=20) == -signal.SIGKILL
        assert _rows(studio, 'SELECT state FROM nexus_dispatch_intents') == ([(state,)] * starts if verdict_window
                      else [(state,)] if state else [])
        assert len(_starts(studio)) == starts
        if not verdict_window:
            assert _rows(studio, "SELECT state,conversation_id FROM nexus_run_tasks WHERE task_key='audit'") == [
                ('dispatched' if state else 'pending', None)]
            if state is None:
                assert _rows(studio, 'SELECT iteration,total_iterations FROM nexus_runs') == [(0, 0)]
            _rows(studio, "UPDATE nexus_run_tasks SET lease_expires=now()-interval '1 s' "
                          "WHERE task_key='audit' RETURNING id")  # advance only the private lease clock
        else:
            assert str(rid) in active_run_ids(studio.app.store)
        for i in range(2):
            cleanup.callback(Worker(studio, worker_id=f'replacement-{i}', poll_interval=0.07).stop)
        if not verdict_window:
            first = _bound(studio, 'audit')
            assert first['attempt'] == 1
            assert len(_starts(studio)) == 1
            _finish(studio, first['conversation_id'], f'Done. Commit {HEAD}.')
        if starts != 2:
            _finish(studio, _bound(studio, 'change')['conversation_id'], f'Done. Commit {HEAD2}.')
        _verify_dag(studio, project, rid)


def test_armed_but_not_sent_is_uncertain_not_a_license_to_post(monkeypatch):
    with studio_stack(monkeypatch, lab_inline=False) as studio:
        _, rid = _start_run(studio)
        child = subprocess.run([sys.executable, '-m', 'tests.dispatch_crash_worker', 'armed_before_send'],
                               env=_worker_env(studio), timeout=20)
        assert child.returncode == -signal.SIGKILL
        _rows(studio, "UPDATE nexus_run_tasks SET lease_expires=now()-interval '1 s' RETURNING id")
        with ExitStack() as cleanup:
            for i in range(2):
                cleanup.callback(Worker(studio, worker_id=f'contender-{i}', poll_interval=0.05).stop)
            _until(lambda: len([1 for method, path in studio.cloud.cloud.calls if method == 'GET' and
                                path == '/api/v1/app-conversations/search']) >= 4, what='repeated read-only lookup')
            assert _rows(studio, 'SELECT state FROM nexus_dispatch_intents') == [('uncertain',)]
            assert not _starts(studio) and not studio.cloud.cloud.start_bodies
            assert LabStore(studio.app.store).claim_task(rid, 'third') is None


def test_late_response_after_takeover_and_concurrent_reconcile(env, monkeypatch):  # noqa: F811
    rid = start(env, [{'task_key': 't', 'title': 'one'}])
    edge = Barrier(2, timeout=15)
    with FakeCloudServer() as server:
        client = CloudClient(base_url=server.base_url, api_key='cloud-key-value')
        send = client.start_conversation

        def slow_reply(**kwargs):
            response = send(**kwargs)  # external execution already started
            edge.wait()
            edge.wait()
            return response
        monkeypatch.setattr(client, 'start_conversation', slow_reply)
        old = scheduler(env, OpenHandsExecutor(client), worker_id='old')
        new = scheduler(env, OpenHandsExecutor(client), worker_id='new')
        with ThreadPoolExecutor(max_workers=3) as pool:
            original = pool.submit(old.tick, rid)
            edge.wait()
            before = env['lab'].list_tasks_internal(rid)[0]
            sql(env, "UPDATE nexus_run_tasks SET lease_expires=now()-interval '1 s'")
            # Hide discovery until the late worker has tried to bind on the old fence.
            server.cloud.search_status = 503
            new.tick(rid)
            edge.wait()
            original.result(timeout=10)
            after = env['lab'].get_task_internal(before['id'])
            assert after['lease_token'] > before['lease_token'] and after['lease_owner'] == 'new'
            assert not after['conversation_id'] and after['attempt'] == 1
            assert sql(env, 'SELECT state FROM nexus_dispatch_intents')[0]['state'] == 'identified'
            assert not sql(env, 'SELECT * FROM nexus_executor_stops')
            server.cloud.search_status = 0
            gate = Barrier(3, timeout=10)

            def rescue(n):
                gate.wait()
                scheduler(env, OpenHandsExecutor(client), worker_id=str(n)).tick(rid)
            list(pool.map(rescue, range(3)))
            assert env['lab'].get_task_internal(after['id'])['conversation_id'] in server.cloud.start_bodies
            assert len(sql(env, 'SELECT id FROM nexus_task_executions')) == len(server.cloud.start_bodies) == 1


def test_missing_async_receipt_search_outage_and_late_visibility(env, monkeypatch):  # noqa: F811
    rid = start(env, [{'task_key': 't', 'title': 'one'}])
    with FakeCloudServer() as server:
        client = CloudClient(base_url=server.base_url, api_key='cloud-key-value')
        server.cloud.start_polls_required = 1000
        sched = scheduler(env, OpenHandsExecutor(client, start_poll_attempts=1, poll_interval=0))
        sched.tick(rid)
        (receipt,) = sql(env, 'SELECT start_id,state FROM nexus_dispatch_intents')
        assert receipt['state'] == 'uncertain' and receipt['start_id']
        server.cloud.start_tasks.clear()
        server.cloud.search_status = 500
        for _ in range(3):
            sched.tick(rid)
        assert env['lab'].list_tasks_internal(rid)[0]['conversation_id'] is None
        server.cloud.search_status = 0
        # A partial listing must not authorize a bind (let alone a resend).
        search = client.search_conversations
        def paged(*, limit, page_id, tag):
            if page_id is None:
                return [], 'next'
            return search(limit=limit, tag=tag)
        monkeypatch.setattr(client, 'search_conversations', paged)
        assert sched.tick(rid).followed == 1
        assert len(server.cloud.start_bodies) == 1


def test_revoked_unknown_keeps_branch_until_late_receipt_owes_a_stop(env, monkeypatch):  # noqa: F811
    from orchestration.cloud_client import CloudUnavailable
    from tests.test_rust_claim import start as another_project

    rid = start(env, [{'task_key': 't', 'title': 'writer'}], repository='org/same')
    with FakeCloudServer() as server:
        client = CloudClient(base_url=server.base_url, api_key='test')
        send = client.start_conversation
        def lose(**kw):
            send(**kw)
            raise CloudUnavailable('lost response')
        monkeypatch.setattr(client, 'start_conversation', lose)
        sched = scheduler(env, OpenHandsExecutor(client))
        sched.tick(rid)
        lab = env['lab']
        lab.set_run_state_internal(rid, 'cancelled')
        _, other = another_project(env['store'], lab, [{'id': 'b', 'title': 'B'}], repo='org/same')
        assert str(rid) in active_run_ids(env['store'])
        assert lab.claim_task(other, 'early') is None
        sched.tick(rid)  # only reconcile; never bring a cancelled run back to running
        (cid,) = server.cloud.start_bodies
        assert lab.stop_for(cid)['state'] == 'requested'
        assert lab.claim_task(other, 'still-too-early') is None
        # Repointed process connection: even at the legacy stop ceiling,
        # a 404 from a different server cannot release the branch.
        from orchestration.nexus_lab import MAX_STOP_ATTEMPTS
        with FakeCloudServer() as other_server:
            sched.executor.client = CloudClient(base_url=other_server.base_url, api_key='other')
            sql(env, 'UPDATE nexus_executor_stops SET attempts=%s', MAX_STOP_ATTEMPTS)
            assert sched.deliver_stops()['pending'] == 1 and not other_server.cloud.calls
            assert lab.claim_task(other, 'wrong-service') is None
        sched.executor.client = client
        sql(env, 'UPDATE nexus_executor_stops SET next_attempt_at=now()')
        sched.deliver_stops()
        assert lab.claim_task(other, 'now-safe') and len(server.cloud.start_bodies) == 1


def test_lost_continuation_ack_requires_exact_user_event_not_the_old_result(env, monkeypatch):  # noqa: F811
    from orchestration.cloud_client import CloudUnavailable
    rid = start(env, [{'task_key': 't', 'title': 'one'}])
    with FakeCloudServer() as server:
        client = CloudClient(base_url=server.base_url, api_key='test')
        sched = scheduler(env, OpenHandsExecutor(client, poll_interval=0))
        sched.tick(rid)
        task, = env['lab'].list_tasks_internal(rid)
        cid = task['conversation_id']
        env['lab'].retry_task(task['id'], verdict='completed_unverified', evidence={}, note='fix it',
                              continue_conversation=True, lease_token=task['lease_token'])
        send = client.send_message
        def lose(*args, **kw):
            send(*args, **kw)
            raise CloudUnavailable('lost acknowledgement')
        monkeypatch.setattr(client, 'send_message', lose)
        sched.tick(rid)
        (text,) = server.cloud.messages[cid]
        server.cloud.add_assistant_message(cid, text)  # an echo is not a user turn
        assert not sched.tick(rid).followed
        server.cloud.events[cid].append({'kind': 'MessageEvent', 'llm_message': {
            'role': 'user', 'content': [{'type': 'text', 'text': text}]}})
        assert sched.tick(rid).followed == 1
        assert env['lab'].get_task_internal(task['id'])['conversation_id'] == cid
        assert len(server.cloud.messages[cid]) == len(server.cloud.start_bodies) == 1


@pytest.mark.parametrize('transport', ['direct', 'local'])
def test_scoped_create_has_one_external_execution_after_lost_reply(monkeypatch, tmp_path, transport):
    from orchestration.cloud_client import CloudUnavailable
    from orchestration.local_runtime import LocalCloudClient
    from tests.fake_runtime import FakeServer
    from orchestration.nexus_lab import GitVerifier, LabScheduler

    with studio_stack(monkeypatch, lab_inline=False, transport='direct') as studio, ExitStack() as cleanup:
        _, rid = _start_run(studio)
        if transport == 'local':
            fake = cleanup.enter_context(FakeServer())
            runtime = fake.runtime
            client = LocalCloudClient(fake.base_url, api_key=runtime.expected_key, workspace_root=str(tmp_path))
        else:
            client, runtime = studio.app.client, studio.sandbox
            client.reuse_sandboxes = False  # identity must still pin the original sandbox
        send = client.start_conversation
        observed = {}
        def lose(**kw):
            observed.update(kw)
            send(**kw)
            raise CloudUnavailable('lost create reply')
        monkeypatch.setattr(client, 'start_conversation', lose)
        lab = LabStore(studio.app.store)
        sched = LabScheduler(lab, OpenHandsExecutor(client, specs=studio.app.specs), GitVerifier(studio.app.github))
        outcome = sched.tick(rid)
        assert runtime.conversations, (outcome, [(t['state'], t['last_error']) for t in lab.list_tasks_internal(rid)])
        cid, = runtime.conversations
        assert cid == observed['dispatch_id']
        assert runtime.conversations[cid].runs == 1
        assert sched.tick(rid).followed == 1
        # An explicit adapter-level replay, not a scheduler retry: even the
        # initial user turn is inside the idempotent create on this sandbox.
        assert send(**observed).app_conversation_id == cid
        assert len(runtime.conversations) == runtime.conversations[cid].runs == 1
        if transport == 'direct':
            assert len(studio.cloud.cloud.sandboxes) == 1


def test_verified_failure_is_signed_and_child_stays_gated(env, monkeypatch):  # noqa: F811
    from orchestration.lab_worker import build_evidence_service
    from orchestration.nexus_lab import GitVerifier
    _signer_env(monkeypatch)
    rid = start(env, [{'task_key': 'a', 'title': 'fails'},
                      {'task_key': 'b', 'title': 'gated', 'dependencies': ['a']}], policy='bounded')
    with FakeCloudServer() as server:
        client = CloudClient(base_url=server.base_url, api_key='test')
        sched = scheduler(env, OpenHandsExecutor(client), GitVerifier(None), evidence=build_evidence_service(env['store']))
        sched.tick(rid)
        (cid,) = server.cloud.start_bodies
        server.cloud.conversations[cid]['execution_status'] = 'error'
        sched.tick(rid)
        parent, child = env['lab'].list_tasks_internal(rid)
        assert (parent['state'], child['state'], child['attempt']) == ('failed', 'pending', 0)
        assert not env['lab'].claim_task(rid, 'cannot-claim') and len(server.cloud.start_bodies) == 1
        rec, = env['store'].evidence_for_project(env['project'])
        assert rec['body']['detail']['verdict'] == 'failed'
        assert EvidenceTrustEngine(Keyring.from_env()).verify(rec).verdict == VERIFIED


def test_expired_prepared_grant_cannot_send_or_disarm_its_successor(env):  # noqa: F811
    from orchestration.dispatch_journal import DispatchHeld, DispatchJournal
    lab, journal = env['lab'], DispatchJournal(env['store'])
    rid = start(env, [{'task_key': 't', 'title': 'only one budgeted attempt'}],
                policy='bounded', max_attempts_per_task=1, max_iterations=1)
    stale = lab.claim_task(rid, 'old', dispatch_intent=True)
    sql(env, "UPDATE nexus_run_tasks SET lease_expires=clock_timestamp()-interval '1 s'")
    with pytest.raises(DispatchHeld):
        journal.arm(stale, {})
    intent, = journal.recoverable(rid)
    assert journal.take_over(intent, 'new', 60)
    current = dict(lab.get_task_internal(stale['id']), _intent_id=stale['_intent_id'])
    journal.arm(current, {'kind': 'start'})
    with pytest.raises(DispatchHeld):
        journal.arm(stale, {})
    assert not journal.rejected(stale)  # late preflight failure is not a rejection of the new send
    assert sql(env, 'SELECT state FROM nexus_dispatch_intents') == [{'state': 'sending'}]
    assert sql(env, 'SELECT iteration,total_iterations FROM nexus_runs') == [{'iteration': 1, 'total_iterations': 1}]


def test_delayed_reverify_does_not_hold_a_replacement_attempt(env):  # noqa: F811
    from orchestration.nexus_lab import Verification
    from tests.test_durable_runs import Conversations
    lab = env['lab']
    rid = start(env, [{'task_key': 't', 'title': 'one'}])
    executor = Conversations()

    class LateVerifier:
        def verify(self, task, report):
            # A different verifier already returned, and a new grant is live,
            # while this worker was waiting for GitHub.
            lab.retry_task(task['id'], verdict='failed', evidence={}, note='redo', continue_conversation=False,
                           lease_token=task['lease_token'])
            new = lab.claim_task(rid, 'other')
            lab.mark_running(new['id'], conversation_id='replacement', lease_token=new['lease_token'])
            return Verification('blocked', 'GitHub temporarily unavailable', retry='reverify')

    sched = scheduler(env, executor, verifier=LateVerifier())
    sched.tick(rid)
    executor.finish(executor.of('t')[0])
    sched.tick(rid)
    current, = lab.list_tasks_internal(rid)
    assert current['conversation_id'] == 'replacement' and current['retry_at'] is None


def test_timeout_at_cycle_ceiling_settles_failure_with_durable_stop(env, monkeypatch):  # noqa: F811
    from tests.test_durable_runs import Conversations
    monkeypatch.setenv('NEXUS_RETRY_MAX_CYCLES', '1')
    rid = start(env, [{'task_key': 'a', 'title': 'a'}, {'task_key': 'b', 'title': 'b', 'dependencies': ['a']}])
    sched = scheduler(env, Conversations())
    sched.tick(rid)
    sql(env, "UPDATE nexus_run_tasks SET started_at=now()-interval '1 hour',timeout_seconds=1, "
             "max_attempts=1 WHERE task_key='a'")
    sched.tick(rid)
    assert [t['state'] for t in env['lab'].list_tasks_internal(rid)] == ['failed', 'pending']
    assert [s['state'] for s in env['lab'].stops(rid)] == ['requested']


def test_broken_signer_keeps_both_obligations_until_task_signature_is_written(env, monkeypatch):  # noqa: F811
    from tests.test_durable_runs import Conversations
    from orchestration.lab_worker import build_evidence_service

    monkeypatch.setenv('NEXUS_EVIDENCE_PRIVATE_KEY', 'invalid-test-key')
    executor = Conversations()
    sched = scheduler(env, executor, evidence=build_evidence_service(env['store']))
    rid = start(env, [{'task_key': 'sign', 'title': 'sign me'}])
    sched.tick(rid)
    executor.finish(executor.of('sign')[0])
    sched.tick(rid)
    assert env['lab'].run_row(rid)['state'] == 'completed' and rid in active_run_ids(env['store'])
    assert not env['store'].evidence_for_project(env['project'])
    due, = sql(env, 'SELECT id FROM nexus_task_attestations_due')
    assert len(sql(env, 'SELECT 1 FROM nexus_run_attestations_due')) == 1

    _signer_env(monkeypatch)
    signer = build_evidence_service(env['store'])
    repaired = scheduler(env, executor, evidence=signer)
    with monkeypatch.context() as m:
        def offline(*_):
            raise RuntimeError('signer offline')
        m.setattr(signer, 'record_task_result', offline)
        repaired.tick(rid)
        assert not env['store'].evidence_for_project(env['project'])  # no premature signed acceptance
    repaired.tick(rid)
    assert rid not in active_run_ids(env['store'])
    run, task = env['store'].evidence_for_project(env['project'])
    engine = EvidenceTrustEngine(Keyring.from_env())
    assert str(task['id']) == str(due['id']) and run['body']['detail']['decision'] == 'ACCEPTED'
    assert all(engine.verify(r).verdict == VERIFIED for r in (task, run))
    assert engine.index_integrity([task, run])['intact']


def test_result_read_on_wrong_endpoint_or_malformed_response_does_not_advance_dag(env, monkeypatch):  # noqa: F811
    rid = start(env, [{'task_key': 'a', 'title': 'first'}, {'task_key': 'b', 'title': 'next', 'dependencies': ['a']}])
    with FakeCloudServer() as a, FakeCloudServer() as b:
        client = CloudClient(base_url=a.base_url, api_key='test')
        sched = scheduler(env, OpenHandsExecutor(client))
        sched.tick(rid)
        cid, = a.cloud.start_bodies
        sched.executor.client = CloudClient(base_url=b.base_url, api_key='test')
        sched.tick(rid)
        assert not b.cloud.calls
        sched.executor.client = client
        with monkeypatch.context() as m:
            # An HTTP success without the requested row is not a terminal
            # failure or a report from some other conversation.
            m.setattr(client, '_request', lambda *args: (200, [{'id': 'unrelated', 'execution_status': 'error'}]))
            sched.tick(rid)
        parent, child = env['lab'].list_tasks_internal(rid)
        assert (parent['state'], parent['conversation_id'], child['state'], child['attempt']) == (
            'running', cid, 'pending', 0)
        assert len(a.cloud.start_bodies) == 1
