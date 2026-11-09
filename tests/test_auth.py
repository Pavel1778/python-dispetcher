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
import time
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
    parse_hash,
    password_hash_problem,
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
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
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
    # Followed the redirect to the workspace, not the login form.
    assert "NEXUS" in raw


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
    # Now even the correct password is refused while the lock holds, and the
    # refusal says so instead of blaming the password.
    st, raw, _ = b.login(password=PASSWORD)
    assert st == 429
    assert "попыток" in raw.lower()
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


# ------------------------------------------- unusable AUTH_PASSWORD_HASH
# Regression: a non-empty but unusable AUTH_PASSWORD_HASH used to make every
# login fail with the generic "wrong login or password" 401, which pointed the
# operator at the credentials instead of at the misconfiguration.


def test_raw_password_in_the_variable_does_not_authenticate(server, monkeypatch):
    """Pasting the password itself must not log in, and must not be reported as
    a wrong password either: it is a configuration error."""
    monkeypatch.setenv("AUTH_PASSWORD_HASH", PASSWORD)
    server.app.auth = AuthService(server.app.store)
    b = Browser(f"http://127.0.0.1:{server.port}")

    st, raw, _ = b.request("POST", "/login", form={"username": "owner",
                                                   "password": PASSWORD})
    # Not a session (never authenticate on a malformed hash), and not a 401
    # that blames the credentials.
    assert st == 503
    assert "AUTH_PASSWORD_HASH" in raw
    st, _, _ = b.json("GET", "/api/dashboard")
    assert st == 401


def test_unusable_hash_is_reported_not_silently_wrong(server, monkeypatch):
    """The login page names the real problem instead of 'wrong credentials'."""
    monkeypatch.setenv("AUTH_PASSWORD_HASH", "not-a-hash")
    server.app.auth = AuthService(server.app.store)
    b = Browser(f"http://127.0.0.1:{server.port}")
    st, raw, _ = b.request("POST", "/login", form={"username": "owner",
                                                   "password": PASSWORD})
    assert st == 503
    assert "Неверный логин или пароль" not in raw
    assert "AUTH_PASSWORD_HASH" in raw


def test_foreign_hash_algorithm_is_rejected(server, monkeypatch):
    """A bcrypt-style value from another tool cannot authenticate."""
    monkeypatch.setenv(
        "AUTH_PASSWORD_HASH", "$2b$12$abcdefghijklmnopqrstuvabcdefghijklmnopqrstuv"
    )
    server.app.auth = AuthService(server.app.store)
    b = Browser(f"http://127.0.0.1:{server.port}")
    st, _, _ = b.request("POST", "/login", form={"username": "owner",
                                                 "password": PASSWORD})
    assert st == 503


def test_configured_requires_a_usable_hash():
    """`configured` is about whether login can succeed, not about non-empty."""
    svc = AuthService(
        store=None,
        username="owner",
        password_hash="hunter2",
        session_secret="s",
    )
    assert svc.configured is False
    assert "AUTH_PASSWORD_HASH" in svc.missing_settings()
    assert any("AUTH_PASSWORD_HASH" in e for e in svc.config_errors())

    svc.password_hash = hash_password(PASSWORD, iterations=1000)
    assert svc.configured is True
    assert svc.config_errors() == []


def test_config_errors_never_contain_the_value():
    """Diagnostics name the variable and the problem, never the secret."""
    secret_value = "pbkdf2_sha256$1000$not-base64-at-all$also-not-base64"
    svc = AuthService(
        store=None, username="owner", password_hash=secret_value,
        session_secret="session-secret-value",
    )
    text = " ".join(svc.config_errors()) + " ".join(svc.missing_settings())
    assert secret_value not in text
    assert "session-secret-value" not in text


def test_verify_password_never_raises():
    """A malformed stored value returns False; it must not raise. A zero or
    negative iteration count used to escape as ValueError and become a 500."""
    for bad in (
        "",
        "not-a-hash",
        "pbkdf2_sha256$0$c2FsdA==$aGFzaA==",
        "pbkdf2_sha256$-1$c2FsdA==$aGFzaA==",
        "pbkdf2_sha256$99999999999999$c2FsdA==$aGFzaA==",
        "pbkdf2_sha256$abc$x$y",
        "pbkdf2_sha256$1000$!!!$!!!",
        "a$b$c$d$e",
        "pbkdf2_sha256$1000$$",
        None,
    ):
        assert verify_password("whatever", bad) is False


def test_malformed_hash_on_a_live_server_is_503_not_500(server, monkeypatch):
    """The login endpoint must not 500 on a corrupt hash (the ValueError path)."""
    monkeypatch.setenv("AUTH_PASSWORD_HASH", "pbkdf2_sha256$0$c2FsdA==$aGFzaA==")
    server.app.auth = AuthService(server.app.store)
    b = Browser(f"http://127.0.0.1:{server.port}")
    st, raw, _ = b.request("POST", "/login", form={"username": "owner",
                                                   "password": PASSWORD})
    assert st in (401, 503)
    assert st != 500


def test_whitespace_in_configured_username_is_tolerated(server, monkeypatch):
    """A stray space in the dashboard value must not block a correct login."""
    monkeypatch.setenv("AUTH_USERNAME", "  owner  ")
    server.app.auth = AuthService(server.app.store)
    b = Browser(f"http://127.0.0.1:{server.port}")
    st, _, _ = b.login()
    assert st == 200
    assert b.cookie() is not None


def test_whitespace_around_hash_is_tolerated(server, monkeypatch):
    """A trailing newline from a copy/paste must not block a correct login."""
    monkeypatch.setenv("AUTH_PASSWORD_HASH", "\n" + os.environ["AUTH_PASSWORD_HASH"] + "\n")
    server.app.auth = AuthService(server.app.store)
    b = Browser(f"http://127.0.0.1:{server.port}")
    st, _, _ = b.login()
    assert st == 200


def test_system_reports_login_errors_without_the_hash(server, monkeypatch):
    """`/api/system` exposes the reasons, so a bad hash is visible remotely."""
    bad = "pbkdf2_sha256$0$c2FsdA==$aGFzaA=="
    monkeypatch.setenv("AUTH_PASSWORD_HASH", bad)
    server.app.auth = AuthService(server.app.store)
    b = Browser(f"http://127.0.0.1:{server.port}")
    b.login(password=PASSWORD)  # establish a session is impossible; use bearer
    st, body, _ = b.json("GET", "/api/system")
    assert st == 401  # no session was created, which is correct
    # The startup gate is what surfaces the reason in a real deployment; assert
    # the service itself reports it truthfully.
    assert server.app.auth.configured is False
    assert any("AUTH_PASSWORD_HASH" in e for e in server.app.auth.config_errors())
    assert bad not in json.dumps(server.app.auth.config_errors())


def test_lockout_is_reported_as_lockout_not_wrong_password(server):
    """After the failure threshold, the correct password must not be blamed.

    Regression: the lockout returned None, so a locked-out owner saw the same
    "wrong login or password" 401 as a bad password and kept retyping it.
    """
    b = Browser(f"http://127.0.0.1:{server.port}")
    for _ in range(server.app.auth.max_failures):
        b.request("POST", "/login", form={"username": "owner", "password": "WRONG"})

    st, raw, _ = b.login()  # the CORRECT password
    assert st == 429
    assert "Неверный логин или пароль" not in raw
    assert "попыток" in raw.lower()


def test_startup_gate_rejects_an_unusable_hash():
    """The bind guard must refuse a public interface on a bad hash, naming it."""
    import subprocess

    env = dict(os.environ)
    env.update({
        "SUPABASE_DB_URL": DSN,
        "WEB_AUTH_TOKEN": "machine-token",
        "AUTH_USERNAME": "owner",
        "AUTH_PASSWORD_HASH": PASSWORD,          # the raw password
        "AUTH_SESSION_SECRET": "s",
    })
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run(
        [sys.executable, "-m", "orchestration.web_server",
         "--host", "0.0.0.0", "--port", "0"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 2
    assert "AUTH_PASSWORD_HASH" in proc.stdout + proc.stderr


# ------------------------------------- generator and validator must agree
# Regression: the deployment rejected a hash the shipped generator produced.
# The generator and the validator have to round-trip through the real CLI and
# the real login endpoint, not just through the helper functions.


def test_cli_hash_output_authenticates_over_http(server, monkeypatch, tmp_path):
    """A hash from `python -m orchestration.web_auth hash` must log in.

    Runs the real CLI under a pty (it reads the password from a prompt), takes
    the hash it prints, and uses it to authenticate against the real server.
    """
    import pty
    import re

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    password = "generator-and-validator-must-agree"

    pid, fd = pty.fork()
    if pid == 0:  # child: run the CLI as the operator would
        os.chdir(repo)
        os.execv(sys.executable, [sys.executable, "-m", "orchestration.web_auth",
                                  "hash"])
    time.sleep(0.5)
    for _ in range(2):
        os.write(fd, password.encode() + b"\n")
        time.sleep(0.5)
    time.sleep(1.5)
    raw = b""
    try:
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            raw += chunk
    except OSError:
        pass
    os.waitpid(pid, 0)

    text = raw.decode(errors="replace")
    match = re.search(r"pbkdf2_sha256\$[0-9]+\$[^\s']+", text)
    assert match, f"the CLI printed no hash: {text!r}"
    printed = match.group(0)

    # The printed value must be exactly what the server accepts.
    assert parse_hash(printed) is not None
    assert password_hash_problem(printed) is None
    assert verify_password(password, printed) is True

    monkeypatch.setenv("AUTH_PASSWORD_HASH", printed)
    server.app.auth = AuthService(server.app.store)
    b = Browser(f"http://127.0.0.1:{server.port}")
    st, _, _ = b.login(password=password)
    assert st == 200, "a hash from the shipped generator failed to log in"
    assert b.cookie() is not None


def test_cli_check_accepts_the_generated_hash(monkeypatch):
    """`check --stdin` exits 0 for a hash the generator produced."""
    import subprocess

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    good = hash_password(PASSWORD, iterations=1000)
    monkeypatch.delenv("AUTH_PASSWORD_HASH", raising=False)
    proc = subprocess.run(
        [sys.executable, "-m", "orchestration.web_auth", "check", "--stdin"],
        cwd=repo, input=good, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
    assert good not in proc.stdout  # the value is never echoed


def test_cli_check_rejects_a_corrupt_hash_without_echoing_it(monkeypatch):
    """`check` exits 1 and names the problem, but never prints the value."""
    import subprocess

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bad = "pbkdf2_sha256$1000$AAAA$BBB"        # digest is not padded base64
    monkeypatch.delenv("AUTH_PASSWORD_HASH", raising=False)
    proc = subprocess.run(
        [sys.executable, "-m", "orchestration.web_auth", "check", "--stdin"],
        cwd=repo, input=bad, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 1
    assert "INVALID" in proc.stderr
    assert bad not in proc.stdout + proc.stderr


def test_cli_check_prefers_the_environment_over_stdin(monkeypatch):
    """`check` with no argument verifies AUTH_PASSWORD_HASH; it must not block on
    stdin, so the environment wins and the reported source says so."""
    import subprocess

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    good = hash_password(PASSWORD, iterations=1000)
    monkeypatch.setenv("AUTH_PASSWORD_HASH", good)
    proc = subprocess.run(
        [sys.executable, "-m", "orchestration.web_auth", "check"],
        cwd=repo, input="junk-on-stdin", capture_output=True, text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "AUTH_PASSWORD_HASH" in proc.stdout


def test_cli_check_stdin_ignores_an_exported_variable(monkeypatch):
    """`check --stdin` must verify the piped value, not an exported one.

    Regression: with AUTH_PASSWORD_HASH already set, a piped new hash was
    silently ignored, so `check` reported OK for the *old* value and the operator
    deployed the wrong one.
    """
    import subprocess

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    monkeypatch.setenv("AUTH_PASSWORD_HASH", "not-a-hash")
    good = hash_password(PASSWORD, iterations=1000)
    proc = subprocess.run(
        [sys.executable, "-m", "orchestration.web_auth", "check", "--stdin"],
        cwd=repo, input=good, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "stdin" in proc.stdout


def test_cli_usage_names_every_subcommand():
    """The blueprint once documented a command with no subcommand; make the
    usage text list them so a bare invocation is self-correcting."""
    import subprocess

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run(
        [sys.executable, "-m", "orchestration.web_auth"],
        cwd=repo, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 2
    for sub in ("hash", "secret", "check"):
        assert sub in proc.stderr


# ------------------------------------ whitespace and transport corruption
# Regression: a terminal wraps the long hash, and the newline in the middle made
# the value "not valid base64", which the operator could not act on.


@pytest.mark.parametrize("wrap", [
    lambda h: h + "\n",                       # trailing newline (.env, paste)
    lambda h: "\n" + h,                       # leading newline
    lambda h: h + "\r\n",                     # Windows clipboard
    lambda h: h[:40] + "\n" + h[40:],         # terminal wrap, mid-value
    lambda h: "  " + h + "  ",                # stray spaces
    lambda h: h[:30] + "\t" + h[30:],         # tab
])
def test_whitespace_from_a_wrapped_paste_still_logs_in(server, monkeypatch, wrap):
    monkeypatch.setenv("AUTH_PASSWORD_HASH", wrap(hash_password(PASSWORD,
                                                                iterations=1000)))
    server.app.auth = AuthService(server.app.store)
    b = Browser(f"http://127.0.0.1:{server.port}")
    st, _, _ = b.login()
    assert st == 200, "whitespace introduced by a wrapped paste blocked the login"


def test_whitespace_is_ignored_but_the_password_is_not_normalised():
    """Only the stored value is normalised; the password must still be exact."""
    stored = hash_password(PASSWORD, iterations=1000)
    assert verify_password(PASSWORD, " " + stored + " ") is True
    assert verify_password(PASSWORD + " ", stored) is False
    assert verify_password(" " + PASSWORD, stored) is False


@pytest.mark.parametrize("label,corrupt,expect", [
    ("shell ate the $ separators",
     lambda h: h.replace("$", ""),
     "lost its '$' separators"),
    ("quotes included",
     lambda h: '"' + h + '"',
     "wrapped in quotes"),
    ("urlsafe base64 alphabet",
     lambda h: h.replace("+", "-").replace("/", "_"),
     "URL-safe base64"),
    ("padding stripped",
     lambda h: h.rstrip("="),
     "padding"),
    ("raw password",
     lambda h: PASSWORD,
     "raw password"),
])
def test_each_corruption_is_named_specifically(label, corrupt, expect):
    """Each realistic corruption gets its own message, so the operator knows
    what to fix instead of being told to regenerate a good hash."""
    # A fixed hash that contains both '+' and '/', so the URL-safe corruption
    # actually changes the alphabet instead of being a no-op.
    stored = ("pbkdf2_sha256$1000$+a/bC1dEf2gH3iJ4kL5mN6oQ=="
              "$Zm9v+GJhci9iYXo=")
    corrupted = corrupt(stored)
    problem = password_hash_problem(corrupted)
    assert problem is not None, label
    assert expect in problem, f"{label}: got {problem!r}"


def test_diagnostics_never_contain_the_value():
    """Every message is safe to log and to show on the login page."""
    stored = hash_password(PASSWORD, iterations=1000)
    variants = [stored, stored.replace("$", ""), '"' + stored + '"',
                stored.rstrip("="), PASSWORD, "junk"]
    for value in variants:
        problem = password_hash_problem(value)
        if problem:
            assert value not in problem
            assert stored not in problem


# ------------------------------------------------ client IP behind a proxy
def test_spoofed_xff_cannot_rotate_the_rate_limit_key(server):
    """An appending proxy must not let the client pick its own rate-limit key.

    Regression: the leftmost ``X-Forwarded-For`` value was used. A proxy only
    appends, so the client controls the leftmost entry; rotating it made every
    attempt look like a new client and the lockout never engaged. The test
    client plays the proxy: it appends the real peer IP to the attacker's value.
    """
    real_client = "127.0.0.1"
    b = Browser(f"http://127.0.0.1:{server.port}")
    codes = []
    for i in range(12):
        st, _, _ = b.request(
            "POST", "/login",
            form={"username": "owner", "password": "WRONG"},
            headers={"X-Forwarded-For": f"203.0.113.{i}, {real_client}"},
        )
        codes.append(st)
    assert 429 in codes, f"lockout never engaged: {codes}"

    # The same real client cannot get in with the correct password either.
    st, _, _ = b.request(
        "POST", "/login",
        form={"username": "owner", "password": PASSWORD},
        headers={"X-Forwarded-For": f"198.51.100.7, {real_client}"},
    )
    assert st == 429, "spoofed header bypassed the lockout"


def test_client_ip_uses_the_rightmost_trusted_hop():
    """The resolver picks the proxy-appended value, not the client-supplied one."""
    from orchestration.web_server import client_ip_from

    assert client_ip_from("203.0.113.9, 198.51.100.4", "10.0.0.1", 1) == "198.51.100.4"
    # No header: the socket peer.
    assert client_ip_from(None, "10.0.0.1", 1) == "10.0.0.1"
    # Zero trusted hops: the header is ignored entirely.
    assert client_ip_from("203.0.113.9", "10.0.0.1", 0) == "10.0.0.1"
    # Fewer addresses than trusted hops: cannot be trusted, so use the peer.
    assert client_ip_from("203.0.113.9", "10.0.0.1", 2) == "10.0.0.1"
    # Two proxies: the second from the right is the client the first proxy saw.
    assert client_ip_from("a, b, c", "10.0.0.1", 2) == "b"


def test_client_ip_joins_every_header_line():
    """A repeated header must not let the client pick the key.

    Regression: ``headers.get`` returns only the first ``X-Forwarded-For`` line.
    A caller that sends its own line and lets the proxy append a second one made
    the resolver read the caller's value, so rotating it defeated the lockout.
    """
    from orchestration.web_server import client_ip_from_values

    # Two lines: the caller's, then what the proxy appended. hops=1 must pick
    # the proxy's value from the second line.
    assert client_ip_from_values(["10.0.0.5", "203.0.113.7"], "127.0.0.1", 1) == "203.0.113.7"
    # Three lines, two trusted hops: the second from the right.
    assert client_ip_from_values(["9.9.9.9", "1.1.1.1", "2.2.2.2"], "127.0.0.1", 2) == "1.1.1.1"
    # A comma list inside one line behaves the same as several lines.
    assert client_ip_from_values(["10.0.0.5, 203.0.113.7"], "127.0.0.1", 1) == "203.0.113.7"
    # No header lines at all: the socket peer.
    assert client_ip_from_values([], "127.0.0.1", 1) == "127.0.0.1"
    assert client_ip_from_values(None, "127.0.0.1", 1) == "127.0.0.1"


def test_trusted_hop_count_defaults_and_rejects_junk(monkeypatch):
    """The hop count must never fall back to a value that trusts more proxies.

    A missing, empty or unparsable value falls back to the safe default of 1
    (trust one hop), not to 0 and not to "trust everything". A negative value is
    clamped to 0 (ignore the header). No input may raise.
    """
    from orchestration.web_server import trusted_hop_count

    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    assert trusted_hop_count() == 1, "unset must default to 1"

    for junk in ("", "   ", "abc", "1.5", "one"):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", junk)
        assert trusted_hop_count() == 1, f"{junk!r} must fall back to 1"

    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "3")
    assert trusted_hop_count() == 3
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", " 3 ")
    assert trusted_hop_count() == 3, "surrounding whitespace must be tolerated"
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "0")
    assert trusted_hop_count() == 0
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "-5")
    assert trusted_hop_count() == 0, "a negative hop count must clamp to 0"


def test_absurd_hop_count_falls_back_to_the_socket_peer():
    """A hop count larger than the header length must use the peer, not a header.

    This is the misconfiguration case: if the count is too high the resolver
    cannot find a trustworthy entry, so it must fall back to the socket peer
    rather than index into the (attacker-controlled) list.
    """
    from orchestration.web_server import client_ip_from_values

    assert client_ip_from_values(["203.0.113.9"], "10.0.0.1", 999) == "10.0.0.1"


def test_empty_and_garbage_entries_are_skipped_not_trusted():
    """Blank entries must not shift the index onto a caller-supplied value.

    A caller can pad the header with empty fields (``X-Forwarded-For: , ,``).
    If those counted as addresses, the ``len < hops`` guard would pass and the
    index would land on an entry the caller chose. Only non-empty entries count.
    """
    from orchestration.web_server import client_ip_from_values

    # All-empty header: nothing to trust, so the socket peer.
    assert client_ip_from_values([", , ,"], "10.0.0.1", 1) == "10.0.0.1"
    assert client_ip_from_values(["", "  ", ","], "10.0.0.1", 1) == "10.0.0.1"
    # Empty padding before the real, proxy-appended value must be ignored.
    assert client_ip_from_values([" , , 203.0.113.9"], "10.0.0.1", 1) == "203.0.113.9"
    assert client_ip_from_values([", 203.0.113.9"], "10.0.0.1", 1) == "203.0.113.9"


def test_over_configured_hop_count_can_return_a_caller_supplied_value():
    """Pin the direction of the risk: a hop count above the real chain is unsafe.

    With more hops configured than the chain actually has, the resolver walks
    left past the proxy-appended entries and can land on one the caller
    prepended. This is why the default is 1 and why it must never be raised
    above the number of proxies actually operated. It documents the hazard; it
    is not a behaviour to rely on.
    """
    from orchestration.web_server import client_ip_from_values

    # Real chain is one hop (the platform proxy appended 203.0.113.9); the
    # caller prepended 1.1.1.1. hops=1 is correct and ignores the caller.
    assert client_ip_from_values(["1.1.1.1", "203.0.113.9"], "10.0.0.1", 1) == "203.0.113.9"
    # hops=2 over-reaches and returns the caller's own value.
    assert client_ip_from_values(["1.1.1.1", "203.0.113.9"], "10.0.0.1", 2) == "1.1.1.1"


def test_repeated_xff_header_cannot_bypass_the_lockout(server):
    """The live bypass, reproduced end to end over a raw socket.

    ``urllib`` collapses duplicate headers, so a raw socket is the only way to
    send two ``X-Forwarded-For`` lines. Both the honest client and the
    duplicate-header client are the same real peer; both must lock.
    """
    import socket

    def attempt(lines: list[str]) -> int:
        body = "username=owner&password=WRONG"
        raw = "\r\n".join([
            "POST /login HTTP/1.1",
            f"Host: 127.0.0.1:{server.port}",
            "Content-Type: application/x-www-form-urlencoded",
            f"Content-Length: {len(body)}",
            *lines,
            "Connection: close",
            "",
            body,
        ])
        s = socket.create_connection(("127.0.0.1", server.port), timeout=10)
        s.sendall(raw.encode())
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        return int(data.split(b"\r\n", 1)[0].split()[1])

    limit = server.app.login_throttle.max_attempts

    honest = [attempt(["X-Forwarded-For: 198.51.100.50"]) for _ in range(limit + 3)]
    assert 429 in honest, f"honest client never locked: {honest}"

    # Same real client, but it repeats the header to try to own the key.
    spoofed = [
        attempt([f"X-Forwarded-For: 10.0.0.{i}", "X-Forwarded-For: 203.0.113.7"])
        for i in range(limit + 7)
    ]
    assert 429 in spoofed, f"duplicate header bypassed the lockout: {spoofed}"


def test_per_ip_throttle_caps_attempts_across_usernames(server):
    """A caller rotating the username must still hit a per-IP ceiling.

    The durable lockout is keyed by (username, IP), so varying the username
    presents a fresh key each time. The in-memory per-IP throttle is the
    backstop that does not depend on what the caller claims.
    """
    b = Browser(f"http://127.0.0.1:{server.port}")
    limit = server.app.login_throttle.max_attempts
    codes = []
    for i in range(limit + 5):
        st, _, _ = b.request("POST", "/login",
                             form={"username": f"user{i}", "password": "WRONG"})
        codes.append(st)
    assert 429 in codes, f"per-IP throttle never engaged: {codes}"


def test_expired_sessions_and_stale_attempts_are_swept(server):
    """Housekeeping actually deletes rows that no longer constrain anything.

    Regression: ``purge_expired_sessions`` existed but was never called, and
    nothing removed a rate-limit row after its window closed, so both tables
    grew forever.
    """
    store = server.app.store
    store.create_session(token_hash="dead", username="owner",
                         csrf_token="c", ttl_seconds=-1)   # already expired
    store.record_login_failure("stale-key", max_failures=5, lockout_seconds=0)

    assert store.purge_expired_sessions() >= 1
    # window_start is now; keep_seconds=0 makes the row stale immediately.
    assert store.purge_stale_login_attempts(keep_seconds=0) >= 1
    assert store.get_session("dead") is None


def test_sweep_runs_from_the_loop(server):
    """The background loop must actually invoke the sweep, not just define it."""
    app = server.app
    store = app.store
    store.create_session(token_hash="loopdead", username="owner",
                         csrf_token="c", ttl_seconds=-1)
    app._sweep_every = 1          # sweep on the next tick
    app._maybe_sweep()
    assert store.get_session("loopdead") is None


def test_global_ceiling_holds_when_the_ip_is_untrustworthy(server):
    """The endpoint-wide ceiling must not depend on the client IP at all.

    A platform that overwrites rather than appends ``X-Forwarded-For`` makes the
    resolved IP attacker-controlled, so the per-IP ceiling cannot be relied on.
    The global ceiling is the backstop that no header can move.

    The username is varied too: the durable lockout is keyed by (username, IP),
    so a fixed username would let the database lockout mask this ceiling and the
    test would pass for the wrong reason.
    """
    b = Browser(f"http://127.0.0.1:{server.port}")
    throttle = server.app.login_throttle
    throttle.max_attempts = 10_000          # disable the per-IP ceiling
    codes = []
    for i in range(throttle.global_max_attempts + 5):
        st, _, _ = b.request("POST", "/login",
                             form={"username": f"u{i}", "password": "WRONG"},
                             headers={"X-Forwarded-For": f"203.0.113.{i % 250}"})
        codes.append(st)
    assert 429 in codes, "the global ceiling never engaged"
