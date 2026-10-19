"""Shared test fixtures.

All tests use a fake OpenHands client and the in-memory store, so no network
access or real credentials are required. Real-Postgres tests are marked
``integration`` and skip unless ``TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.dispatcher.core import Dispatcher
from app.openhands_client import Conversation, StartTask
from app.store.memory import InMemoryStateStore


class FakeOpenHandsClient:
    """Deterministic stand-in for OpenHandsClient.

    Tests script the sequence of start tasks / conversations to return and can
    assert how many conversations were actually created.
    """

    def __init__(self) -> None:
        self.start_calls: list[dict] = []
        self.start_task_queue: list[StartTask] = []
        self.start_task_lookup: dict[str, StartTask] = {}
        self.conversation_lookup: dict[str, Conversation] = {}
        self.fail_with: Exception | None = None
        self._counter = 0

    @property
    def has_credentials(self) -> bool:
        return True

    def queue_start_task(self, task: StartTask) -> None:
        self.start_task_queue.append(task)
        self.start_task_lookup[task.id] = task

    def queue_conversation(self, conv: Conversation) -> None:
        self.conversation_lookup[conv.id] = conv

    async def start_conversation(self, **kwargs) -> StartTask:
        if self.fail_with is not None:
            raise self.fail_with
        self.start_calls.append(kwargs)
        self._counter += 1
        if self.start_task_queue:
            return self.start_task_queue.pop(0)
        return StartTask(
            id=f"start-{self._counter}",
            status="WORKING",
            app_conversation_id=None,
            sandbox_id=None,
            detail=None,
            created_at=None,
            updated_at=None,
        )

    async def get_start_task(self, start_task_id: str) -> StartTask | None:
        return self.start_task_lookup.get(start_task_id)

    async def get_start_tasks(self, ids):
        return [self.start_task_lookup[i] for i in ids if i in self.start_task_lookup]

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self.conversation_lookup.get(conversation_id)

    async def get_conversations(self, ids):
        return [self.conversation_lookup[i] for i in ids if i in self.conversation_lookup]

    async def aclose(self) -> None:
        return None


@pytest.fixture
def settings() -> Settings:
    return Settings(
        openhands_cloud_api_key="sk-test-not-real",
        database_url=None,
        dispatcher_enable_background_loop=False,
        dispatcher_poll_interval_seconds=0,
        jr_bridge_token="test-bridge-token",
        jr_scope_secret="test-scope-secret",
        jr_allowed_repositories="acme/widgets",
        # Tests that need the MCP transport build their own app and enable it.
        mcp_enabled=False,
    )


@pytest.fixture
async def store() -> InMemoryStateStore:
    s = InMemoryStateStore()
    await s.connect()
    yield s
    await s.close()


@pytest.fixture
def fake_client() -> FakeOpenHandsClient:
    return FakeOpenHandsClient()


@pytest.fixture
def dispatcher(settings, store, fake_client) -> Dispatcher:
    return Dispatcher(settings=settings, store=store, client=fake_client, worker_id="test-worker")


@pytest.fixture
def now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def pg_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL")
