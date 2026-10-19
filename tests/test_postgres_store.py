"""Postgres store configuration tests (no live database required)."""

from __future__ import annotations

from app.store.postgres import PostgresStateStore


async def test_transaction_pooler_disables_prepared_statements(monkeypatch):
    captured: dict = {}

    class FakePool:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def open(self, **kwargs):
            return None

    monkeypatch.setattr("app.store.postgres.AsyncConnectionPool", FakePool)
    store = PostgresStateStore("postgresql://x", transaction_pooler=True)
    await store.connect()
    assert captured["kwargs"]["prepare_threshold"] is None


async def test_direct_connection_keeps_prepared_statements(monkeypatch):
    captured: dict = {}

    class FakePool:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def open(self, **kwargs):
            return None

    monkeypatch.setattr("app.store.postgres.AsyncConnectionPool", FakePool)
    store = PostgresStateStore("postgresql://x", transaction_pooler=False)
    await store.connect()
    assert "prepare_threshold" not in captured["kwargs"]
