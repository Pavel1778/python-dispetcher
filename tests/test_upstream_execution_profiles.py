"""Opt-in process acceptance against the unmodified PyPI agent-server 1.60.1.
No Cloud, real model credential, git push or model-generated file is involved.
Use tools/with_disposable_postgres.sh env NEXUS_TEST_AGENT_PYTHON=/.../python pytest -s <this file>.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

AGENT = os.environ.get('NEXUS_TEST_AGENT_PYTHON')
DSN = os.environ.get('TEST_SUPABASE_DB_URL')
pytestmark = pytest.mark.skipif(not AGENT or not DSN, reason='requires isolated PG and upstream agent-server 1.60.1 interpreter')
ROOT = Path(__file__).resolve().parents[1]
PROFILE = '/api/openhands/execution-profile'


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def http(url, path, headers, body=None, method=None):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Content-Type': 'application/json', **headers}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as res:
            return res.status, json.load(res)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


@contextlib.contextmanager
def service(argv, env, cwd, probe, log):
    with open(log, 'ab') as f, subprocess.Popen(argv, env=env, cwd=cwd, stdout=f, stderr=f) as p:
        try:
            until = time.monotonic() + 65
            while time.monotonic() < until:
                assert p.poll() is None, 'service stopped before readiness'
                try:
                    if probe()[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(.2)
            else:
                pytest.fail('service never became ready')
            yield p.pid
        finally:
            p.terminate()
            try:
                p.wait(timeout=12)
            except subprocess.TimeoutExpired:
                p.kill()


@pytest.fixture
def github():
    from tests.fake_github import FakeGitHub, FakeGitHubServer
    with FakeGitHubServer(FakeGitHub([{'full_name': 'nexus-fixture/probe'}])) as host:
        yield host


def test_real_runtime_redeploy_and_configured_sessions(tmp_path, monkeypatch, github):
    from orchestration.cloud_store import PostgresStore
    from orchestration.nexus_lab_store import LabStore
    from orchestration.nexus_projects import ProjectService
    from tests._test_db import reset_schema

    reset_schema(DSN)
    agent_port, web_port = port(), port()
    target, web = f'http://127.0.0.1:{agent_port}', f'http://127.0.0.1:{web_port}'
    session, machine, credential = (secrets.token_hex(24) for _ in range(3))
    agent_headers = {'X-Session-API-Key': session}
    web_headers = {'Authorization': 'Bearer ' + machine}
    home = tmp_path / 'volume'
    home.mkdir()
    agent_env = {'HOME': str(home), 'PATH': os.environ['PATH'], 'LANG': 'C.UTF-8',
                 'SESSION_API_KEY': session, 'OH_SECRET_KEY': secrets.token_hex(24),
                 'OH_ENABLE_VSCODE': 'false', 'OH_ENABLE_BROWSER': 'false', 'DO_NOT_TRACK': '1',
                 'LITELLM_LOCAL_MODEL_COST_MAP': 'True', 'HF_HUB_OFFLINE': '1', 'OTEL_SDK_DISABLED': 'true',
                 'HTTPS_PROXY': 'http://127.0.0.1:1', 'HTTP_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': '127.0.0.1,localhost'}
    machine_env = {**agent_env, 'PYTHONPATH': str(ROOT), 'SUPABASE_DB_URL': DSN, 'WEB_AUTH_TOKEN': machine,
                   'GITHUB_API_URL': github.base_url, 'GITHUB_TOKEN': 'socket-fixture',
                   'OPENHANDS_TRANSPORT': 'local', 'LOCAL_AGENT_SERVER_URL': target, 'NEXUS_ALLOW_PRIVATE_TARGETS': '1',
                   'NEXUS_LAB_INLINE': '0', 'NEXUS_MANAGED_CHECKOUT_ROOT': str(home / 'assigned'), 'OHP_WORKSPACE_ROOT': str(home / 'workspaces')}
    monkeypatch.setenv('NEXUS_ALLOW_PRIVATE_TARGETS', '1')
    def agent():
        return service([AGENT, '-m', 'openhands.agent_server', '--host', '127.0.0.1', '--port', str(agent_port)],
                                agent_env, home, lambda: http(target, '/openapi.json', {}), tmp_path / 'agent.log')
    def backend():
        return service([sys.executable, '-m', 'orchestration.web_server', '--host', '127.0.0.1', '--port', str(web_port)],
                                  machine_env, home, lambda: http(web, PROFILE, web_headers), tmp_path / 'backend.log')
    def peer(path, body=None, method=None):
        return http(target, path, agent_headers, body, method)
    def api(path, body=None):
        return http(web, path, web_headers, body)
    sessions, expected_llms = [], []
    agent_pids, backend_pids, worker_pids = [], [], []
    wanted = {'provider': 'openai', 'model': 'openai/gpt-4o-mini', 'base_url': 'https://127.0.0.1:1/v1',
              'connection_id': None, 'params': {'max_output_tokens': 81, 'temperature': .2}}
    for deployment in range(2):
        with agent() as apid, backend() as bpid:
            agent_pids.append(apid)
            backend_pids.append(bpid)
            if deployment == 0:
                code, conn = peer('/api/llm/provider-connections', {'provider': 'openai', 'display_name': 'isolated',
                                                                    'base_url': wanted['base_url'], 'api_key': credential})
                assert code == 201
                assert peer('/api/agent-profiles/isolated', {'agent_kind': 'openhands', 'llm_profile_ref': 'default',
                                                             'tools': [], 'mcp_server_refs': []})[0] == 201
                wanted.update(key_ref=conn['id'], agent_profile_id=peer('/api/agent-profiles/isolated')[1]['profile']['id'])
                assert api(PROFILE, wanted)[0] == 200
            else:
                assert api(PROFILE)[1]['profile']['desired'] == wanted
            code, synced = api(PROFILE + '/sync', {})
            assert code == 200, synced
            profile = synced['profile']
            if deployment:
                assert profile['observation']['applied'] is False
            assert profile['observation']['state'] == 'verified' and profile['revision'] == 1
            if deployment == 1:
                # Restore at revision 1 has been read back. A new revision must
                # affect the *next* create, never rewrite the earlier session.
                wanted = {**wanted, 'model': 'openai/gpt-4o', 'params': {'temperature': .1, 'max_output_tokens': 56}}
                assert api(PROFILE, wanted)[1]['profile']['revision'] == 2
            expected_llms.append({'model': wanted['model'], 'base_url': wanted['base_url'], **wanted['params']})
            # Real backend/worker, new project/session and runtime GET of the resolved LLM.
            code, project = api('/api/v1/projects', {'name': 'probe-' + str(deployment)})
            assert code == 201
            project_id = project['project']['id']
            assert api('/api/v1/projects/' + project_id + '/execution-profile')[1]['profile']['desired'] == wanted
            # Submit a real task with an independently staged disposable checkout to the production store. Only the new worker can bind it.
            store = PostgresStore(DSN)
            try:
                with store._tx() as cur:
                    cur.execute("SELECT principal FROM nexus_project_members WHERE project_id=%s AND role='owner'", (project_id,))
                    principal = cur.fetchone()['principal']
                lab = LabStore(store)
                goal = ProjectService(store).create_goal(principal, project_id, 'Report your working directory')[0]
                run = lab.start_run(principal, project_id, goal['id'], policy='bounded')
                from orchestration.nexus_binding import ProjectRepositoryService
                repo = 'nexus-fixture/probe'
                branch = 'dev-' + str(deployment)
                store.add_repository(repo, default_branch=branch, allowed_branches=[branch])
                ProjectRepositoryService(store).link(principal, project_id, repo)
                task = lab.add_task(principal, project_id, run['id'], task_key='check', title='Report your working directory',
                                    repository=repo, branch=branch, kind='research_audit')
                checkout = home / 'assigned' / project_id / str(task['id'])
                checkout.mkdir(parents=True)
                def git(*args):
                    return subprocess.check_output(['git', '-C', str(checkout), *args], text=True).strip()
                git('init', '-q', '-b', branch)
                git('remote', 'add', 'origin', 'https://github.com/nexus-fixture/probe.git')
                git('-c', 'user.name=probe', '-c', 'user.email=probe@example.invalid', 'commit', '-qm', 'seed', '--allow-empty')
                github.gh.push(repo, git('rev-parse', 'HEAD'))
                with subprocess.Popen([sys.executable, '-m', 'orchestration.lab_worker', '--once'], env=machine_env,
                                      cwd=home, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as w:
                    worker_pids.append(w.pid)
                    w.communicate(timeout=45)
                    assert w.returncode == 0
                claimed = lab.get_task_internal(task['id'])
                assert claimed['state'] == 'running', (claimed['state'], claimed['last_error'])
                sessions.append(str(claimed['conversation_id']))
                with store._tx() as cur:
                    cur.execute('SELECT context FROM nexus_dispatch_intents WHERE task_id=%s', (task['id'],))
                    ctx = cur.fetchone()['context']
                    observed_checkout = ctx['checkout']
                    assert ctx['base_sha'] == observed_checkout['head']
                    assert ctx['expected_session']['model'] == wanted['model']
                assert observed_checkout['head'] == git('rev-parse', 'HEAD') and observed_checkout['branch'] == branch
                assert peer('/api/conversations/' + sessions[-1])[1]['workspace']['working_dir'] == str(checkout)
                # Pause NEXUS polling, not the runtime. The refused model request cannot verify a task.
                lab.set_run_state(principal, project_id, run['id'], 'paused')
            finally:
                store.close()
            for cid, expected in zip(sessions, expected_llms, strict=True):
                code, observed = peer('/api/conversations/' + cid)
                assert code == 200
                llm = observed['agent']['llm']
                assert {k: llm.get(k) for k in expected} == expected
            if deployment == 0:
                # Delete only the copy created by this test in the disposable server.
                name = profile['observation']['profile']
                assert peer('/api/profiles/' + name, method='DELETE')[0] == 200
                # Restore by startup on the next deployment, not by a manual settings PATCH.
                assert peer('/api/llm/provider-connections/' + wanted['key_ref'],
                            {'api_key': secrets.token_hex(24)}, 'PATCH')[0] == 200
    assert len(set(sessions)) == len(set(agent_pids)) == len(set(backend_pids)) == len(set(worker_pids)) == 2
    print(json.dumps({'agent_server': '1.60.1', 'worker_bound_sessions_read_back': len(sessions), 'agent_pids': agent_pids,
                      'backend_pids': backend_pids, 'worker_pids': worker_pids, 'session_profiles': expected_llms, 'revisions': [1, 2],
                      'key_rotation': 'native connection, stable ref', 'model_inference': 'not successful (isolated unreachable provider)' }))
