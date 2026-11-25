"""Benchmark safety and workload regressions."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from benchmarks.dispatcher_local import _InstantClient, _seed
from orchestration.control_plane import ControlPlane, TaskState
from orchestration.dispatcher import Dispatcher


def test_postgres_benchmark_refuses_non_disposable_database():
    env = {**os.environ, "NEXUS_BENCHMARK_DATABASE_URL": "postgresql://localhost/production"}
    script = Path(__file__).resolve().parents[1] / "benchmarks" / "dispatcher_postgres.py"
    result = subprocess.run([sys.executable, str(script), "--allow-disposable-writes"],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "nexus_bench_*" in result.stderr


def test_local_benchmark_keeps_seeded_tasks_live(tmp_path):
    cp = ControlPlane(tmp_path / "bench.db")
    try:
        _seed(cp, backlog=2, active=3, queued=0)
        dispatcher = Dispatcher(cp, _InstantClient(), worker_id="w1", workspace_root="/tmp/ws",
                                desired_state_path=None)
        for _ in range(3):
            dispatcher.tick()
        running = cp.list_tasks(state=TaskState.RUNNING)
        assert len(running) == 3
        assert all(task.conversation_id and task.lease_live for task in running)
    finally:
        cp.close()
