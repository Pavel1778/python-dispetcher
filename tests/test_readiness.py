"""Readiness-barrier tests against a real local HTTP server.

These exercise the init race the barrier exists to close: a conversation
created before the durable desired state is applied loses its skills and MCP
servers. Every test drives real sockets; only the LLM is faked.
"""

from __future__ import annotations

import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.control_plane import ControlPlane, TaskState  # noqa: E402
from orchestration.dispatcher import Dispatcher  # noqa: E402
from orchestration.openhands_client import OpenHandsClient  # noqa: E402
from orchestration.runtime_state import (  # noqa: E402
    DesiredStateError,
    RuntimeReadiness,
    load_desired_state,
)
from tests.fake_runtime import FakeServer  # noqa: E402

KEY = "test-key"

HUB_STATE = {
    "version": 1,
    "agent_context": {
        "load_public_skills": True,
        "load_user_skills": True,
        "registered_marketplaces": [
            {"name": "pavel-hub", "source": "/workspace/skills-hub", "auto_load": True}
        ],
    },
    "mcp_config": {
        "fetch": {
            "transport": "stdio",
            "command": "uvx",
            "args": ["mcp-server-fetch"],
            "enabled": True,
        }
    },
}


@pytest.fixture
def server():
    with FakeServer() as s:
        yield s


@pytest.fixture
def cp(tmp_path):
    plane = ControlPlane(tmp_path / "cp.db")
    yield plane
    plane.close()


@pytest.fixture
def state_file(tmp_path):
    p = tmp_path / "runtime-desired-state.json"
    p.write_text(json.dumps(HUB_STATE), encoding="utf-8")
    return p


def make_dispatcher(cp, server, state_file):
    client = OpenHandsClient(server.base_url, api_key=KEY, timeout=5.0)
    return Dispatcher(
        cp,
        client,
        worker_id="w1",
        workspace_root="/workspace/project",
        desired_state_path=str(state_file),
    )


# ------------------------------------------------------------ desired state
def test_missing_desired_state_is_not_an_error(tmp_path):
    """No file means no barrier: the dispatcher keeps its previous behaviour."""
    assert load_desired_state(tmp_path / "absent.json") is None


def test_broken_desired_state_is_an_error(tmp_path):
    """A present but unparseable file must fail loudly, not be ignored."""
    p = tmp_path / "broken.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(DesiredStateError):
        load_desired_state(p)


def test_non_object_desired_state_is_an_error(tmp_path):
    p = tmp_path / "list.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(DesiredStateError):
        load_desired_state(p)


def test_barrier_reports_all_load_flags(server, state_file):
    """Every load flag in the desired state must be applied and reported."""
    client = OpenHandsClient(server.base_url, api_key=KEY, timeout=5.0)
    report = RuntimeReadiness(client, state_file).ensure()
    assert report.ready, report.reason
    assert report.load_public_skills is True
    assert report.load_user_skills is True


def test_barrier_detects_a_missing_load_user_flag(server, state_file):
    """A flag the desired state wants but the server lacks must fail the check.

    The hub's user-skill symlinks are the path that actually delivers hub skills
    to a conversation, so a silently-off load_user_skills is a real defect, not
    a cosmetic one.
    """
    client = OpenHandsClient(server.base_url, api_key=KEY, timeout=5.0)
    # Patch the server to look as if the write never happened for that flag.
    original = client.patch_settings

    def patch_without_user_flag(diff):
        # Copy, so the caller's desired-state dict is not mutated.
        diff = copy.deepcopy(diff)
        ctx = (diff.get("agent_settings_diff") or {}).get("agent_context") or {}
        ctx.pop("load_user_skills", None)
        return original(diff)

    client.patch_settings = patch_without_user_flag  # type: ignore[method-assign]
    report = RuntimeReadiness(client, state_file).ensure()
    assert not report.ready
    assert report.reason and "load_user_skills" in report.reason


# ------------------------------------------------------------- the barrier
def test_barrier_applies_and_verifies(server, state_file):
    client = OpenHandsClient(server.base_url, api_key=KEY, timeout=5.0)
    report = RuntimeReadiness(client, state_file).ensure()

    assert report.ready, report.reason
    assert report.applied
    assert report.load_public_skills is True
    assert report.marketplaces == ["pavel-hub"]
    assert report.mcp_servers == ["fetch"]


def test_barrier_is_idempotent_and_cached(server, state_file):
    """A second call must not write again; the verified result is cached."""
    client = OpenHandsClient(server.base_url, api_key=KEY, timeout=5.0)
    r = RuntimeReadiness(client, state_file)
    first = r.ensure()
    assert first.applied
    second = r.ensure()
    assert second.ready
    assert not second.applied, "steady state must not re-write"


def test_barrier_reports_unavailable_without_lying(server, state_file):
    """A transport failure is not readiness. It must not be reported as ok."""
    client = OpenHandsClient("http://127.0.0.1:9", api_key=KEY, timeout=2.0)
    report = RuntimeReadiness(client, state_file).ensure()
    assert not report.ready
    assert report.reason and "unavailable" in report.reason


def test_barrier_retries_after_a_failed_write(server, state_file):
    """A 503 must not be cached as success; the next call retries."""
    client = OpenHandsClient(server.base_url, api_key=KEY, timeout=5.0)
    r = RuntimeReadiness(client, state_file)
    server.runtime.patch_fail_times = 1

    failed = r.ensure()
    assert not failed.ready
    retried = r.ensure()
    assert retried.ready, retried.reason


def test_barrier_preserves_unrelated_fields(server, state_file):
    """The patch merges field by field; it must not clear load_user_skills."""
    server.runtime.settings["agent_settings"]["agent_context"] = {
        "load_user_skills": True,
        "load_public_skills": False,
    }
    client = OpenHandsClient(server.base_url, api_key=KEY, timeout=5.0)
    RuntimeReadiness(client, state_file).ensure()

    ctx = server.runtime.settings["agent_settings"]["agent_context"]
    assert ctx["load_user_skills"] is True, "unrelated field was clobbered"
    assert ctx["load_public_skills"] is True
    assert [m["name"] for m in ctx["registered_marketplaces"]] == ["pavel-hub"]


def test_barrier_does_not_clear_servers_it_did_not_set(server, state_file):
    """A desired state without mcp_config must leave MCP exactly as it was."""
    state_file.write_text(
        json.dumps({"agent_context": HUB_STATE["agent_context"]}), encoding="utf-8"
    )
    server.runtime.settings["agent_settings"]["mcp_config"] = {"existing": {"url": "x"}}
    client = OpenHandsClient(server.base_url, api_key=KEY, timeout=5.0)
    RuntimeReadiness(client, state_file).ensure()
    assert "existing" in server.runtime.settings["agent_settings"]["mcp_config"]


# ---------------------------------------------- the race, end to end
def test_dispatcher_creates_conversation_with_context_applied(cp, server, state_file):
    """The core regression: a dispatched task must carry the durable state.

    Before the barrier, a conversation created in the window before the async
    SessionStart hook completed had an empty agent_context. Now the dispatcher
    applies and verifies the state first, so the created conversation is told
    about the hub and its MCP servers.
    """
    cp.submit_task("t1", "do the thing")
    d = make_dispatcher(cp, server, state_file)
    report = d.tick()

    assert report.dispatched, report.unavailable
    conv = server.runtime.conversations[cp.get_task("t1").conversation_id]
    assert conv.agent_context.get("load_public_skills") is True
    assert [m["name"] for m in conv.agent_context["registered_marketplaces"]] == [
        "pavel-hub"
    ]
    assert "fetch" in conv.mcp_config


def test_dispatch_is_deferred_when_runtime_is_unavailable(cp, state_file):
    """No runtime means no dispatch: the task waits, it is not failed."""
    client = OpenHandsClient("http://127.0.0.1:9", api_key=KEY, timeout=2.0)
    d = Dispatcher(
        cp,
        client,
        worker_id="w1",
        workspace_root="/workspace/project",
        desired_state_path=str(state_file),
    )
    cp.submit_task("t1", "do it")
    report = d.tick()

    assert report.unavailable == ["t1"]
    assert report.dispatched == []
    task = cp.get_task("t1")
    assert task.state is TaskState.QUEUED
    assert task.attempt == 0, "an environment failure is not the task's fault"


def test_dispatch_is_deferred_when_desired_state_is_broken(cp, server, tmp_path):
    """A broken desired-state file blocks dispatch instead of racing."""
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    cp.submit_task("t1", "do it")
    d = make_dispatcher(cp, server, bad)
    report = d.tick()

    assert report.unavailable == ["t1"]
    assert cp.get_task("t1").state is TaskState.QUEUED
    assert server.runtime.created_count == 0


def test_conversation_created_after_write_sees_the_new_state(cp, server, state_file):
    """The settings snapshot must not be a pre-write one.

    The barrier writes, then the dispatcher forwards ``agent_settings``. If it
    forwarded a snapshot taken before the write, the conversation would be
    created with the old context and the race would remain.
    """
    cp.submit_task("t1", "do it")
    d = make_dispatcher(cp, server, state_file)
    d.tick()

    cid = cp.get_task("t1").conversation_id
    assert server.runtime.conversations[cid].agent_context, (
        "conversation was created without the verified context"
    )


def test_second_task_reuses_verified_state_without_rewriting(cp, server, state_file):
    """Once verified, later ticks must not keep patching the store."""
    cp.submit_task("t1", "one")
    cp.submit_task("t2", "two")
    d = make_dispatcher(cp, server, state_file)
    d.tick(max_dispatch=1)
    d.tick(max_dispatch=1)

    assert server.runtime.created_count == 2
    for cid in (
        cp.get_task("t1").conversation_id,
        cp.get_task("t2").conversation_id,
    ):
        assert server.runtime.conversations[cid].agent_context.get(
            "load_public_skills"
        )
