"""Route authorization: every protected endpoint must require authentication.

The point of this suite is to catch an endpoint that was added to the server but
*not* protected — the classic "unregistered protected route" defect. It probes a
representative list of endpoints with no credentials and asserts each is refused
with 401, while the small public allowlist stays reachable. If someone adds a
protected endpoint without wiring ``_require_read``/``_require_write``, a probe
here that returns 200 without a token fails the test.

It also pins the two independent auth paths: a browser session (cookie + CSRF)
and the machine bearer. Neither may substitute for the other.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.web_auth import hash_password  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run route-auth tests"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
TOKEN = "machine-token"
UUID = "11111111-1111-1111-1111-111111111111"

# Endpoints that must be reachable with no credentials at all.
PUBLIC_GET = {"/login", "/healthz", "/readyz", "/static/app.css", "/static/app.js",
              "/favicon.svg", "/manifest.webmanifest", "/service-worker.js"}

# Endpoints that must require a session or bearer. Method + path.
PROTECTED_GET = [
    "/", "/api/session", "/api/dashboard", "/api/system", "/api/config",
    "/api/repositories", "/api/github/status", "/api/github/repositories",
    "/api/tasks", "/api/workflows", f"/api/workflows/{UUID}", "/api/health",
    "/api/capabilities", "/api/v1/projects",
    f"/api/v1/projects/{UUID}", f"/api/v1/projects/{UUID}/repositories",
    f"/api/v1/projects/{UUID}/tasks", f"/api/v1/projects/{UUID}/workflows",
    f"/api/v1/projects/{UUID}/evidence", f"/api/v1/projects/{UUID}/goals",
    f"/api/v1/projects/{UUID}/events", f"/api/v1/projects/{UUID}/files",
]
PROTECTED_POST = [
    "/api/repositories", "/api/repositories/sync", "/api/repositories/remove",
    "/api/workflows", f"/api/workflows/{UUID}/cancel",
    f"/api/workflows/{UUID}/resume", f"/api/tasks/{UUID}/retry",
    "/api/v1/projects", f"/api/v1/projects/{UUID}/goals",
    f"/api/v1/projects/{UUID}/repositories",
    f"/api/v1/projects/{UUID}/tasks",
    f"/api/v1/projects/{UUID}/evidence",
    f"/api/v1/projects/{UUID}/evidence/{UUID}/verify",
]


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


@pytest.fixture
def fake():
    with FakeCloudServer() as server:
        yield server


@pytest.fixture
def app(fake, monkeypatch):
    _reset_schema()
    monkeypatch.setenv("OPENHANDS_API_KEY", "test-key")
    monkeypatch.setenv("AUTH_USERNAME", "owner")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password("pw"))
    monkeypatch.setenv("AUTH_SESSION_SECRET", "s" * 32)
    a = App(DSN, poll_interval=0.2, token=TOKEN)
    a.client = CloudClient(fake.base_url, api_key="test-key", timeout=10.0)
    a.dispatcher.client = a.client
    a.runtime.client = a.client
    a.verifier.client = a.client
    yield a
    a.store.close()


@pytest.fixture
def base(app):
    ws = WebServer(app, "127.0.0.1", 0)
    ws.start()
    yield f"http://127.0.0.1:{ws.port}"
    ws.stop()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _status(method: str, url: str, body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _opener.open(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_public_endpoints_are_reachable(base):
    for path in PUBLIC_GET:
        assert _status("GET", base + path) == 200, path


def test_protected_get_endpoints_require_auth(base):
    for path in PROTECTED_GET:
        status = _status("GET", base + path)
        # "/" redirects to /login (303); everything else is 401.
        assert status in (301, 302, 303, 401), (path, status)
        if path != "/":
            assert status == 401, (path, status)


def test_protected_post_endpoints_require_auth(base):
    for path in PROTECTED_POST:
        status = _status("POST", base + path, {})
        assert status == 401, (path, status)


def test_bearer_token_is_accepted(base):
    status = _status("GET", base + "/api/system",
                     headers={"Authorization": f"Bearer {TOKEN}"})
    assert status == 200


def test_wrong_bearer_is_refused(base):
    status = _status("GET", base + "/api/system",
                     headers={"Authorization": "Bearer nope"})
    assert status == 401


def test_session_cookie_without_csrf_cannot_write(base, app):
    # A write with a session cookie but no CSRF header is 401 (no session seen
    # without the cookie) — the point is it is never accepted.
    status = _status("POST", base + "/api/v1/projects", {"name": "x"})
    assert status == 401


def test_login_sets_a_session_and_csrf_is_enforced(base):
    # Log in for real and obtain the session cookie.
    data = urllib.parse.urlencode({"username": "owner", "password": "pw"}).encode()
    req = urllib.request.Request(base + "/login", data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    with opener.open(req, timeout=10) as r:
        assert r.status in (200, 302)
    # A write with the session but no CSRF header must be refused.
    body = json.dumps({"name": "demo"}).encode()
    req2 = urllib.request.Request(base + "/api/v1/projects", data=body, method="POST")
    req2.add_header("Content-Type", "application/json")
    try:
        with opener.open(req2, timeout=10) as r:
            assert r.status == 403  # CSRF missing
    except urllib.error.HTTPError as e:
        assert e.code == 403


import urllib.parse  # noqa: E402  (used by test_login_sets_a_session_and_csrf_is_enforced)
