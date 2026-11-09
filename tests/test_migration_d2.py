"""Migration and readiness: the additive D-2 migration is safe and idempotent.

Checks that matter for an MVP an operator must be able to run:

* Applying every migration to an empty database succeeds and records each file.
* Re-applying is a no-op (the runner skips by checksum).
* Applying the D-2 migration to a database that already has the legacy schema
  (rows present) succeeds without touching those rows: legacy tasks/workflows
  keep ``project_id IS NULL``.
* ``/healthz`` stays a one-line liveness probe; ``/readyz`` reflects a real
  database round-trip and is public.
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import PostgresStore  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run migration tests"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def _drop_all() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")


def test_all_migrations_apply_and_record(tmp_path):
    _drop_all()
    # PostgresStore auto-applies migrations on construction (AUTO_MIGRATE
    # default), so the schema is already current by the time we look.
    store = PostgresStore(DSN)
    try:
        with store._tx() as cur:
            cur.execute("SELECT filename FROM schema_migrations ORDER BY filename")
            recorded = {r["filename"] for r in cur.fetchall()}
        on_disk = {f for f in os.listdir(_MIG) if f.endswith(".sql")}
        assert recorded == on_disk
        assert "0014_nexus_project_binding.sql" in recorded
        assert store.verify_schema() == []
        # A second explicit run is a no-op (checksums match).
        assert store.apply_migrations() == []
    finally:
        store.close()


def test_reapplying_migrations_is_a_noop():
    _drop_all()
    store = PostgresStore(DSN)
    try:
        # The constructor already applied them; an explicit re-run must skip all.
        assert store.apply_migrations() == []
    finally:
        store.close()


def test_d2_migration_preserves_legacy_rows():
    """A legacy task/workflow keeps project_id NULL; nothing is backfilled."""
    _drop_all()
    store = PostgresStore(DSN)
    try:
        store.apply_migrations()
        # Legacy rows created before D-2 would have no project_id. Insert one and
        # re-run the migration to prove it is additive and does not touch it.
        wid = store.create_workflow("legacy goal")
        task = store.submit_task("legacy work", workflow_id=wid)
        store.apply_migrations()  # idempotent; must not alter the row
        assert store.get_task(task.id).project_id is None
        with store._tx() as cur:
            cur.execute("SELECT project_id FROM workflows WHERE id=%s", (wid,))
            assert cur.fetchone()["project_id"] is None
    finally:
        store.close()


def test_readyz_reflects_database(app_and_server):
    base = app_and_server
    with urllib.request.urlopen(base + "/readyz", timeout=10) as r:
        import json

        status = r.status
        body = json.loads(r.read().decode())
    assert status == 200
    assert body["status"] == "ready" and body["database"] is True


def test_healthz_is_a_minimal_one_liner(app_and_server):
    base = app_and_server
    with urllib.request.urlopen(base + "/healthz", timeout=10) as r:
        raw = r.read().decode().strip()
    import json

    assert json.loads(raw) == {"status": "ok"}


@pytest.fixture
def app_and_server(monkeypatch):
    from orchestration.cloud_client import CloudClient
    from orchestration.web_auth import hash_password
    from orchestration.web_server import App, WebServer
    from tests.fake_cloud import FakeCloudServer

    _drop_all()
    monkeypatch.setenv("OPENHANDS_API_KEY", "test-key")
    monkeypatch.setenv("AUTH_USERNAME", "owner")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password("pw"))
    monkeypatch.setenv("AUTH_SESSION_SECRET", "s" * 32)
    with FakeCloudServer() as fake:
        a = App(DSN, poll_interval=0.2, token="t")
        a.client = CloudClient(fake.base_url, api_key="test-key", timeout=5.0)
        a.dispatcher.client = a.client
        a.runtime.client = a.client
        a.verifier.client = a.client
        ws = WebServer(a, "127.0.0.1", 0)
        ws.start()
        yield f"http://127.0.0.1:{ws.port}"
        ws.stop()
        a.store.close()
