"""Python vs Rust HTTP API parity, on one real PostgreSQL.

The Rust API (`nexus-api`) serves the read routes and health probes; the Python
server serves the whole surface. Both are started on the **same database**, the
same projects are read, and the answers are compared field for field. This is
the Phase 2 API-compatibility gate: a route is only "at parity" when its JSON
matches for the same inputs.

Compared here:
* `GET /healthz` - identical body;
* `GET /readyz` - both `{"status":"ready","database":true}` on a live DB;
* `GET /api/v1/projects` - the same list (by id) for the same principal;
* `GET /api/v1/projects/{id}` - the same project;
* `GET /api/v1/projects/{id}/runs` - the same runs (by id and state).

Skips cleanly without cargo/`NEXUS_RUST_BIN` or `TEST_SUPABASE_DB_URL`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RUST = os.path.join(_REPO, "rust")

pytestmark = pytest.mark.skipif(
    shutil.which("cargo") is None and not os.environ.get("NEXUS_RUST_BIN"),
    reason="no Rust toolchain (set NEXUS_RUST_BIN to an already-built nexus-api)",
)


def _dsn() -> str | None:
    return os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")


def _rust_api_bin() -> str:
    override = os.environ.get("NEXUS_API_BIN")
    if override:
        return override
    if not hasattr(_rust_api_bin, "_path"):
        subprocess.run(
            ["cargo", "build", "--quiet", "-p", "nexus-api"], cwd=_RUST, check=True,
            env={**os.environ, "CARGO_TERM_COLOR": "never"},
        )
        _rust_api_bin._path = os.path.join(_RUST, "target", "debug", "nexus-api")
    return _rust_api_bin._path


def _reset_schema() -> None:
    import psycopg

    mig = os.path.join(_REPO, "migrations")
    with psycopg.connect(_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(mig)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(mig, name)).read())


def _get(url: str, *, headers: dict | None = None) -> tuple[int, dict | None]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return e.code, (json.loads(body) if body else None)


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def stacks(monkeypatch):
    if not _dsn():
        pytest.skip("set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")
    _reset_schema()
    monkeypatch.setenv("OPENHANDS_API_KEY", "parity-key")
    monkeypatch.setenv("NEXUS_SECRET_KEY", "parity-secret-key")
    monkeypatch.setenv("AUTH_USERNAME", "owner")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", "pbkdf2_sha256$1$AAAA$AAAA")  # unused here
    monkeypatch.setenv("AUTH_SESSION_SECRET", "p" * 32)
    monkeypatch.setenv("NEXUS_LAB_INLINE", "0")  # no background loop during the test

    from orchestration.cloud_store import PostgresStore
    from orchestration.nexus_projects import ProjectService
    from orchestration.web_server import App, WebServer

    # Seed a principal and a project with a run.
    store = PostgresStore(_dsn())
    svc = ProjectService(store)
    project = svc.create("user:owner", "API parity project")
    goal = svc.create_goal("user:owner", str(project["id"]), "g")[0]["id"]
    from orchestration.nexus_lab_store import LabStore

    lab = LabStore(store)
    lab.start_run("user:owner", str(project["id"]), str(goal), policy="durable")
    store.close()

    # Python server (bearer token so no session is needed).
    token = "machine-token"
    app = App(_dsn(), poll_interval=0.2, token=token, lab_inline=False)
    py_server = WebServer(app, "127.0.0.1", 0)
    py_server.start()
    py_base = f"http://127.0.0.1:{py_server.port}"

    # Rust server.
    port = _free_port()
    log = open("/tmp/nexus_api_parity.log", "w")
    proc = subprocess.Popen(
        [_rust_api_bin()], env={**os.environ, "NEXUS_DB_URL": _dsn(),
                                "NEXUS_API_ADDR": f"127.0.0.1:{port}",
                                "NEXUS_API_TRUST_PRINCIPAL_HEADER": "1"},
        stdout=log, stderr=log,
    )
    rs_base = f"http://127.0.0.1:{port}"
    # Wait for it to accept connections.
    for _ in range(100):
        try:
            _get(rs_base + "/healthz")
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    try:
        yield {"py": py_base, "rs": rs_base, "token": token,
               "project": str(project["id"])}
    finally:
        py_server.stop()
        app.store.close()
        proc.terminate()
        proc.wait(timeout=5)


def test_healthz_matches(stacks):
    _, py = _get(stacks["py"] + "/healthz")
    _, rs = _get(stacks["rs"] + "/healthz")
    assert py == rs == {"status": "ok"}


def test_readyz_matches(stacks):
    py_status, py = _get(stacks["py"] + "/readyz")
    rs_status, rs = _get(stacks["rs"] + "/readyz")
    assert py_status == rs_status == 200
    assert py == rs == {"status": "ready", "database": True}


def test_list_projects_matches(stacks):
    headers = {"Authorization": f"Bearer {stacks['token']}"}
    _, py = _get(stacks["py"] + "/api/v1/projects", headers=headers)
    _, rs = _get(stacks["rs"] + "/api/v1/projects",
                 headers={"X-Nexus-Principal": "user:owner"})
    # Both wrap the list in {"projects": [...]}.
    py_projects, rs_projects = py["projects"], rs["projects"]
    assert sorted(p["id"] for p in py_projects) == sorted(p["id"] for p in rs_projects)
    assert py_projects[0]["name"] == rs_projects[0]["name"] == "API parity project"


def test_get_project_matches(stacks):
    headers = {"Authorization": f"Bearer {stacks['token']}"}
    pid = stacks["project"]
    _, py = _get(f"{stacks['py']}/api/v1/projects/{pid}", headers=headers)
    _, rs = _get(f"{stacks['rs']}/api/v1/projects/{pid}",
                 headers={"X-Nexus-Principal": "user:owner"})
    # Both wrap the project in {"project": {...}}.
    assert py["project"]["id"] == rs["project"]["id"] == pid
    assert py["project"]["name"] == rs["project"]["name"]


def test_list_runs_matches(stacks):
    headers = {"Authorization": f"Bearer {stacks['token']}"}
    pid = stacks["project"]
    _, py = _get(f"{stacks['py']}/api/v1/projects/{pid}/runs", headers=headers)
    _, rs = _get(f"{stacks['rs']}/api/v1/projects/{pid}/runs",
                 headers={"X-Nexus-Principal": "user:owner"})
    py_runs = {(r["id"], r["state"]) for r in py["runs"]}
    rs_runs = {(r["id"], r["state"]) for r in rs["runs"]}
    assert py_runs == rs_runs
    assert len(py_runs) == 1
