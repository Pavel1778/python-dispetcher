"""Authority, revision, transport and journal boundaries; these faults are not live-provider proof."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import uuid

import pytest

from orchestration.cloud_client import CloudClient
from orchestration.cloud_store import NotFound
from orchestration.execution_profiles import ExecutionProfiles, ProfileBlocked, validate
from orchestration.mcp_server import McpError, McpServer
from orchestration.nexus_lab import OpenHandsExecutor
from tests.fake_cloud import FakeCloudServer
from tests.test_durable_runs import env, ALICE, scheduler, sql, start  # noqa: F401

pytestmark = pytest.mark.skipif(not os.environ.get('TEST_SUPABASE_DB_URL'), reason='requires isolated PostgreSQL')


def desired(**overrides):
    return {'provider': 'openai', 'model': 'openai/test', 'base_url': 'https://model.example/v1',
            'key_ref': '1' * 32, 'agent_profile_id': str(uuid.UUID(int=9)), 'connection_id': None,
            'params': {'temperature': 0}, **overrides}


@pytest.mark.parametrize('bad', [{'params': {'api_key': 'never-stored'}}, {'base_url': 'https://user:never-stored@host'},
                                 {'base_url': 'file:///no'}, {'base_url': 'https://host/?token=never-stored'},
                                 {'key_ref': 'never-stored'}, {'model': 'other/test'}, {'params': {'top_p': False}},
                                 {'params': {'temperature': float('nan')}}, {'secret': 'never-stored'}])
def test_invalid_desired_never_echoes_input(bad):
    with pytest.raises(ValueError) as refusal:
        validate(desired(**bad))
    assert 'never-stored' not in str(refusal.value)


def test_precedence_membership_and_agent_projection(env):  # noqa: F811
    svc = ExecutionProfiles(env['store'])
    p = env['project']
    unrelated = str(env['projects'].create('user:bob', 'other')['id'])
    run = start(env, [{'task_key': 't', 'title': 'T'}])
    task = str(env['lab'].list_tasks_internal(run)[0]['id'])
    for scope, model in [({}, 'global'), ({'project_id': p}, 'project'), ({'project_id': p, 'task_id': task}, 'task')]:
        data = desired(model='openai/' + model)
        assert svc.save(ALICE, data, **scope, operator=True)['revision'] == 1
        assert svc.save(ALICE, data, **scope, operator=True)['revision'] == 1
    assert [svc.effective(*scope)['desired']['model'] for scope in [(unrelated,), (p,), (p, task)]] == [
        'openai/global', 'openai/project', 'openai/task']
    with pytest.raises(NotFound):
        svc.view('user:bob', project_id=unrelated, task_id=task)
    with pytest.raises(NotFound):
        svc.save(ALICE, desired(connection_id=str(uuid.uuid4())), project_id=p)
    with pytest.raises(NotFound):
        svc.save(ALICE, desired(), project_id=p)
    with pytest.raises(NotFound):
        svc.view(ALICE)
    def tool(principal):
        return McpServer(env['store'], principal).handle('tools/call', {'name': 'get_execution_profile', 'arguments': {'project_id': p, 'task_id': task}})
    assert 'key_ref' not in json.dumps(tool(ALICE)) and 'openai/task' in json.dumps(tool(ALICE))
    with pytest.raises(McpError, match='not found'):
        tool('user:bob')
    env['lab'].claim_task(run, 'owner')
    with pytest.raises(ValueError, match='pending'):
        svc.save(ALICE, desired(), project_id=p, task_id=task, operator=True)


def test_concurrent_revision_fences_late_readback(env):  # noqa: F811
    svc = ExecutionProfiles(env['store'])
    row = svc.save(ALICE, desired(), operator=True)
    with ThreadPoolExecutor(8) as pool:
        revs = list(pool.map(lambda n: svc.save(ALICE, desired(model=f'openai/m{n}'), operator=True)['revision'], range(16)))
    assert sorted(revs) == list(range(2,18))
    with pytest.raises(ProfileBlocked, match='changed'):
        svc.observe(row, 'verified')
    assert svc.effective()['observation'] == {'state': 'saved'}


def test_known_profile_barrier_releases_send_permit_not_an_unknown_send(env):  # noqa: F811
    profiles = ExecutionProfiles(env['store'])
    profiles.save(ALICE, desired(), operator=True)
    rid = start(env, [{'task_key': 't', 'title': 'T'}])
    with FakeCloudServer() as server:
        client = CloudClient(server.base_url, api_key='probe')
        client.execution_profiles = profiles
        worker = scheduler(env, OpenHandsExecutor(client))
        for _ in range(3):
            worker.tick(rid)
        assert not server.cloud.start_bodies
        assert [r['state'] for r in sql(env,'SELECT state FROM nexus_dispatch_intents')] == ['rejected']
        row = env['lab'].list_tasks_internal(rid)[0]
        assert row['state'] == 'waiting' and 'profile blocked' in row['wait_reason']


@pytest.mark.parametrize('fault,path,expected', [('expired', '/api/llm/provider-connections', 1),
    ('missing', '/api/llm/provider-connections',1), ('drift', '/api/llm/provider-connections',1),
    ('timeout','/openapi.json',3), ('rate','/openapi.json',3), ('outage','/openapi.json',3),
    ('version','/openapi.json',1), ('partial','/api/profiles/nexus-',3), ('invalid_model','/validate',1)])
def test_faults_cannot_launch_or_echo_raw_peer_errors(env, fault, path, expected):  # noqa: F811
    svc = ExecutionProfiles(env['store'])
    row = svc.save(ALICE, desired(), operator=True)
    d = row['desired']
    calls = []
    created = {}
    def peer(method, url, body):
        calls.append((method, url))
        if url == '/openapi.json':
            if fault == 'timeout':
                raise TimeoutError('untrusted secret')
            if fault in ('rate','outage'):
                return (429 if fault=='rate' else 503), {'detail':'untrusted secret'}
            return 200, {'info':{'version':'old' if fault=='version' else '1.60.1'}, 'paths': {
                '/api/profiles/{name}': {'get':{}, 'post':{}}, '/api/llm/provider-connections': {'get':{}},
                '/api/agent-profiles':{'get':{}},'/api/conversations':{'post':{}},'/api/profiles/{name}/validate':{'post':{}}}}
        if url == '/api/agent-profiles':
            return 200, {'profiles': [{'id':d['agent_profile_id'], 'agent_kind':'openhands'}]}
        if url == '/api/llm/provider-connections':
            if fault == 'expired':
                return 401, {'error':'untrusted secret'}
            return 200, [{'id':d['key_ref'],'api_key_set':fault!='missing','provider':'openai',
                          'base_url':'https://wrong.example' if fault=='drift' else d['base_url']}]
        if url.endswith('/validate'):
            return 200, {'valid':False, 'error':'untrusted secret'}
        if method == 'POST':
            created.update(config=body['llm'], name=url.rsplit('/',1)[-1], api_key_set=fault!='partial')
            return 201, {}
        return (200, created) if created else (404, {})
    with pytest.raises(ProfileBlocked):
        svc.reconcile(row, peer, test_model=True)
    observation = svc.effective()['observation']
    assert observation['state'] == 'blocked' and 'untrusted secret' not in json.dumps(observation)
    assert sum(path in url for _,url in calls) == expected
    assert ('POST','/api/conversations') not in calls


def test_pre_journal_grant_with_no_proof_cannot_resend_or_release_branch(env):  # noqa: F811
    rid = start(env, [{'task_key':'t','title':'T'}], repository='org/dev')
    grant = env['lab'].claim_task(rid,'legacy')  # no journal, no route, no proof of non-send
    sql(env,"UPDATE nexus_run_tasks SET lease_expires=now()-interval '1 second'")
    with FakeCloudServer() as server:
        worker = scheduler(env, OpenHandsExecutor(CloudClient(server.base_url, api_key='probe')))
        for _ in range(4):
            worker.tick(rid)
        (intent,) = sql(env,'SELECT state,context FROM nexus_dispatch_intents')
        assert intent['state'] == 'uncertain' and intent['context'] == {'legacy_no_receipt': True}
        assert not server.cloud.start_bodies and not env['lab'].holds_lease(grant['id'], grant['lease_token'])
        assert env['lab'].claim_task(rid,'next') is None


def test_project_member_cannot_reconfigure_or_control_an_owners_run(env):  # noqa: F811
    p = env['project']
    run = start(env, [{'task_key':'t','title':'T'}])
    sql(env, "INSERT INTO nexus_project_members(project_id,principal,role) VALUES (%s,'member','member')", p)
    svc = ExecutionProfiles(env['store'])
    with pytest.raises(NotFound):
        svc.save('member', desired(), project_id=p)
    server = McpServer(env['store'], 'member')  # transport role alone is insufficient
    with pytest.raises(McpError, match='not found'):
        server.handle('tools/call', {'name':'cancel_run','arguments':{'project_id':p,'run_id':run}})
    assert env['lab'].run_row(run)['state'] == 'running'


@pytest.mark.parametrize('fault', ['absent', 'fetch_origin', 'push_origin', 'branch', 'dirty', 'sha', 'unknown_head', 'expired', 'rotated'])
def test_staged_checkout_needs_clean_git_and_original_live_grant(env, tmp_path, monkeypatch, fault):  # noqa: F811
    """The peer runs the actual fixed preflight, not an always-green response."""
    import subprocess
    from pathlib import Path
    from orchestration.dispatch_journal import DispatchJournal

    service = ExecutionProfiles(env['store'])
    row = service.save(ALICE, desired(), operator=True)
    repo = 'fixture/project'
    rid = start(env, [{'task_key': 't', 'title': 'read'}], repository=repo)
    task = env['lab'].claim_task(rid, 'original', dispatch_intent=True)
    scope = (env['project'], str(task['id']))
    directory = tmp_path.joinpath(*scope)
    directory.mkdir(parents=True)
    git = ('git', '-C', str(directory))
    for args in [('init', '-q', '-b', 'main'), ('remote', 'add', 'origin', 'https://github.com/' + repo + '.git'),
                 ('-c', 'user.name=Test', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'seed', '--allow-empty')]:
        subprocess.run([*git, *args], check=True)
    head = subprocess.check_output([*git, 'rev-parse', 'HEAD'], text=True).strip()
    monkeypatch.setenv('NEXUS_MANAGED_CHECKOUT_ROOT', str(tmp_path))
    dispatch = DispatchJournal(env['store'])
    dispatch.arm(task, {'base_sha': {'unknown_head': None, 'sha': '0' * 40}.get(fault, head),
                        'armed_lease': str(task['lease_token'])})
    if fault == 'absent':
        directory.rename(directory.with_suffix('.unused'))
    elif fault in ('push_origin', 'fetch_origin'):
        subprocess.run([*git, 'remote', 'set-url', *(['--push'] if fault == 'push_origin' else []),
                        'origin', 'https://github.com/other/project.git'], check=True)
    elif fault == 'dirty':
        (directory / 'uncommitted').touch()
    elif fault == 'branch':
        subprocess.run([*git, 'switch', '-qc', 'other'], check=True)
    elif fault in ('expired', 'rotated'):
        sql(env,"UPDATE nexus_run_tasks SET lease_expires=now()-interval '1 second'")
        if fault == 'rotated':
            assert dispatch.take_over(dispatch.recoverable(rid)[0], 'replacement', 900)
    def peer(method, path, body):
        assert (method, path) == ('POST', '/api/bash/execute_bash_command') and Path(body['cwd']) == directory
        if not directory.is_dir():
            return 422, {}
        out = subprocess.run(['sh', '-c', body['command']], cwd=directory, capture_output=True, text=True, timeout=8)
        return 200, {'stdout': out.stdout, 'exit_code': out.returncode}
    with pytest.raises(ProfileBlocked):
        service.checkout(row, scope, task['_intent_id'], repo, 'main', peer)
    assert 'checkout' not in sql(env, 'SELECT context FROM nexus_dispatch_intents')[0]['context']


@pytest.mark.parametrize('fault', ['status', 'id', 'cwd', 'model', 'base_url', 'params', 'tags'])
def test_post_create_drift_is_ambiguous_not_a_refused_start(fault):
    from orchestration.cloud_client import CloudUnavailable

    cid = str(uuid.uuid4())
    expected = {'model': 'openai/test', 'base_url': 'https://model.example/v1', 'params': {'temperature': .25},
                'working_dir': '/assigned/test'}
    tags = {'nexusdispatch': cid, 'nexustask': 't'}
    llm = {**expected['params'], 'model': expected['model'], 'base_url': expected['base_url']}
    data = {'id': cid, 'agent': {'llm': llm}, 'workspace': {'working_dir': expected['working_dir']}, 'tags': tags.copy()}
    ExecutionProfiles.check_session((200, data), cid, expected, tags)
    if fault in ('model', 'base_url', 'params'):
        llm['temperature' if fault == 'params' else fault] = None
    elif fault == 'id':
        data['id'] = str(uuid.uuid4())
    elif fault == 'cwd':
        data['workspace'] = {}
    elif fault == 'tags':
        data['tags'].pop('nexustask')
    with pytest.raises(CloudUnavailable):
        ExecutionProfiles.check_session((503 if fault == 'status' else 200, data), cid, expected, tags)
