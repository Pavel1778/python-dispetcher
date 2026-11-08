"""No writes without a bound, one-use human confirmation."""
import os

import pytest

from orchestration.github_confirmations import Confirmations
from tests.test_nexus_projects import request

pytest_plugins = ["tests.test_nexus_projects"]


@pytest.fixture(autouse=True)
def writes_disabled_by_default(monkeypatch):
    monkeypatch.delenv("GITHUB_ENABLE_WRITES", raising=False)


class FakeGitHub:
    configured = True

    def __init__(self):
        self.calls = []
        self.actor = "alice"
        self.repo = {"id": 123, "full_name": "alice/example", "private": True,
                     "visibility": "private", "admin": True}

    def current_user(self):
        return {"login": self.actor}

    def repository_details(self, name):
        return dict(self.repo)

    def create_repository(self, name, private):
        self.calls.append(("create", name, private))
        return {"full_name": self.actor + "/" + name, "private": private}

    def set_visibility(self, name, private):
        self.calls.append(("visibility", name, private))
        return {"full_name": name, "private": private}


def test_confirmation_is_session_bound_one_use_and_validates():
    g, c = FakeGitHub(), Confirmations()
    preview = c.prepare(g, "session-a", {"action": "create", "name": "my-repo", "private": True})
    assert g.calls == []
    with pytest.raises(ValueError):
        c.confirm(g, "session-b", preview["confirmation"])
    assert c.confirm(g, "session-a", preview["confirmation"])["full_name"] == "alice/my-repo"
    with pytest.raises(ValueError):
        c.confirm(g, "session-a", preview["confirmation"])
    assert g.calls == [("create", "my-repo", True)]
    for body in ({"action": "create", "name": "../x", "private": False},
                 {"action": "create", "name": "x", "private": "false"},
                 {"action": "create", "name": "x", "private": True, "extra": 1}):
        with pytest.raises(ValueError):
            c.prepare(g, "session-a", body)


def test_visibility_requires_admin_and_rechecks_state():
    g, c = FakeGitHub(), Confirmations()
    body = {"action": "visibility", "name": "alice/example", "private": False}
    g.repo["admin"] = False
    with pytest.raises(ValueError):
        c.prepare(g, "a", body)
    g.repo["admin"] = True
    preview = c.prepare(g, "a", body)
    g.repo["id"] = 456
    with pytest.raises(ValueError):
        c.confirm(g, "a", preview["confirmation"])
    with pytest.raises(ValueError):
        c.confirm(g, "a", preview["confirmation"])
    assert not g.calls
    preview = c.prepare(g, "a", body)
    c.confirm(g, "a", preview["confirmation"])
    assert g.calls == [("visibility", "alice/example", False)]


def test_expired_and_changed_identity_do_not_write():
    g, c = FakeGitHub(), Confirmations(ttl=0)
    p = c.prepare(g, "a", {"action": "create", "name": "a", "private": True})
    with pytest.raises(ValueError):
        c.confirm(g, "a", p["confirmation"])
    c = Confirmations()
    p = c.prepare(g, "a", {"action": "create", "name": "a", "private": True})
    g.actor = "bob"
    with pytest.raises(ValueError):
        c.confirm(g, "a", p["confirmation"])
    assert not g.calls


@pytest.mark.skipif(not os.environ.get("TEST_SUPABASE_DB_URL"), reason="requires disposable PostgreSQL")
def test_http_requires_session_csrf_and_explicit_enable(http_server):
    base, alice, bob = http_server
    # The fixture runs a real server. Give it a fake GitHub client via its App
    # in a separate lightweight HTTP test below; this one tests fail-closed gates.
    payload = {"action": "create", "name": "x", "private": True}
    assert request(base, "/api/github/prepare", "POST", payload, token="machine-token")[0] == 403
    assert request(base, "/api/github/prepare", "POST", payload, session=alice, csrf=False)[0] == 403
    assert request(base, "/api/github/prepare", "POST", payload, session=alice)[0] == 403
    assert request(base, "/api/github/confirm", "POST", {"confirmation": "x"}, session=bob)[0] == 403


def test_http_confirm_flow_with_fake_github():
    import http.client
    import json
    import threading
    from http.server import ThreadingHTTPServer
    from types import SimpleNamespace

    from orchestration.web_auth import Session
    from orchestration.web_server import Handler

    github = FakeGitHub()
    sessions = {x: Session(token=x, csrf_token="csrf-" + x, username=x) for x in ("alice", "bob")}
    auth = SimpleNamespace(resolve=lambda token: sessions.get(token),
                           csrf_ok=lambda session, csrf: csrf == session.csrf_token)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.app = SimpleNamespace(auth=auth, token="machine", github=github,
                                github_confirmations=Confirmations(), github_writes_enabled=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def post(path, body, session=None, csrf=None, bearer=False):
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if session:
            headers["Cookie"] = "dispatcher_session=" + session
        if csrf:
            headers["X-CSRF-Token"] = csrf
        if bearer:
            headers["Authorization"] = "Bearer machine"
        conn.request("POST", path, json.dumps(body), headers)
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    try:
        body = {"action": "create", "name": "demo", "private": True}
        assert post("/api/github/prepare", body, bearer=True)[0] == 403
        assert post("/api/github/prepare", body, session="alice", bearer=True)[0] == 403
        status, preview = post("/api/github/prepare", body, "alice", "csrf-alice")
        assert status == 200 and github.calls == []
        confirmation = {"confirmation": preview["confirmation"]}
        assert post("/api/github/confirm", confirmation, "bob", "csrf-bob")[0] == 400
        assert post("/api/github/confirm", confirmation, "alice", "csrf-alice")[0] == 200
        assert post("/api/github/confirm", confirmation, "alice", "csrf-alice")[0] == 400
        assert github.calls == [("create", "demo", True)]
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()


def test_confirmation_is_consumed_before_ambiguous_network_failure():
    from orchestration.github_client import GitHubUnavailable

    class TimeoutGitHub(FakeGitHub):
        def create_repository(self, name, private):
            self.calls.append(("create", name, private))
            raise GitHubUnavailable("timeout")

    g, c = TimeoutGitHub(), Confirmations()
    preview = c.prepare(g, "session", {"action": "create", "name": "demo", "private": False})
    with pytest.raises(GitHubUnavailable):
        c.confirm(g, "session", preview["confirmation"])
    with pytest.raises(ValueError):
        c.confirm(g, "session", preview["confirmation"])
    assert g.calls == [("create", "demo", False)]
