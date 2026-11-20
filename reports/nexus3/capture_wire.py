"""Capture a limited wire contract from the checkout in cwd, never from a deployed URL.

Run via tools/with_disposable_postgres.sh. Requires the repo's test dependencies.
Only known fixture routes, selected headers (no cookies) and fictional data.
"""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

AUDIT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AUDIT / 'benchmarks'))
sys.path.insert(0, str(Path.cwd()))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('out', type=Path)
    ap.add_argument('--compare', type=Path, help='fail if the contract differs; ignores only Git revision')
    args = ap.parse_args()
    from forensic_workload import private_database
    private_database()  # the stack below creates/reset its disposable DB
    from tests.test_studio_api import TOKEN, studio_stack

    with pytest.MonkeyPatch.context() as env, studio_stack(env, lab_inline=False) as studio:
        def request(method, path, *, authorized=False, body=None):
            headers = {'Accept': 'application/json'}
            if authorized:
                headers['Authorization'] = 'Bearer ' + TOKEN
            if body is not None:
                headers['Content-Type'] = 'application/json'
            conn = http.client.HTTPConnection('127.0.0.1', studio.server.port, timeout=10)
            try:
                conn.request(method, path, body=body, headers=headers)
                res = conn.getresponse()
                data = res.read()
                keep = ('Content-Type', 'Cache-Control', 'X-Content-Type-Options', 'Location', 'Content-Security-Policy')
                return {'status': res.status, 'headers': {k: res.getheader(k) for k in keep if res.getheader(k)},
                        'body': (json.loads(data) if data and 'application/json' in res.getheader('Content-Type', '')
                                 else {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})}
            finally:
                conn.close()

        cases = {path: request('GET', path) for path in ('/', '/login/state', '/healthz', '/readyz', '/static/app.css')}
        cases['projects_no_auth'] = request('GET', '/api/v1/projects')
        foreign = studio.app.projects.create('user:other', 'Invisible')['id']
        cases['foreign_project'] = request('GET', f'/api/v1/projects/{foreign}', authorized=True)
        cases['mcp_no_auth'] = request('POST', '/mcp', body=b'{}')
        cases['mcp_parse_error'] = request('POST', '/mcp', authorized=True, body=b'not json')
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
                'name': 'get_project', 'arguments': {'project_id': str(foreign)}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'not_a_method'},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        ]
        responses = [request('POST', '/mcp', authorized=True, body=json.dumps(m).encode()) for m in messages]
        child = subprocess.run([sys.executable, '-m', 'orchestration.mcp_server'], check=True, timeout=20,
                               env={**os.environ, 'NEXUS_DB_URL': os.environ['TEST_SUPABASE_DB_URL'],
                                    'NEXUS_MCP_PRINCIPAL': 'machine:default'}, capture_output=True,
                               input=''.join(json.dumps(m) + '\n' for m in messages), text=True)
        replies = [json.loads(line) for line in child.stdout.splitlines()]
        assert len(replies) == 4 and [r['body'] for r in responses[:-1]] == replies
        assert responses[-1]['status'] == 202 and cases['foreign_project']['status'] == 404
        assert cases['projects_no_auth']['status'] == cases['mcp_no_auth']['status'] == 401
        assert responses[2]['body']['error']['code'] == -32004 and cases['mcp_parse_error']['status'] == 400
    result = {'http': cases, 'mcp_http': responses, 'stdio_matches_http_ids': [r['id'] for r in replies]}
    if args.compare and result != json.loads(args.compare.read_text())['contract']:
        raise SystemExit('wire contract changed; inspect before updating the reference')
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    with args.out.open('x', encoding='utf-8') as f:
        json.dump({'source_sha': sha, 'contract': result}, f, indent=2, ensure_ascii=False)
        f.write('\n')
    print(f'{sha}: {len(cases)} HTTP cases, {len(responses)} MCP messages, {len(replies)} stdio replies')


if __name__ == '__main__':
    main()
