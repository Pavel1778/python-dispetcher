"""The worker's launch artifacts: a systemd unit, Compose and Render services.

The autonomous lab is advanced by a separate process. These tests check that
the deployment files that *start* it are present and consistent, without
starting anything: the unit file parses, every launch path runs the same module
with the same database, and the inline loop's switch is documented. No database
and no network are needed.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNIT = os.path.join(ROOT, "packaging", "systemd", "nexus-lab-worker.service")
COMPOSE = os.path.join(ROOT, "docker-compose.yml")
RENDER = os.path.join(ROOT, "render.yaml")

_WORKER_CMD = re.compile(r"python(?:3(?:\.\d+)?)?[\s\",]+-m[\s\",]+orchestration\.lab_worker")


def _runs_the_worker(text: str) -> bool:
    """Whether *text* starts the worker, in shell form or in a YAML list form."""
    return bool(_WORKER_CMD.search(text))


def test_the_systemd_unit_is_present_and_runs_the_worker_module():
    text = open(UNIT, encoding="utf-8").read()
    assert _WORKER_CMD.search(text), "the unit does not start the lab worker"
    assert "EnvironmentFile=" in text, "the unit must read the shared environment"
    assert "Restart=always" in text, "a worker that dies must come back"
    assert "WantedBy=multi-user.target" in text
    # A stop signal is the graceful shutdown the loop handles.
    assert "SIGTERM" in text or "KillSignal" in text


@pytest.mark.skipif(not shutil.which("systemd-analyze"),
                    reason="systemd-analyze is not installed")
def test_the_systemd_unit_parses(tmp_path):
    """`systemd-analyze verify` accepts the unit (a real syntax check)."""
    # The unit names paths that need not exist here; verify reports those as
    # warnings, not errors, and exits non-zero on a real syntax error.
    result = subprocess.run(["systemd-analyze", "verify", UNIT],
                            capture_output=True, text=True)
    assert "Unknown section" not in result.stderr, result.stderr
    assert "Unknown key" not in result.stderr, result.stderr


def test_docker_compose_starts_the_worker_beside_the_app():
    text = open(COMPOSE, encoding="utf-8").read()
    assert _WORKER_CMD.search(text), "compose does not start the lab worker"
    # The worker and the app must share the database service.
    assert text.count("SUPABASE_DB_URL: postgresql://nexus:nexus@db:5432/nexus") >= 2


def test_render_defines_a_worker_service():
    text = open(RENDER, encoding="utf-8").read()
    assert "type: worker" in text, "render.yaml has no worker service"
    assert _WORKER_CMD.search(text), "the render worker does not run the lab worker"


def test_the_worker_module_is_the_same_one_every_launch_path_names():
    """systemd, Compose and Render start the same module - not three forks."""
    for path in (UNIT, COMPOSE, RENDER):
        assert _WORKER_CMD.search(open(path, encoding="utf-8").read()), path


def test_worker_setup_docs_describe_every_launch_mechanism():
    doc = open(os.path.join(ROOT, "docs", "WORKER_SETUP.md"), encoding="utf-8").read()
    assert "python -m orchestration.lab_worker" in doc
    for mechanism in ("systemd", "Docker Compose", "Render"):
        assert mechanism in doc, f"{mechanism} is not documented"
    assert "NEXUS_LAB_INLINE" in doc, "the inline switch must be explained"
