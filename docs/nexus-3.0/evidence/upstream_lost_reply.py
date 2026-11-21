"""Managed-session recovery with a real upstream process; no model or hosted service.

Run from the checkout under tools/with_disposable_postgres.sh with arguments
/path/to/python-with-openhands-agent-server-1.60.1 /new/result.json. The relay
counts and delays actual socket responses. It neither implements nor emulates
conversation creation. No credential, peer payload, volume or log is exported.
"""
from __future__ import annotations

import contextlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import threading

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
from benchmarks.forensic_workload import private_database  # noqa: E402
from orchestration.cloud_store import PostgresStore  # noqa: E402
from orchestration.execution_profiles import ExecutionProfiles  # noqa: E402
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402
from tests.test_upstream_execution_profiles import http, port, service  # noqa: E402


@contextlib.contextmanager
def relay(upstream):
    accepted, release = threading.Event(), threading.Event()
    deny_read = threading.Event()
    creates, unavailable = [], []

    class Forward(BaseHTTPRequestHandler):
        def proxy(self):
            path = self.path
            if self.command == 'GET' and path.startswith('/api/conversations/') and deny_read.is_set():
                unavailable.append(True)
                self.send_error(503)
                return
            remote = HTTPConnection('127.0.0.1', upstream, timeout=30)
            try:
                headers = {'X-Session-API-Key': self.headers.get('X-Session-API-Key', ''),
                           'Content-Type': 'application/json'}
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                remote.request(self.command, path, body=body or None, headers=headers)
                reply = remote.getresponse()
                data = reply.read()
                if self.command == 'POST' and path == '/api/conversations':
                    creates.append(reply.status)
                    if reply.status in (200, 201) and len(creates) == 1:
                        accepted.set()
                        release.wait(40)  # never hand this ACK to the original worker
                        return
                self.send_response(reply.status)
                self.send_header('Content-Type', reply.getheader('Content-Type', 'application/json'))
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            finally:
                remote.close()

        do_GET = do_POST = do_PATCH = proxy

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Forward) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port, accepted, release, deny_read, creates, unavailable
        finally:
            release.set()
            server.shutdown()
            thread.join(timeout=5)


def probe(agent_python, home):
    dsn = private_database()
    os.environ['NEXUS_ALLOW_PRIVATE_TARGETS'] = '1'  # this scratch cluster only
    up = port()
    session = secrets.token_hex(32)
    native = 'http://127.0.0.1:' + str(up)
    env = {'HOME': str(home), 'PATH': os.environ['PATH'], 'LANG': 'C.UTF-8',
           'SESSION_API_KEY': session, 'OH_SECRET_KEY': secrets.token_hex(32), 'DO_NOT_TRACK': '1',
           'OH_ENABLE_VSCODE': 'false', 'OH_ENABLE_BROWSER': 'false', 'OTEL_SDK_DISABLED': 'true',
           'LITELLM_LOCAL_MODEL_COST_MAP': 'True', 'HF_HUB_OFFLINE': '1',
           'HTTPS_PROXY': 'http://127.0.0.1:1', 'HTTP_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': '127.0.0.1,localhost'}
    def peer(path, body=None):
        return http(native, path, {'X-Session-API-Key': session}, body)

    with contextlib.ExitStack() as stack:
        stack.enter_context(service([agent_python, '-m', 'openhands.agent_server', '--host', '127.0.0.1', '--port', str(up)],
                                    env, home, lambda: peer('/openapi.json'), home / 'private-peer.log'))
        assert peer('/openapi.json')[1]['info']['version'] == '1.60.1'
        proxy_port, accepted, release, outage, posts, unavailable = stack.enter_context(relay(up))
        worker_env = {**env, 'PYTHONPATH': str(ROOT), 'SUPABASE_DB_URL': dsn,
                      'LOCAL_AGENT_SERVER_URL': f'http://127.0.0.1:{proxy_port}', 'OPENHANDS_TRANSPORT': 'local',
                      'NEXUS_ALLOW_PRIVATE_TARGETS': '1', 'OHP_WORKSPACE_ROOT': str(home / 'workspaces')}
        store = PostgresStore(dsn)
        stack.callback(store.close)
        profiles, projects, lab = ExecutionProfiles(store), ProjectService(store), LabStore(store)
        url = 'https://127.0.0.1:1/v1'
        status, link = peer('/api/llm/provider-connections', {'provider': 'openai', 'display_name': 'probe',
                                                              'base_url': url, 'api_key': secrets.token_hex(24)})
        assert status == 201
        assert peer('/api/agent-profiles/probe', {'agent_kind': 'openhands', 'llm_profile_ref': 'default',
                                                 'tools': [], 'mcp_server_refs': []})[0] == 201
        d = dict(provider='openai', model='openai/gpt-4o-mini', base_url=url, key_ref=link['id'],
                 agent_profile_id=peer('/api/agent-profiles/probe')[1]['profile']['id'], connection_id=None,
                 params={'temperature': .2})
        assert profiles.save('fixture', d, operator=True)['revision'] == 1
        p = projects.create('fixture', 'restart')['id']
        goal = projects.create_goal('fixture', p, 'recover this one launch')[0]['id']
        rid = lab.start_run('fixture', p, goal, policy='bounded', plan=[{'id':'audit','title':'audit','kind':'research_audit'}])['id']

        def intent():
            with store._tx() as cur:
                cur.execute('SELECT id, state, lease_token, context FROM nexus_dispatch_intents WHERE run_id=%s', (rid,))
                (row,) = cur.fetchall()
                return row

        def worker():
            return subprocess.Popen([sys.executable, '-m', 'orchestration.lab_worker', '--once'], env=worker_env,
                                    cwd=home, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def replacement():
            with worker() as proc:
                try:
                    assert proc.wait(timeout=40) == 0
                finally:
                    proc.kill()

        with worker() as original:
            try:
                assert accepted.wait(35), 'no upstream acceptance'
                before = intent()
                assert before['state'] == 'sending' and posts == [201]
                assert before['context']['expected_session']['revision'] == 1
                original.kill()
                assert original.wait(timeout=5) == -signal.SIGKILL
            finally:
                original.kill()
                release.set()
        # Deterministic lease-clock advance. Pausing prevents subsequent work;
        # neither pausing nor loss of the ACK is authority to send another POST.
        lab.set_run_state('fixture', p, rid, 'paused')
        with store._tx() as cur:
            cur.execute("UPDATE nexus_run_tasks SET lease_expires=now()-interval '1 second' WHERE run_id=%s", (rid,))
        assert profiles.save('fixture', {**d, 'model':'openai/gpt-4o'}, operator=True)['revision'] == 2
        outage.set()
        replacement()
        middle = intent()
        assert unavailable and middle['state'] == 'uncertain' and posts == [201]
        outage.clear()
        replacement()
        final = intent()
        assert final['state'] == 'bound' and final['id'] == before['id']
        assert final['lease_token'] == before['lease_token'] + 1 and posts == [201]
        cid = str(final['id'])
        status, observed = peer('/api/conversations/' + cid)
        assert status == 200 and observed['agent']['llm']['model'] == d['model']
        with store._tx() as cur:
            cur.execute('SELECT conversation_id FROM nexus_task_executions WHERE run_id=%s', (rid,))
            assert [str(r['conversation_id']) for r in cur.fetchall()] == [cid]
        assert lab.run_row(rid)['total_iterations'] == 1
        return dict(candidate=subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip(),
                    upstream='openhands-agent-server 1.60.1', transport='local; counting loopback relay',
                    original_exit=-9, terminal_acks_forwarded_to_original=0, native_create_statuses=posts,
                    replacements=2, interrupted_conversation_reads=len(unavailable),
                    intent_states=[before['state'], middle['state'], final['state']],
                    fences=[before['lease_token'], middle['lease_token'], final['lease_token']],
                    original_launch_revision=1, desired_during_recovery=2, original_model_read_back=True,
                    execution_records=1, total_iterations=1, model_completion='not tested')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='nexus-managed-peer-') as directory:
        data = probe(sys.argv[1], Path(directory))
    with Path(sys.argv[2]).open('x') as result:
        json.dump(data, result, indent=2)
        result.write('\n')
