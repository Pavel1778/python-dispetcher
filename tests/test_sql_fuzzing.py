"""Injection regression vectors against a disposable PostgreSQL instance.

These cases exercise real parameterized queries, not a SQL parser or mock. They
are targeted regression vectors, not a claim of exhaustive fuzzing.
"""
from __future__ import annotations

import os
import uuid

import pytest

from orchestration.cloud_store import PostgresStore

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable PostgreSQL")


@pytest.mark.parametrize("payload", [
    "' OR 1=1 --",
    "'; DROP TABLE tasks; --",
    "x' UNION SELECT pg_sleep(1)--",
    "\\' OR 'a'='a",
    "тест 😀 ' % _ ; --",
    "a" * 1024 + "'/*",
])
def test_task_user_values_are_sql_parameters(payload):
    store = PostgresStore(DSN)
    suffix = uuid.uuid4().hex
    key = payload + suffix
    try:
        submitted = store.submit_task(payload, idempotency_key=key)
        duplicate = store.submit_task("ignored", idempotency_key=key)
        assert duplicate.id == submitted.id
        assert store.get_task(submitted.id).goal == payload
        assert store.list_tasks(owner=payload) == []
        with store._tx() as cur:
            cur.execute("SELECT to_regclass('public.tasks') AS name")
            assert cur.fetchone()["name"] == "tasks"
    finally:
        try:
            with store._tx() as cur:
                cur.execute("DELETE FROM tasks WHERE idempotency_key=%s", (key,))
        finally:
            store.close()
