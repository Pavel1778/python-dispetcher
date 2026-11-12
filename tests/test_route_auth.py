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
              "/favicon.svg", "/manifest.webmanifest", "/service-worker.js",
              # The login page is styled before there is a session, so the
              # stylesheet, the scripts and the fonts are public. They hold no data.
              "/static/boot.js", "/static/markdown.js", "/static/highlight.js",
              "/static/fonts/inter-latin.woff2", "/static/fonts/inter-cyrillic.woff2",
              "/static/fonts/jetbrains-mono-latin.woff2",
              "/static/fonts/jetbrains-mono-cyrillic.woff2", "/favicon.ico"}

# Endpoints that must require a session or bearer. Method + path.
PROTECTED_GET = [
    "/", "/api/session", "/api/studio", "/api/dashboard", "/api/system", "/api/config",
    "/api/repositories", "/api/github/status", "/api/github/repositories",
    "/api/tasks", "/api/workflows", f"/api/workflows/{UUID}", "/api/health",
    "/api/capabilities", "/api/v1/projects",
    f"/api/v1/projects/{UUID}", f"/api/v1/projects/{UUID}/repositories",
    f"/api/v1/projects/{UUID}/tasks", f"/api/v1/projects/{UUID}/workflows",
    f"/api/v1/projects/{UUID}/evidence", f"/api/v1/projects/{UUID}/goals",
    f"/api/v1/projects/{UUID}/events", f"/api/v1/projects/{UUID}/files",
    f"/api/v1/projects/{UUID}/conversations",
    f"/api/v1/projects/{UUID}/conversations/{UUID}",
    f"/api/v1/projects/{UUID}/conversations/{UUID}/messages",
    f"/api/v1/projects/{UUID}/agents", f"/api/v1/projects/{UUID}/binding",
    "/api/openhands/accounts", f"/api/openhands/accounts/{UUID}",
    "/api/openhands/pool", "/api/openhands/diagnostics", "/api/providers",
    f"/api/github/accounts/{UUID}/repositories",
    "/api/github/accounts", f"/api/github/accounts/{UUID}",
    f"/api/github/accounts/{UUID}/repositories/owner/name/branches",
    "/api/github/repositories/owner/name/branches",
    f"/api/openhands/accounts/{UUID}/capabilities",
    f"/api/openhands/accounts/{UUID}/errors",
    f"/api/v1/projects/{UUID}/workflows/{UUID}", f"/api/v1/projects/{UUID}/runs/{UUID}",
    f"/api/v1/projects/{UUID}/context",
    f"/api/v1/projects/{UUID}/github/repositories/owner/name/tree",
    f"/api/v1/projects/{UUID}/github/repositories/owner/name/contents/README.md",
    # A path under /static/ that is not a listed asset is not public either.
    "/static/app.js.map", "/static/index.html",
]
PROTECTED_POST = [
    "/mcp",
    "/api/repositories", "/api/repositories/sync", "/api/repositories/remove",
    "/api/workflows", f"/api/workflows/{UUID}/cancel",
    f"/api/workflows/{UUID}/resume", f"/api/tasks/{UUID}/retry",
    "/api/v1/projects", f"/api/v1/projects/{UUID}/goals",
    f"/api/v1/projects/{UUID}/repositories",
    f"/api/v1/projects/{UUID}/tasks",
    f"/api/v1/projects/{UUID}/evidence",
    f"/api/v1/projects/{UUID}/evidence/{UUID}/verify",
    f"/api/v1/projects/{UUID}/conversations",
    f"/api/v1/projects/{UUID}/conversations/{UUID}/messages",
    f"/api/v1/projects/{UUID}/binding", f"/api/v1/projects/{UUID}/binding/branch",
    f"/api/v1/projects/{UUID}/repository", f"/api/v1/projects/{UUID}/context/refresh",
    f"/api/v1/projects/{UUID}/runs/{UUID}/pause", f"/api/v1/projects/{UUID}/runs/{UUID}/cancel",
    f"/api/v1/projects/{UUID}/runs/{UUID}/tasks/{UUID}/switch",
    "/api/github/accounts", f"/api/github/accounts/{UUID}/revoke",
    f"/api/v1/projects/{UUID}/openhands-account",
    "/api/openhands/accounts",
    f"/api/openhands/accounts/{UUID}/verify",
    "/api/openhands/pool", "/api/openhands/diagnostics",
    f"/api/openhands/accounts/{UUID}/enable",
    f"/api/openhands/accounts/{UUID}/priority",
    f"/api/openhands/accounts/{UUID}/remove",
    f"/api/openhands/accounts/{UUID}/edit",
    f"/api/openhands/accounts/{UUID}/connection",
    f"/api/github/accounts/{UUID}/verify",
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


def test_protected_patch_and_delete_endpoints_require_auth(base):
    """The studio renames and archives with PATCH; neither works without a credential."""
    for method, path, body in [
        ("PATCH", f"/api/v1/projects/{UUID}", {"name": "x"}),
        ("PATCH", f"/api/v1/projects/{UUID}/conversations/{UUID}", {"archived": True}),
        ("PATCH", f"/api/v1/projects/{UUID}/conversations/{UUID}", {"title": "x"}),
        ("DELETE", f"/api/v1/projects/{UUID}/conversations/{UUID}", None),
    ]:
        assert _status(method, base + path, body) == 401, (method, path)


def test_bearer_token_is_accepted(base):
    status = _status("GET", base + "/api/system",
                     headers={"Authorization": f"Bearer {TOKEN}"})
    assert status == 200


def test_wrong_bearer_is_refused(base):
    status = _status("GET", base + "/api/system",
                     headers={"Authorization": "Bearer nope"})
    assert status == 401


def test_the_bearer_is_the_whole_token_or_nothing(base):
    """Compared whole, as bytes, in constant time: nearly right is wrong, and a
    header that is not ASCII gets an answer, not an exception."""
    import http.client
    from urllib.parse import urlparse

    where = urlparse(base)

    def status_for(header: bytes) -> int:
        conn = http.client.HTTPConnection(where.hostname, where.port, timeout=10)
        try:
            conn.putrequest("GET", "/api/system")
            conn.putheader("Authorization", header)
            conn.endheaders()
            return conn.getresponse().status
        finally:
            conn.close()

    right = f"Bearer {TOKEN}".encode()
    assert status_for(right) == 200
    for wrong in (
        right + b"x",                                  # the token and more
        right[:-1],                                    # all of it but the last character
        right[:-1] + b"X",                             # the same length, one character off
        b"Bearer  " + TOKEN.encode(),
        b"Basic " + TOKEN.encode(),
        b"Bearer",
        right[:-1] + b"\xe9",                          # not ASCII
        right + b"\xc3\xa9",
        b"Bearer \xff\xfe\xfd",
    ):
        assert status_for(wrong) == 401, wrong


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


def test_the_studio_writes_need_the_csrf_token_of_their_session(base):
    """Every write the page makes carries X-CSRF-Token; without it a session is not enough."""
    data = urllib.parse.urlencode({"username": "owner", "password": "pw"}).encode()
    login = urllib.request.Request(base + "/login", data=data, method="POST")
    login.add_header("Content-Type", "application/x-www-form-urlencoded")
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    with opener.open(login, timeout=10):
        pass

    def call(method: str, path: str, body: dict | None, csrf: str | None = None):
        raw = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(base + path, data=raw, method=method)
        if raw is not None:
            request.add_header("Content-Type", "application/json")
        if csrf:
            request.add_header("X-CSRF-Token", csrf)
        try:
            with opener.open(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")

    status, session = call("GET", "/api/session", None)
    assert status == 200 and session["username"] == "owner"
    csrf = session["csrf_token"]
    assert len(csrf) >= 32
    # Reads work with the cookie alone; the page status is one of them.
    assert call("GET", "/api/studio", None)[0] == 200

    status, created = call("POST", "/api/v1/projects", {"name": "CSRF"}, csrf)
    assert status == 201
    project = created["project"]["id"]
    status, conversation = call("POST", f"/api/v1/projects/{project}/conversations", {"title": "t"}, csrf)
    assert status == 201
    cid = conversation["conversation"]["id"]

    writes = [
        ("POST", f"/api/v1/projects/{project}/repository", {"full_name": "owner/name"}),
        ("POST", f"/api/v1/projects/{project}/conversations", {"title": "x"}),
        ("POST", f"/api/v1/projects/{project}/conversations/{cid}/messages", {"text": "x"}),
        ("PATCH", f"/api/v1/projects/{project}/conversations/{cid}", {"archived": True}),
        ("PATCH", f"/api/v1/projects/{project}", {"name": "renamed"}),
        ("POST", "/api/github/accounts", {"label": "x", "token": "y"}),
    ]
    for method, path, body in writes:
        assert call(method, path, body)[0] == 403, (method, path, "no token")
        assert call(method, path, body, "0" * len(csrf))[0] == 403, (method, path, "wrong token")
    # Nothing was changed by the refused requests.
    assert call("GET", f"/api/v1/projects/{project}", None)[1]["project"]["name"] == "CSRF"
    listed = call("GET", f"/api/v1/projects/{project}/conversations", None)[1]["conversations"]
    assert [c["archived"] for c in listed] == [False]
    # ... and with the token the same write is accepted.
    assert call("PATCH", f"/api/v1/projects/{project}/conversations/{cid}", {"archived": True}, csrf)[0] == 200


import urllib.parse  # noqa: E402  (used by the two session tests above)
