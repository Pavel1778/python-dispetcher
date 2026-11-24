"""Exercise the Debian operator command without touching host units or updating the checkout."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "nexus.sh"
WEB = "nexus-web"
WORKER = "nexus-lab-worker"
PULL = "git pull --ff-only"
INSTALL = "python3 -m pip install -r requirements.txt"


@pytest.fixture
def invoke(tmp_path):
    script = tmp_path / "scripts" / SCRIPT.name
    script.parent.mkdir()
    shutil.copy2(SCRIPT, script)
    calls = tmp_path / "calls"
    calls.touch()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("systemctl", "git", "python3"):
        shim = bin_dir / name
        shim.write_text('#!/bin/sh\ncall="${0##*/} $*"\n'
                        'echo "$call" >> "$CALLS"\n'
                        'if grep -Fxq -- "$call" "$REFUSE"; then exit 1; fi\n')
        shim.chmod(0o755)
    # Retry immediately, but use real curl and real HTTP responses on loopback.
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "sleep").chmod(0o755)

    def run(command, *, refused=(), ready=(200,), configured=True):
        refuse = tmp_path / "refuse"
        refuse.write_text("\n".join(refused))
        codes = iter(ready)

        class Probe(BaseHTTPRequestHandler):
            def do_GET(self):
                code = 200 if self.path == "/healthz" else 404
                if self.path == "/readyz":
                    code = next(codes, ready[-1])
                with calls.open("a") as f:
                    print(f"GET {self.path} {code}", file=f)
                self.send_response(code)
                if code == 302:
                    self.send_header("Location", "/healthz")
                self.end_headers()

            def log_message(self, *args):
                pass

        with HTTPServer(("127.0.0.1", 0), Probe) as http:
            env_file = tmp_path / "operator.env"
            if configured:
                env_file.write_text(f"PORT={http.server_port}\n")
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            try:
                result = subprocess.run(
                    [script, command], cwd=tmp_path, text=True, capture_output=True, timeout=10,
                    env={"HOME": str(tmp_path), "PATH": f"{bin_dir}:{os.defpath}", "PORT": "0",
                         "NEXUS_ENV_FILE": str(env_file), "CALLS": str(calls), "REFUSE": str(refuse)},
                )
            finally:
                http.shutdown()
                thread.join()
        return result, calls.read_text().splitlines()

    return run


@pytest.mark.parametrize("failed_units", [(), (WORKER,), (WEB,), (WORKER, WEB)])
def test_stop_attempts_both_units_and_reports_any_refusal(invoke, failed_units):
    result, calls = invoke("stop", refused=[f"systemctl stop {u}" for u in failed_units],
                           configured=False)
    assert calls == [f"systemctl stop {WORKER}", f"systemctl stop {WEB}"]
    assert (result.returncode == 0) == (not failed_units)
    assert ("services stopped" in result.stdout) == (not failed_units)
    for unit in failed_units:
        assert unit in result.stderr


def test_restart_does_not_start_after_a_partial_stop(invoke):
    result, calls = invoke("restart", refused=[f"systemctl stop {WORKER}"])
    assert result.returncode != 0
    assert calls == [f"systemctl stop {WORKER}", f"systemctl stop {WEB}"]


def test_restart_checks_for_the_env_file_before_stopping_anything(invoke):
    result, calls = invoke("restart", configured=False)
    assert result.returncode != 0
    assert not calls


@pytest.mark.parametrize("code", [503, 302])
def test_liveness_or_a_redirect_is_not_readiness(invoke, code):
    result, calls = invoke("start", ready=[code])
    assert result.returncode != 0
    assert f"GET /readyz {code}" in calls
    assert f"systemctl start {WORKER}" not in calls
    assert "/readyz" in result.stderr


@pytest.mark.parametrize("unit", [WEB, WORKER])
@pytest.mark.parametrize("verb", ["start", "is-active --quiet"])
def test_refused_start_or_inactive_unit_is_not_success(invoke, verb, unit):
    result, calls = invoke("start", refused=[f"systemctl {verb} {unit}"])
    assert result.returncode != 0
    assert unit in result.stderr
    if unit == WEB:
        assert f"systemctl start {WORKER}" not in calls


@pytest.mark.parametrize("failed_step", [PULL, INSTALL])
def test_update_does_not_restart_after_a_failed_prerequisite(invoke, failed_step):
    result, calls = invoke("update", refused=[failed_step])
    assert result.returncode != 0
    assert calls == ([PULL] if failed_step == PULL else [PULL, INSTALL])


def test_update_restarts_the_two_units_in_readiness_order(invoke):
    result, calls = invoke("update", ready=[503, 200])
    assert result.returncode == 0, result.stderr
    changes = [call for call in calls if not call.startswith(("GET", "systemctl is-active"))]
    assert changes == [PULL, INSTALL, f"systemctl stop {WORKER}", f"systemctl stop {WEB}",
                       f"systemctl start {WEB}", f"systemctl start {WORKER}"]
    web_start = calls.index(f"systemctl start {WEB}")
    worker_start = calls.index(f"systemctl start {WORKER}")
    assert web_start < calls.index("GET /readyz 503") < calls.index("GET /readyz 200") < worker_start


def test_status_probes_the_port_from_the_env_file(invoke):
    result, calls = invoke("status")
    assert result.returncode == 0, result.stderr
    assert "GET /healthz 200" in calls
    assert "GET /readyz 200" in calls
    assert not any(call.startswith(("systemctl start", "systemctl stop")) for call in calls)
