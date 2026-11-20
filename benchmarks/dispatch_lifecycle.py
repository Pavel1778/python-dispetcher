#!/usr/bin/env python3
"""Measure a real Python worker, separately from its socket services and DB.

Use tools/with_disposable_postgres.sh. Run in the checkout to measure; same
harness works on the baseline. No sleeps between ticks, no real LLM or build.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import json
import hashlib
import math
import os
from pathlib import Path
import resource
import subprocess
import sys
import time
from unittest.mock import patch

import psycopg

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
SHA = '0123456789abcdef0123456789abcdef01234567'
REPO = 'fixture/audit'


def driver(count):
    from orchestration.cloud_store import PostgresStore
    from orchestration.lab_worker import LabWorker, build_evidence_service
    from orchestration.nexus_lab_store import LabStore

    dsn = os.environ['TEST_SUPABASE_DB_URL']
    # The parent owns this scratch cluster; the driver only talks to its loopback fakes.
    from psycopg.conninfo import conninfo_to_dict
    if not conninfo_to_dict(dsn).get('host', '').startswith('/tmp/nexus-audit.'):
        raise SystemExit('scratch socket required')
    if not all(os.environ[k].startswith('http://127.0.0.1:') for k in ('OPENHANDS_CLOUD_URL', 'GITHUB_API_URL')):
        raise SystemExit('local fixture services required')
    store = PostgresStore(dsn, auto_migrate=False)
    try:
        evidence = build_evidence_service(store)
        worker = LabWorker(store, worker_id='profile', evidence=evidence, poll_interval=0.05)
        claim = LabStore.claim_task
        sign = evidence.record_task_result
        started, samples = {}, []
        transactions = statements = 0
        tx = store._tx

        @contextmanager
        def counted():
            nonlocal transactions
            transactions += 1
            with tx() as cursor:
                class Count:
                    def execute(self, *a, **kw):
                        nonlocal statements
                        statements += 1
                        return cursor.execute(*a, **kw)

                    def __getattr__(self, name):
                        return getattr(cursor, name)
                yield Count()

        def grant(*args, **kw):
            t = time.monotonic_ns()
            row = claim(*args, **kw)
            if row:
                started[str(row['id'])] = t
            return row

        def signed(task, verdict, report):
            row = sign(task, verdict, report)
            samples.append((time.monotonic_ns() - started.pop(str(task['id']))) / 1e6)
            return row

        with patch.object(store, '_tx', counted), patch.object(LabStore, 'claim_task', grant), \
                patch.object(evidence, 'record_task_result', signed):
            t = time.monotonic()
            before = resource.getrusage(resource.RUSAGE_SELF)
            for _ in range(count + 2):
                summary = worker.tick_once()
                assert not summary['errors'], summary
                if len(samples) == count:
                    break
            wall = time.monotonic() - t
            usage = resource.getrusage(resource.RUSAGE_SELF)
        assert len(samples) == count and not started, (len(samples), count)
        ordered = sorted(samples)
        return {'tasks': count, 'wall_s': round(wall, 4), 'tasks_s': round(count / wall, 2),
                **{f'p{p}_ms': round(ordered[math.ceil(count*p/100)-1], 3) for p in (50, 95, 99)},
                'worker_peak_rss_kib': usage.ru_maxrss,
                'cpu_user_s': round(usage.ru_utime - before.ru_utime, 4),
                'cpu_sys_s': round(usage.ru_stime - before.ru_stime, 4),
                'store_execute_calls': statements, 'transactions': transactions}
    finally:
        store.close()


def profile(count, rounds):
    from benchmarks.forensic_workload import private_database
    from orchestration.cloud_store import PostgresStore
    from orchestration.evidence_trust import EvidenceTrustEngine, Keyring, VERIFIED, generate_signer, public_key_b64
    from orchestration.nexus_binding import ProjectRepositoryService
    from orchestration.nexus_lab_store import LabStore
    from orchestration.nexus_projects import ProjectService
    from tests.fake_cloud import FakeCloud, FakeCloudServer
    from tests.fake_github import FakeGitHub, FakeGitHubServer

    dsn = private_database()
    signer = generate_signer('profile')
    ring = Keyring()
    ring.add('profile', public_key_b64(signer))
    verify = EvidenceTrustEngine(ring)

    class ImmediateAudit(FakeCloud):
        def make_conversation(self, **kw):
            cid = super().make_conversation(**kw, execution_status='finished')
            self.add_assistant_message(cid, 'Audit complete. Commit ' + SHA)
            return cid

    trials = []
    store = PostgresStore(dsn)
    try:
        lab = LabStore(store)
        projects = ProjectService(store)
        store.add_repository(REPO, default_branch='main', allowed_branches=['main'])
        gh = FakeGitHub(repos=[{'full_name': REPO, 'default_branch': 'main', 'private': True}])
        gh.commits = [{'sha': SHA, 'commit': {'message': 'start'}}]
        for i in range(rounds):
            pid = projects.create('bench', f'Audit {i}')['id']
            ProjectRepositoryService(store).link('bench', pid, REPO)
            goal, _ = projects.create_goal('bench', pid, 'Audit the checked-in code')
            rid = lab.start_run('bench', pid, goal['id'], repository=REPO, branch='main',
                                max_iterations=count, plan=[{'id': str(n), 'title': 'Audit',
                                'kind': 'research_audit', 'dependencies': [str(n-1)] if n else []}
                                for n in range(count)])['id']
            with FakeCloudServer(ImmediateAudit()) as cloud, FakeGitHubServer(gh) as github:
                env = {**os.environ, 'OPENHANDS_TRANSPORT': 'app', 'OPENHANDS_CLOUD_URL': cloud.base_url,
                       'OPENHANDS_API_KEY': 'fixture', 'GITHUB_TOKEN': 'fixture', 'GITHUB_API_URL': github.base_url,
                       'NEXUS_EVIDENCE_PRIVATE_KEY': base64.urlsafe_b64encode(signer.private_key.private_bytes_raw()).decode(),
                       'NEXUS_EVIDENCE_KEYRING': 'profile:' + public_key_b64(signer), 'NEXUS_EVIDENCE_KEY_ID': 'profile'}
                child = subprocess.run([sys.executable, __file__, 'driver', str(count)], env=env,
                                       text=True, stdout=subprocess.PIPE, check=True, timeout=180)
                trial = json.loads(child.stdout)
                assert lab.run_row(rid)['state'] == 'completed'
                rows = store.list_evidence(project_id=str(pid), limit=count+2)
                assert len(rows) == count+1 and verify.index_integrity(list(reversed(rows)))['intact']
                assert all(verify.verify(r, expected_commit=SHA if r['subject_kind']=='task' else None).verdict
                           == VERIFIED for r in rows)
                assert len(cloud.cloud.start_bodies) == count
                trial.update(provider_executions=count, signatures_checked=len(rows))
                trials.append(trial)
        with psycopg.connect(dsn) as con:
            version = con.info.server_version
    finally:
        store.close()
    revision = (subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() if (ROOT / '.git').exists()
                else (ROOT / '.git-archive-commit').read_text().strip())
    return {'revision': revision, 'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'python': sys.version.split()[0], 'pg': version, 'trials': trials,
            'scope': 'Sequential dependent no-change audits; HTTP doubles; verified head, signed task result and run; '
                     'one new worker process per round. Tight tick_once loop, no QA build or live provider latency.',
            'metrics': 'Nearest-rank claim entry to task signature commit; throughput and CPU across ticks including '
                       'confirmation/maintenance; peak RSS whole worker process, not PG/fixture servers. '
                       'Count worker Store cursor.execute calls, exclude transaction protocol/pool housekeeping.'}


if __name__ == '__main__':
    if sys.argv[1:2] == ['driver']:
        print(json.dumps(driver(int(sys.argv[2]))))
    else:
        ap = argparse.ArgumentParser(description=__doc__)
        ap.add_argument('--tasks', type=int, default=100, choices=range(2, 501), metavar='2..500')
        ap.add_argument('--rounds', type=int, default=3, choices=range(1, 11))
        ap.add_argument('--out', required=True, type=Path)
        a = ap.parse_args()
        a.out.write_text(json.dumps(profile(a.tasks, a.rounds), indent=2) + '\n')
