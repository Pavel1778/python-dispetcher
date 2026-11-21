"""Hostile text through the real API must be stored as text, never executed.

The audit found the SQL is built with f-strings in several places, so this
checks the only thing that matters: what happens when a caller sends SQL. Every
payload is written through the API's own free-text fields (a project name, a
conversation title) and then the database is read back directly.

The store is the app's own DSN, not the one this process started with: the test
harness rewrites ``TEST_SUPABASE_DB_URL`` to a per-process database.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_studio_api import studio_stack  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")),
    reason="set TEST_SUPABASE_DB_URL to run the injection probe")

#: Each payload would destroy or alter data if it reached the database as SQL.
PAYLOADS = [
    "'; DROP TABLE nexus_projects; --",
    "x' OR '1'='1",
    '"; DELETE FROM nexus_run_tasks; --',
    "\\'; UPDATE nexus_projects SET name='pwned'; --",
    "') UNION SELECT password_hash FROM sessions --",
]


@pytest.fixture
def served(monkeypatch):
    monkeypatch.setenv("NEXUS_ALLOW_PRIVATE_TARGETS", "1")
    with studio_stack(monkeypatch) as stack:
        yield stack


def test_free_text_fields_store_sql_as_text(served):
    api = served.api
    status, body = api.post("/api/v1/projects", {"name": "injection-base"})
    assert status == 201, body
    project = body["project"]["id"]

    for payload in PAYLOADS:
        s1, _ = api.post("/api/v1/projects", {"name": payload[:120]})
        s2, _ = api.post(f"/api/v1/projects/{project}/conversations",
                         {"title": payload, "mode": "ask"})
        assert s1 == 201 and s2 == 201, (payload, s1, s2)

    import psycopg

    with psycopg.connect(served.app.store.dsn) as conn:
        with conn.cursor() as cur:
            # The tables the payloads targeted are still there.
            cur.execute("SELECT to_regclass('nexus_projects') IS NOT NULL")
            assert cur.fetchone()[0] is True
            cur.execute("SELECT to_regclass('nexus_run_tasks') IS NOT NULL")
            assert cur.fetchone()[0] is True
            # No row was rewritten by the UPDATE payload.
            cur.execute("SELECT count(*) FROM nexus_projects WHERE name='pwned'")
            assert cur.fetchone()[0] == 0
            # Every payload was stored verbatim, as data.
            cur.execute("SELECT count(*) FROM nexus_projects WHERE name = ANY(%s)",
                        (PAYLOADS,))
            assert cur.fetchone()[0] == len(PAYLOADS)
