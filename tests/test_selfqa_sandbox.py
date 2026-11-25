"""Container-mode wiring. A real Docker daemon is tested only when provided."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestration import nexus_selfqa as qa
from tests.test_selfqa import _make_repo

IMAGE = "example/qa@sha256:" + "a" * 64
nonroot_posix = pytest.mark.skipif(os.name != 'posix' or os.getuid() == 0,
                                  reason='container mode requires a non-root POSIX worker')


def test_missing_docker_blocks_instead_of_running_on_host(tmp_path, monkeypatch):
    repo, head = _make_repo(tmp_path)
    sentinel = tmp_path / "must-not-run"
    which = qa.shutil.which
    monkeypatch.setattr(qa.shutil, "which", lambda name: None if name == "docker" else which(name))
    check = qa.QaCheck("danger", (sys.executable, "-c", f"open({str(sentinel)!r},'w').write('x')"))
    result = qa.SelfQa(runtime="docker", image=IMAGE).run(repository=repo, commit=head, checks=[check])
    assert result.verdict == "blocked"
    assert not sentinel.exists()
    assert result.environment["runner"] == "docker"


def test_invalid_runtime_and_unpinned_image_fail_closed(tmp_path, monkeypatch):
    repo, head = _make_repo(tmp_path)
    monkeypatch.setattr(qa.shutil, "which", lambda _name: "/usr/bin/docker")
    assert qa.SelfQa(runtime="dockre").run(repository=repo, commit=head).verdict == "blocked"
    assert qa.SelfQa(runtime="docker", image="example/qa:latest").run(
        repository=repo, commit=head).verdict == "blocked"


@nonroot_posix
def test_container_command_is_restricted_and_never_runs_check_on_host(tmp_path, monkeypatch):
    repo, head = _make_repo(tmp_path)
    sentinel = tmp_path / "host-sentinel"
    check = qa.QaCheck("safe", ("python", "-c", f"open({str(sentinel)!r},'w').write('x')"))
    calls = []
    monkeypatch.setenv("OPENHANDS_API_KEY", "private-value")
    which = qa.shutil.which
    monkeypatch.setattr(qa.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else which(name))

    def fake_run(argv, *, root, timeout, env):
        calls.append((argv, root, env))
        assert not (root / ".git").exists()
        return 0, b"ok", b"", False

    monkeypatch.setattr(qa, "_run_bounded", fake_run)
    report = qa.SelfQa(runtime="docker", image=IMAGE).run(repository=repo, commit=head, checks=[check])
    assert report.verdict == "passed"
    assert report.checked_sha == head
    assert report.checks[0].stdout == "ok"
    assert not sentinel.exists()
    args, _root, env = calls[0]
    assert args[:3] == ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
    for flag in ("--pull=never", "--label=nexus.selfqa.managed=true", "--network=none", "--read-only", "--cap-drop=ALL",
                 "--security-opt=no-new-privileges", f"--user={os.getuid()}:{os.getgid()}", "--pids-limit=128",
                 "--memory=512m", "--memory-swap=512m", "--cpus=1"):
        assert flag in args
    assert any(value.startswith("type=bind,") and value.endswith(",dst=/workspace,readonly") for value in args)
    assert args[-5:] == ["--entrypoint", "python", IMAGE, "-c", check.argv[2]]
    assert "OPENHANDS_API_KEY" not in env


@nonroot_posix
def test_timeout_removes_container_and_docker_errors_do_not_become_success(tmp_path, monkeypatch):
    monkeypatch.setattr(qa.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(qa, "_run_bounded", lambda *a, **kw: (None, b"partial", b"", True))
    calls = []

    def cleanup(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(qa.subprocess, "run", cleanup)
    result = qa.run_docker_check(qa.QaCheck("timeout", ("python", "test.py")), tmp_path,
                                 timeout=0.1, image=IMAGE)
    assert result.status == "timeout"
    assert result.stdout == "partial"
    assert calls and calls[0][:5] == ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", "rm", "-f"]
    monkeypatch.setattr(qa, "_run_bounded", lambda *a, **kw: (125, b"", b"failure", False))
    with pytest.raises(qa.SandboxUnavailable):
        qa.run_docker_check(qa.QaCheck("bad", ("python",)), tmp_path, timeout=1, image=IMAGE)


@nonroot_posix
def test_failed_container_cleanup_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(qa.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(qa, "_run_bounded", lambda *a, **kw: (None, b"", b"", True))
    monkeypatch.setattr(qa.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 1))
    with pytest.raises(qa.SandboxUnavailable, match="cleanup"):
        qa.run_docker_check(qa.QaCheck("bad", ("python",)), tmp_path, timeout=1, image=IMAGE)


@nonroot_posix
@pytest.mark.skipif(not os.environ.get("NEXUS_TEST_DOCKER_IMAGE"),
                    reason="set NEXUS_TEST_DOCKER_IMAGE to a preloaded digest-pinned Python image")
def test_real_docker_restricts_network_and_writes(tmp_path):
    image = os.environ["NEXUS_TEST_DOCKER_IMAGE"]
    root = Path(tmp_path / "checkout")
    root.mkdir(mode=0o755)
    host_secret = tmp_path / "host-secret"
    host_secret.write_text("not shared with the container")
    code = ("import os; assert os.geteuid()!=0; "
            "assert not os.access('/workspace',os.W_OK); "
            "assert not os.path.exists('/var/run/docker.sock'); "
            f"assert not os.path.exists({str(host_secret)!r}); "
            "assert set(os.listdir('/sys/class/net'))=={'lo'}")
    result = qa.run_docker_check(qa.QaCheck("isolation", ("python", "-c", code)), root,
                                 timeout=30, image=image)
    assert result.status == "passed", result.as_dict()


@pytest.mark.skipif(os.name != 'posix', reason='POSIX user IDs required')
def test_container_refuses_a_root_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(qa.shutil, 'which', lambda _name: '/usr/bin/docker')
    monkeypatch.setattr(qa.os, 'getuid', lambda: 0)
    with pytest.raises(qa.SandboxUnavailable, match='non-root'):
        qa.run_docker_check(qa.QaCheck('unsafe', ('python',)), tmp_path, timeout=1, image=IMAGE)


@nonroot_posix
def test_container_refuses_remote_or_invalid_socket(tmp_path, monkeypatch):
    monkeypatch.setattr(qa.shutil, 'which', lambda _name: '/usr/bin/docker')
    monkeypatch.setenv('NEXUS_SELFQA_DOCKER_SOCKET', 'tcp://attacker:2375')
    with pytest.raises(qa.SandboxUnavailable, match='socket'):
        qa.run_docker_check(qa.QaCheck('unsafe', ('python',)), tmp_path, timeout=1, image=IMAGE)


def test_per_project_engine_keeps_sandbox_settings():
    engine = qa.SelfQa(runtime='docker', image=IMAGE)
    gate = qa.ProjectQualityGate(engine, token_for=lambda *_args: 'project-token')
    derived = gate._engine_for('project', 'owner/repo')
    assert derived.runtime == 'docker' and derived.image == IMAGE
    assert derived.token == 'project-token'
    assert not engine.token


def test_signed_receipt_rejects_an_unpinned_image():
    result = qa.QaReport(verdict='passed', reason='ok', repository='o/r', commit='a' * 40,
                         checked_sha='a' * 40, binding={'project': 'p'},
                         started_utc=1, finished_utc=2,
                         environment={'runner': 'docker', 'image': 'latest'},
                         checks=[qa.QaResult('check', True, 'passed', exit_code=0, argv=('python',))])
    with pytest.raises(ValueError, match='image'):
        qa.checked_receipt(result, {'project': 'p'}, 'o/r', 'a' * 40, 0, 3)
