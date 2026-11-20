"""Measure the same lost response on either checkout. No deployed service/DSN.

From the checkout under test: tools/with_disposable_postgres.sh python /absolute/path/to/this.py OUT.json
"""
import argparse
import hashlib
import json
from pathlib import Path
import signal
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path.cwd()))
# Test child only. The provider has already processed the one POST and sent
# its HTTP headers, but the caller has not read the start response body.
CRASH = '''
import os, signal
from urllib.request import OpenerDirector
from orchestration import lab_worker
original = OpenerDirector.open
def receive(self, req, *args, **kw):
    response = original(self, req, *args, **kw)
    if req.get_method() == 'POST' and req.selector == '/api/v1/app-conversations':
        os.kill(os.getpid(), signal.SIGKILL)
    return response
OpenerDirector.open = receive
raise SystemExit(lab_worker.main(['--worker-id','lost-reply','--once']))
'''


def main():
    from benchmarks.forensic_workload import private_database
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('out', type=Path)
    args = ap.parse_args()
    private_database()
    from tests.test_lab_inline import _start_run
    from tests.test_lab_worker_process import _worker_env
    from tests.test_studio_api import studio_stack

    with pytest.MonkeyPatch.context() as mp, studio_stack(mp, lab_inline=False) as studio:
        _, rid = _start_run(studio)
        env = _worker_env(studio)
        rc = subprocess.run([sys.executable, '-c', CRASH], env=env, capture_output=True, timeout=25).returncode
        assert rc == -signal.SIGKILL
        first_id, = studio.cloud.cloud.start_bodies

        def snapshot():
            api = studio.cloud.cloud
            with studio.app.store._tx() as cur:
                cur.execute("SELECT t.state,t.attempt,t.lease_token,t.conversation_id,r.iteration,r.total_iterations "
                            "FROM nexus_runs r JOIN nexus_run_tasks t ON t.run_id=r.id WHERE r.id=%s AND t.task_key='audit'", (rid,))
                row = cur.fetchone()
                bound = row.pop('conversation_id')
                cur.execute('SELECT count(*) AS n FROM nexus_task_executions WHERE run_id=%s', (rid,))
                n = cur.fetchone()['n']
                cur.execute("SELECT to_regclass('nexus_dispatch_intents') IS NOT NULL AS journal")
                journal = None
                if cur.fetchone()['journal']:
                    cur.execute('SELECT state FROM nexus_dispatch_intents WHERE run_id=%s ORDER BY created_at', (rid,))
                    journal = [r['state'] for r in cur.fetchall()]
            return {**row, 'bound': bool(bound), 'bound_to_first': bound == first_id,
                    'start_posts': api.calls.count(('POST','/api/v1/app-conversations')),
                    'search_reads': api.calls.count(('GET','/api/v1/app-conversations/search')),
                    'distinct_remote_conversations': len(api.start_bodies),
                    'remote_initial_runs': sum(b['initial_message']['run'] is True for b in api.start_bodies.values()),
                    'local_execution_rows': n, 'intent_states': journal}

        states = {'after_sigkill': snapshot()}
        with studio.app.store._tx() as cur:
            cur.execute("UPDATE nexus_run_tasks SET lease_expires=now()-interval '1 second' "
                        "WHERE run_id=%s AND task_key='audit'", (rid,))
        for status, name in [(503, 'search_unavailable'), (0, 'search_restored')]:
            studio.cloud.cloud.search_status = status
            child = subprocess.run([sys.executable, '-m','orchestration.lab_worker','--worker-id',name,'--once'],
                                   env=env, text=True, capture_output=True, check=True, timeout=25)
            assert "'errors': []" in child.stderr
            states[name] = snapshot()
    rev = (subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip() if Path('.git').exists()
           else Path('.git-archive-commit').read_text().strip())
    with args.out.open('x') as out:
        json.dump({'revision': rev, 'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   'fault': 'SIGKILL after HTTP acceptance, before reading body; force private lease expiry; '
                            'one replacement tick with search HTTP 503, then one with search available',
                   'killed_returncode': rc, 'states': states}, out, indent=2)
        out.write('\n')


if __name__ == '__main__':
    main()
