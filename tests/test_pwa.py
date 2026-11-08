"""PWA assets and auth boundaries without a PostgreSQL server."""

import http.client
import json
import shutil
import struct
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestration.web_server import Handler


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.app = SimpleNamespace(auth=SimpleNamespace(resolve=lambda _: None), token="test-token")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    thread.join()
    httpd.server_close()


def get(server, path, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    conn.request("GET", path, headers=headers)
    response = conn.getresponse()
    result = (response.status, dict(response.getheaders()), response.read())
    conn.close()
    return result


def test_manifest_and_icons_public(server):
    status, headers, body = get(server, "/manifest.webmanifest")
    assert status == 200
    assert headers["Content-Type"] == "application/manifest+json"
    assert headers["Cache-Control"] == "no-cache"
    manifest = json.loads(body)
    assert manifest["start_url"] == manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    for icon in manifest["icons"]:
        status, headers, image = get(server, icon["src"])
        assert status == 200 and headers["Content-Type"] == "image/png"
        assert image[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", image[16:24])
        assert icon["sizes"] == f"{width}x{height}"


def test_login_dashboard_and_auth_remain_separate(server):
    status, headers, body = get(server, "/login")
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert b'/manifest.webmanifest' in body and b'/static/pwa/register.js' in body
    assert get(server, "/")[0] == 303
    status, headers, body = get(server, "/", "test-token")
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert b'/manifest.webmanifest' in body and b'/static/pwa/register.js' in body
    assert get(server, "/api/session")[0] == 401
    assert get(server, "/static/pwa/../../web_server.py")[0] == 401


def test_worker_is_public_and_not_cached(server):
    for path in ("/service-worker.js", "/static/pwa/register.js"):
        status, headers, body = get(server, path)
        assert status == 200 and body
        assert headers["Content-Type"].startswith("application/javascript")
    assert get(server, "/service-worker.js")[1]["Cache-Control"] == "no-cache"


def test_worker_network_and_offline_behavior():
    if not shutil.which("node"):
        pytest.skip("Node is needed to exercise service worker JavaScript")
    subprocess.run(
        ["node", str(Path(__file__).with_name("pwa_worker_test.js"))],
        check=True,
        timeout=10,
    )
