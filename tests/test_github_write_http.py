"""Exercise the GitHub REST adapter over sockets, including redirect refusal."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from orchestration.github_client import GitHubClient, GitHubError


class FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, data):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.server.calls.append(("GET", self.path, self.headers.get("Authorization"), None))
        if self.path == "/user":
            self.reply(200, {"login": "alice"})
        elif self.path == "/repos/alice/example":
            self.reply(200, {"id": 123, "full_name": "alice/example", "private": True,
                             "visibility": "private", "permissions": {"admin": True}})
        else:
            self.reply(404, {})

    def do_POST(self):
        self.write("POST")

    def do_PATCH(self):
        self.write("PATCH")

    def write(self, method):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append((method, self.path, self.headers.get("Authorization"), body))
        self.reply(201 if method == "POST" else 200,
                   {"full_name": "alice/" + body.get("name", "example"), "private": body["private"]})


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    srv.calls = []
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join()
    srv.server_close()


def test_write_adapter_uses_expected_requests(server):
    client = GitHubClient(f"http://127.0.0.1:{server.server_port}", token="secret")
    assert client.current_user() == {"login": "alice"}
    assert client.repository_details("alice/example")["admin"] is True
    assert client.create_repository("new", True) == {"full_name": "alice/new", "private": True}
    assert client.set_visibility("alice/example", False) == {"full_name": "alice/example", "private": False}
    assert server.calls[-2:] == [("POST", "/user/repos", "Bearer secret", {"name": "new", "private": True}),
                                ("PATCH", "/repos/alice/example", "Bearer secret", {"private": False})]


def test_github_token_is_not_forwarded_on_redirect(server):
    class Redirect(FakeHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{server.server_port}/user")
            self.end_headers()

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=redirect.serve_forever, daemon=True)
    thread.start()
    try:
        client = GitHubClient(f"http://127.0.0.1:{redirect.server_port}", token="secret")
        with pytest.raises(GitHubError):
            client.current_user()
        assert server.calls == []
    finally:
        redirect.shutdown()
        thread.join()
        redirect.server_close()
