"""Direct Cloud transport tests.

These run against a real PostgreSQL database. The account-level sandbox calls
are answered by a small fake (no sockets, no LLM); the *HTTP* to each sandbox's
agent-server is answered by an in-memory fake injected as ``client._send``. What
is under test is the routing and persistence logic -- which sandbox a
conversation lands on, and whether a later client can still find it -- not the
HTTP layer, which the live end-to-end run covers.

The database is taken from ``TEST_SUPABASE_DB_URL`` (falling back to
``SUPABASE_DB_URL``); the module is skipped when neither is set.
"""

from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient, CloudNotFound  # noqa: E402
from orchestration.cloud_direct import (  # noqa: E402
    CloudDirectClient,
    build_cloud_client,
)
from orchestration.cloud_store import PostgresStore  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run direct transport tests"
)

MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"
)


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DROP TABLE IF EXISTS sr_tool_calls, task_events, conversations, "
                "conversation_routes, cloud_sandboxes, tasks, repositories, "
                "workflows CASCADE"
            )
            for name in sorted(os.listdir(MIGRATIONS_DIR)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(MIGRATIONS_DIR, name)).read())


@pytest.fixture(autouse=True)


def _schema():
    _reset_schema()
    yield


class FakeCloudAccount:
    """Account-level Cloud calls: ``start_sandbox`` / ``wait_for_sandbox`` only."""

    def __init__(self):
        self.sandboxes: dict[str, dict] = {}
        self.start_count = 0

    def start_sandbox(self, *, sandbox_spec_id=None):
        self.start_count += 1
        sid = f"sandbox-{self.start_count}"
        self.sandboxes[sid] = {
            "id": sid,
            "status": "STARTING",
            "session_api_key": f"key-{sid}",
            "exposed_urls": None,
        }
        return dict(self.sandboxes[sid])

    def wait_for_sandbox(self, sandbox_id, *, attempts=40, interval=5.0):
        info = self.sandboxes[sandbox_id]
        info["status"] = "RUNNING"
        info["exposed_urls"] = [
            {"name": "AGENT_SERVER", "url": f"http://{sandbox_id}.test:60000",
             "port": 60000}
        ]
        return dict(info)

    @staticmethod
    def agent_server_url(info):
        for entry in info.get("exposed_urls") or []:
            if entry.get("name") == "AGENT_SERVER":
                return entry["url"]
        return None

    def get_sandbox(self, sandbox_id):
        if sandbox_id not in self.sandboxes:
            raise CloudNotFound(sandbox_id)
        return dict(self.sandboxes[sandbox_id])

    def delete_sandbox(self, sandbox_id):
        self.sandboxes.pop(sandbox_id, None)
        return True


class FakeAgentServer:
    """In-memory stand-in for sandbox agent-servers, isolated per sandbox URL."""

    def __init__(self):
        # sandbox url -> {conversation id -> payload}
        self.by_sandbox: dict[str, dict[str, dict]] = {}
        self.requests: list[tuple[str, str, str]] = []

    def _box(self, url: str) -> dict[str, dict]:
        return self.by_sandbox.setdefault(url, {})

    def __call__(self, route, method, path, body=None):
        self.requests.append((route.agent_server_url, method, path))
        box = self._box(route.agent_server_url)
        if path == "/api/settings":
            return 200, {"agent_settings": {"llm": {"model": "fake-model"}}}
        if path == "/api/tools/catalog":
            return 200, {"tools": [
                {"name": "terminal", "user_selectable": True, "in_default_set": True,
                 "usable": True},
                {"name": "file_editor", "user_selectable": True, "in_default_set": True,
                 "usable": True},
            ]}
        if path.startswith("/api/conversations/search"):
            return 200, {"items": list(box.values())}
        if method == "POST" and path == "/api/conversations":
            cid = body["conversation_id"]
            box[cid] = {
                "id": cid,
                "execution_status": "running",
                "tags": dict(body.get("tags") or {}),
                "client_tools": list(body.get("client_tools") or []),
            }
            return 201, {"id": cid}
        match = re.fullmatch(r"/api/conversations/([^/?]+)", path)
        if match:
            cid = match.group(1)
            if method == "GET":
                if cid not in box:
                    return 404, {"detail": "gone"}
                return 200, box[cid]
            if method == "DELETE":
                box.pop(cid, None)
                return 200, {}
        raise AssertionError(f"unexpected {method} {path}")


def _client(store, account, server=None):
    client = CloudDirectClient(account, store, llm_model="fake-model")
    server = server or FakeAgentServer()
    client._send = server  # type: ignore[method-assign]
    return client, server


def test_start_conversation_is_bound_and_survives_a_new_client():
    store = PostgresStore(DSN)
    try:
        account = FakeCloudAccount()
        client, server = _client(store, account)
        start = client.start_conversation(initial_message="do the thing", title="t1")

        assert start.status == "READY"
        cid = start.app_conversation_id
        route = store.conversation_route(cid)
        assert route is not None
        assert route["sandbox_id"] == start.sandbox_id

        # A brand-new client (empty cache) must still resolve the conversation
        # from the persisted route -- this is what makes recovery survive a
        # process restart. It shares the fake (one live sandbox), as would two
        # clients in one deployment.
        client2, _ = _client(store, account, server=server)
        conv = client2.get_conversation(cid)
        assert conv.id == cid
        assert conv.sandbox_id == start.sandbox_id
    finally:
        store.close()


def test_running_sandbox_is_reused_for_jr_tasks():
    store = PostgresStore(DSN)
    try:
        account = FakeCloudAccount()
        client, _ = _client(store, account)
        a = client.start_conversation(initial_message="one", title="a")
        b = client.start_conversation(initial_message="two", title="b")

        assert a.sandbox_id == b.sandbox_id
        assert account.start_count == 1
    finally:
        store.close()


def test_client_tools_force_a_dedicated_sandbox():
    store = PostgresStore(DSN)
    try:
        account = FakeCloudAccount()
        client, _ = _client(store, account)
        jr = client.start_conversation(initial_message="jr", title="jr")
        sr = client.start_conversation(
            initial_message="sr", title="sr",
            client_tools=[{"name": "sr_create_task"}],
        )

        assert sr.sandbox_id != jr.sandbox_id
        assert account.start_count == 2
    finally:
        store.close()


def test_reuse_disabled_provisions_a_fresh_sandbox():
    store = PostgresStore(DSN)
    try:
        account = FakeCloudAccount()
        client, _ = _client(store, account)
        client.reuse_sandboxes = False
        a = client.start_conversation(initial_message="one", title="a")
        b = client.start_conversation(initial_message="two", title="b")

        assert a.sandbox_id != b.sandbox_id
        assert account.start_count == 2
    finally:
        store.close()


def test_a_sandbox_the_cloud_no_longer_runs_is_not_reused():
    """``cloud_sandboxes`` remembers a sandbox as RUNNING for ever; the Cloud
    pauses and removes them. A stale row was handed out for every new
    conversation, and each one failed against a host that no longer answered
    (the live table held one such row, three weeks old)."""
    store = PostgresStore(DSN)
    try:
        account = FakeCloudAccount()
        client, _ = _client(store, account)
        first = client.start_conversation(initial_message="one", title="a")
        # Still running: the next conversation shares it.
        assert client.start_conversation(initial_message="two", title="b").sandbox_id \
            == first.sandbox_id
        assert account.start_count == 1

        account.sandboxes[first.sandbox_id]["status"] = "PAUSED"
        second = client.start_conversation(initial_message="three", title="c")
        assert second.sandbox_id != first.sandbox_id and account.start_count == 2
        statuses = {row["sandbox_id"]: row["status"] for row in store.list_sandboxes()}
        assert statuses == {first.sandbox_id: "PAUSED", second.sandbox_id: "RUNNING"}

        # Gone altogether: recorded as missing, and a third one is started.
        account.delete_sandbox(second.sandbox_id)
        third = client.start_conversation(initial_message="four", title="d")
        assert account.start_count == 3
        statuses = {row["sandbox_id"]: row["status"] for row in store.list_sandboxes()}
        assert statuses[second.sandbox_id] == "MISSING" and statuses[third.sandbox_id] == "RUNNING"
    finally:
        store.close()


def test_search_filters_by_tag_across_sandboxes():
    store = PostgresStore(DSN)
    try:
        account = FakeCloudAccount()
        client, _ = _client(store, account)
        client.reuse_sandboxes = False
        client.start_conversation(
            initial_message="a", title="a", tags={"orchestratorsr": "aaa"}
        )
        client.start_conversation(
            initial_message="b", title="b", tags={"orchestratorsr": "bbb"}
        )

        found, _ = client.search_conversations(tag=("orchestratorsr", "aaa"))
        assert len(found) == 1
        assert found[0].tags["orchestratorsr"] == "aaa"
    finally:
        store.close()


def test_reading_an_unrouted_conversation_raises_not_found():
    store = PostgresStore(DSN)
    try:
        account = FakeCloudAccount()
        client, _ = _client(store, account)
        with pytest.raises(CloudNotFound):
            client.get_conversation("00000000-0000-0000-0000-000000000000")
    finally:
        store.close()


def test_build_cloud_client_selects_transport(monkeypatch):
    store = PostgresStore(DSN)
    try:
        monkeypatch.setenv("OPENHANDS_API_KEY", "test-key")
        assert isinstance(build_cloud_client(store, transport="direct"),
                          CloudDirectClient)
        assert isinstance(build_cloud_client(store, transport="app"), CloudClient)
        with pytest.raises(ValueError):
            build_cloud_client(store, transport="nonsense")
    finally:
        store.close()


# ------------------------------------------------- repository selection mode


def test_direct_transport_reports_no_native_repository_selection():
    assert CloudDirectClient.supports_repository_selection is False
    assert CloudClient.supports_repository_selection is True


def test_direct_transport_reports_the_cloud_base_url():
    """The direct transport must expose ``base_url``, not just wrap a client.

    ``/api/system`` and ``/api/config`` report ``self.client.base_url`` for
    operator diagnostics. ``CloudDirectClient`` composes a ``CloudClient``, so
    without a delegating property both endpoints raised ``AttributeError`` and
    answered 500 -- on the default transport. The failure was found by driving
    the real HTTP server with ``OPENHANDS_TRANSPORT=direct``, not by reading the
    two classes, which is why this asserts through the public attribute.
    """
    for transport in ("direct", "app"):
        inner = CloudClient("https://cloud.example.test/", api_key="test-key")
        client = (CloudDirectClient(inner, store=None)  # type: ignore[arg-type]
                  if transport == "direct" else inner)
        assert client.base_url == "https://cloud.example.test"


def test_repository_task_default_mode_injects_a_clone_instruction(monkeypatch):
    """The workspace is empty, so the agent must be told to clone first.

    Asserts through the real call path: the message the agent-server receives
    (recorded by the fake) carries the clone instruction.
    """
    store = PostgresStore(DSN)
    try:
        monkeypatch.delenv("OPENHANDS_DIRECT_REPO_MODE", raising=False)
        account = FakeCloudAccount()
        client, server = _client(store, account)
        captured: dict = {}
        inner = client._send

        def recording(route, method, path, body=None):
            if method == "POST" and path == "/api/conversations":
                captured["body"] = body
            return inner(route, method, path, body)

        client._send = recording  # type: ignore[method-assign]
        client.start_conversation(
            initial_message="fix the bug",
            selected_repository="acme/repo",
            selected_branch="dev",
            title="repo task",
        )
        text = captured["body"]["initial_message"]["content"][0]["text"]
        assert "acme/repo" in text and "dev" in text
        assert text.endswith("fix the bug")
        assert "clone" in text.lower()
    finally:
        store.close()


def test_repository_task_refuse_mode_raises(monkeypatch):
    from orchestration.cloud_client import CloudError

    store = PostgresStore(DSN)
    try:
        monkeypatch.setenv("OPENHANDS_DIRECT_REPO_MODE", "refuse")
        account = FakeCloudAccount()
        client, _ = _client(store, account)
        with pytest.raises(CloudError):
            client.start_conversation(
                initial_message="fix",
                selected_repository="acme/repo",
                selected_branch="main",
            )
    finally:
        store.close()


def test_repository_task_allow_mode_is_unchanged(monkeypatch):
    store = PostgresStore(DSN)
    try:
        monkeypatch.setenv("OPENHANDS_DIRECT_REPO_MODE", "allow")
        account = FakeCloudAccount()
        client, _ = _client(store, account)
        assert client._apply_repository_mode("fix", "acme/repo", "main") == "fix"
    finally:
        store.close()


def test_repository_less_task_is_never_prefixed(monkeypatch):
    store = PostgresStore(DSN)
    try:
        monkeypatch.setenv("OPENHANDS_DIRECT_REPO_MODE", "refuse")
        account = FakeCloudAccount()
        client, _ = _client(store, account)
        # No repository: even refuse mode must not raise.
        assert client._apply_repository_mode("analyze", None, None) == "analyze"
    finally:
        store.close()
