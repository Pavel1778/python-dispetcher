"""Login, session and CSRF tests against the real server over real HTTP.

The server is the real :class:`orchestration.web_server.WebServer`; requests go
over a socket with a cookie jar, so the whole path (cookie parsing, session
lookup, CSRF check, logout) is exercised. Only the Cloud API is a local fake.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.web_auth import (  # noqa: E402
    SESSION_COOKIE,
    AuthService,
    hash_password,
    verify_password,
)
from orchestration.web_server import App, WebServer  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run auth tests"
)

_MIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"
)

PASSWORD = "correct horse battery staple"


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DROP TABLE IF EXISTS sessions, login_attempts, sr_tool_calls, "
                "task_events, conversations, tasks, repositories, workflows CASCADE"
            )
            for name in ("0001_init.sql", "0002_sr_runtime.sql",
                         "0003_tool_call_recovery.sql", "0004_auth.sql"):
                cur.execute(open(os.path.join(_MIG, name)).read())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn redirects into a normal response so tests can inspect them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Browser:
    """A cookie-jar HTTP client that speaks the login form and JSON API."""

    def __init__(self, base: str):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def request(self, method, path, *, data=None, form=None, headers=None,
                follow=True):
        req = urllib.request.Request(self.base + path, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if form is not None:
            req.data = urllib.parse.urlencode(form).encode()
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        elif data is not None:
            req.data = json.dumps(data).encode()
            req.add_header("Content-Type", "application/json")
        opener = self.opener
        if not follow:
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(self.jar),
                _NoRedirect(),
            )
        try:
            with opener.open(req, timeout=10) as r:
                raw = r.read().decode()
                return r.status, raw, dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)

    def json(self, method, path, **kw):
        st, raw, hdr = self.request(method, path, **kw)
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = {"raw": raw}
        return st, body, hdr

    def login(self, username="owner", password=PASSWORD):
        return self.request("POST", "/login", form={"username": username,
                                                    "password": password})

    def cookie(self, name=SESSION_COOKIE):
        for c in self.jar:
            if c.name == name:
                return c
        return None


@pytest.fixture(autouse=True)
def _schema():
    _reset_schema()
    yield


@pytest.fixture
def fake():
    with FakeCloudServer() as server:
        yield server


@pytest.fixture
def server(fake, monkeypatch):
    monkeypatch.setenv("OPENHANDS_API_KEY", "test-key")
    monkeypatch.setenv("AUTH_USERNAME", "owner")
    monkeypatch.setenv("AUTH_SESSION_SECRET", "unit-test-session-secret")
    # A low iteration count keeps the suite fast; production uses the default.
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password(PASSWORD, iterations=1000))
    app = App(DSN, poll_interval=3600, token="machine-token")
    app.client = CloudClient(fake.base_url, api_key="test-key", timeout=5.0)
    app.dispatcher.client = app.client
    app.runtime.client = app.client
    ws = WebServer(app, "127.0.0.1", 0)
    ws.start()
    yield ws
    ws.stop()


@pytest.fixture
def browser(server):
    return Browser(f"http://127.0.0.1:{server.port}")


# ------------------------------------------------------------ password hash
def test_password_hash_roundtrip():
    stored = hash_password("s3cret", iterations=1000)
    assert verify_password("s3cret", stored) is True
    assert verify_password("wrong", stored) is False
    assert stored.startswith("pbkdf2_sha256$")
    assert "s3cret" not in stored  # the password is never stored in the clear


def test_password_hash_rejects_malformed():
    assert verify_password("x", "not-a-hash") is False
    assert verify_password("x", "") is False


# ------------------------------------------------------------------- login
def test_login_page_is_public(browser):
    st, raw, _ = browser.request("GET", "/login")
    assert st == 200 and "Войти" in raw


def test_root_redirects_to_login_when_anonymous(browser):
    # urllib follows the 303 to /login, so the final body is the login page.
    st, raw, _ = browser.request("GET", "/")
    assert st == 200 and "Войти" in raw


def test_anonymous_api_is_401(browser):
    st, body, _ = browser.json("GET", "/api/dashboard")
    assert st == 401


def test_wrong_password_is_rejected_without_enumeration(browser):
    st, raw, _ = browser.login(password="nope")
    assert st == 401 and "Неверный" in raw
    # Unknown user gets the same status and the same message.
    st2, raw2, _ = browser.login(username="ghost", password="nope")
    assert st2 == st and ("Неверный" in raw2) == ("Неверный" in raw)


def test_correct_credentials_create_session(browser):
    st, raw, hdr = browser.login()
    assert st == 200
    cookie = browser.cookie()
    assert cookie is not None
    st, body, _ = browser.json("GET", "/api/session")
    assert st == 200 and body["username"] == "owner"
    assert body["csrf_token"]


def test_session_cookie_attributes(browser):
    # Inspect the raw Set-Cookie without following the 303 redirect.
    st, raw, hdr = browser.request("POST", "/login",
                                   form={"username": "owner", "password": PASSWORD},
                                   follow=False)
    assert st == 303
    set_cookie = hdr.get("Set-Cookie", "")
    assert SESSION_COOKIE + "=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Strict" in set_cookie
    assert "Path=/" in set_cookie
    assert "Max-Age=" in set_cookie
    # Secure is not asserted here: this test speaks plain HTTP, so the server
    # (correctly) omits it. test_cookie_is_secure_on_https covers the HTTPS case.


def test_cookie_is_secure_on_https(server, browser):
    # Simulate a TLS-terminating proxy, as Render is.
    st, raw, hdr = browser.request(
        "POST", "/login",
        form={"username": "owner", "password": PASSWORD},
        headers={"X-Forwarded-Proto": "https"}, follow=False,
    )
    assert st == 303
    assert "Secure" in hdr.get("Set-Cookie", "")


def test_login_page_redirects_when_already_logged_in(browser):
    browser.login()
    st, raw, _ = browser.request("GET", "/login")
    # Followed the redirect to the dashboard, not the login form.
    assert "Диспетчер OpenHands" in raw


# --------------------------------------------------------------- protected
def test_protected_reads_need_session(browser):
    st, _, _ = browser.json("GET", "/api/tasks")
    assert st == 401
    st, _, _ = browser.json("GET", "/api/workflows")
    assert st == 401
    st, _, _ = browser.json("GET", "/api/system")
    assert st == 401


def test_dashboard_after_login(browser):
    browser.login()
    st, body, _ = browser.json("GET", "/api/dashboard")
    assert st == 200 and "counts" in body


# -------------------------------------------------------------------- CSRF
def test_write_without_csrf_is_forbidden(browser):
    browser.login()
    st, body, _ = browser.json("POST", "/api/workflows", data={"goal": "x"})
    assert st == 403 and "csrf" in body["error"]


def test_write_with_csrf_succeeds(browser):
    browser.login()
    st, sess, _ = browser.json("GET", "/api/session")
    csrf = sess["csrf_token"]
    st, body, _ = browser.json(
        "POST", "/api/workflows", data={"goal": "csrf task"},
        headers={"X-CSRF-Token": csrf},
    )
    assert st == 200 and body["workflow_id"]


def test_wrong_csrf_is_forbidden(browser):
    browser.login()
    st, _, _ = browser.json(
        "POST", "/api/workflows", data={"goal": "x"},
        headers={"X-CSRF-Token": "not-the-token"},
    )
    assert st == 403


# ------------------------------------------------------------------ logout
def test_logout_revokes_session(browser):
    browser.login()
    st, body, _ = browser.json("GET", "/api/dashboard")
    assert st == 200
    st, body, _ = browser.json("POST", "/logout")
    assert st == 200
    # The same client, with its (now cleared) cookie, is unauthorized again.
    st, _, _ = browser.json("GET", "/api/dashboard")
    assert st == 401


def test_expired_session_is_rejected(server, browser):
    browser.login()
    # Force expiry directly in the store, then check the session is refused.
    import psycopg
    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE sessions SET expires_at = now() - interval '1 hour'")
    st, _, _ = browser.json("GET", "/api/dashboard")
    assert st == 401


def test_logout_clears_cookie(browser):
    browser.login()
    st, body, hdr = browser.json("POST", "/logout")
    assert "Max-Age=0" in hdr.get("Set-Cookie", "")


# ------------------------------------------------------------ rate limiting
def test_login_rate_limit_locks_out(server, monkeypatch):
    # A tiny threshold so the test is fast and deterministic.
    monkeypatch.setattr(server.app.auth, "max_failures", 3)
    b = Browser(f"http://127.0.0.1:{server.port}")
    for _ in range(3):
        st, _, _ = b.request("POST", "/login", form={"username": "owner",
                                                     "password": "bad"})
        assert st == 401
    # Now even the correct password is refused while the lock holds.
    st, _, _ = b.login(password=PASSWORD)
    assert st == 401
    st, body, _ = b.json("GET", "/api/dashboard")
    assert st == 401


# ------------------------------------------------- machine token independence
def test_bearer_token_still_works(server):
    """The machine token is independent of the browser session."""
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/dashboard")
    req.add_header("Authorization", "Bearer machine-token")
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 200


def test_bearer_token_can_write_without_csrf(server):
    """A machine client has no CSRF token; the bearer token is its auth."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.port}/api/workflows",
        data=json.dumps({"goal": "machine task"}).encode(), method="POST",
    )
    req.add_header("Authorization", "Bearer machine-token")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 200


def test_wrong_bearer_token_is_rejected(server):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/dashboard")
    req.add_header("Authorization", "Bearer wrong")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 401


# ------------------------------------------------------- no secret exposure
def test_api_never_returns_the_password_or_hash(browser, monkeypatch):
    monkeypatch.setenv("AUTH_SESSION_SECRET", "leak-me-not")
    browser.login()
    for path in ("/api/session", "/api/system", "/api/config", "/api/dashboard"):
        st, raw, _ = browser.request("GET", path)
        assert st == 200
        assert PASSWORD not in raw
        assert "leak-me-not" not in raw
        assert os.environ["AUTH_PASSWORD_HASH"] not in raw


def test_login_not_configured_returns_503(server, monkeypatch):
    """With no AUTH_* set, login fails closed (503), never 500."""
    monkeypatch.delenv("AUTH_USERNAME", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("AUTH_SESSION_SECRET", raising=False)
    # Rebuild the AuthService so it re-reads the (now empty) environment.
    server.app.auth = AuthService(server.app.store)
    b = Browser(f"http://127.0.0.1:{server.port}")
    st, raw, _ = b.request("POST", "/login", form={"username": "owner",
                                                   "password": PASSWORD})
    assert st == 503
    assert "не настроен" in raw.lower()
    # And a session-less read is still refused.
    st, _, _ = b.json("GET", "/api/dashboard")
    assert st == 401
