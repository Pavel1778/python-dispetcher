"""Each machine edge, using real committed Python transitions, not two hand-copied tables."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from orchestration import control_plane as cp, nexus_workflow as wf
from orchestration.cloud_store import InvalidTransition
from tests.test_durable_runs import env, start, ALICE  # noqa: F401
from tests.test_rust_parity import RUN_STATES

BIN = os.environ.get('NEXUS_EDGE_PROBE')
pytestmark = pytest.mark.skipif(not BIN or not os.environ.get('TEST_SUPABASE_DB_URL'),
                                reason='needs Rust state_edges and isolated PG')


def test_typed_edges_match_committed_states(env, tmp_path):  # noqa: F811
    rid = start(env, [{'task_key': 't', 'title': 't'}])
    lab = env['lab']
    edges = []
    for frm in RUN_STATES:
        for to in RUN_STATES:
            with lab.store._tx() as cur:
                cur.execute('UPDATE nexus_runs SET state=%s WHERE id=%s', (frm, rid))
            try:
                result = lab.set_run_state(ALICE, env['project'], rid, to)['state']
            except InvalidTransition:
                result = 'refused'
            assert lab.run_row(rid)['state'] == (frm if result == 'refused' else to)
            edges.append(('run', frm, to, result))
    for frm in sorted(wf.STATES):
        for to in sorted(wf.STATES):
            try:
                result = wf.check_transition(frm, to)
            except ValueError:
                result = 'refused'
            edges.append(('workflow', frm, to, result))
    tasks = cp.ControlPlane(tmp_path / 'edges.db')
    try:
        tasks.submit_task('t', 'edge')
        for frm in cp.TaskState:
            for to in cp.TaskState:
                with tasks._tx() as tx:
                    tx.execute('UPDATE tasks SET state=? WHERE id=?', (frm.value, 't'))
                try:
                    result = tasks.transition('t', to).state.value
                except cp.InvalidTransition:
                    result = 'refused'
                assert tasks.get_task('t').state == (frm if result == 'refused' else to)
                edges.append(('task', frm.value, to.value, result))
    finally:
        tasks.close()
    probe = subprocess.run([BIN], input=''.join(' '.join(e[:3]) + '\n' for e in edges), text=True,
                           check=True, capture_output=True)
    rust = probe.stdout.splitlines()
    assert len(rust) == len(edges)
    matrix = [dict(machine=m, current=f, target=t, python=p, rust=r) for (m,f,t,p),r in zip(edges,rust)]
    if out := os.environ.get('NEXUS_EDGE_RESULTS'):
        Path(out).write_text(json.dumps(matrix, separators=(',', ':')) + '\n')
    assert not [e for e in matrix if e['python'] != e['rust']]
