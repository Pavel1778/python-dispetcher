"""Tests for the MCP config / plugin / scope-instruction builders."""

from __future__ import annotations

import json

import pytest

from app.bridge_config import (
    build_agent_settings_payload,
    build_mcp_config,
    build_plugin_mcp_json,
    build_plugin_spec,
    build_scope_instructions,
)
from app.config import Settings


def _settings(**over) -> Settings:
    base = dict(
        mcp_public_base_url="https://dispatcher.example.com",
        mcp_mount_path="/mcp",
        mcp_server_name="openhands-dispatcher",
        jr_scope_secret="scope-secret",
    )
    base.update(over)
    return Settings(**base)


def test_mcp_config_shape():
    cfg = build_mcp_config(_settings())
    server = cfg["mcpServers"]["openhands-dispatcher"]
    assert server["url"] == "https://dispatcher.example.com/mcp"
    assert server["transport"] == "streamable-http"
    assert server["timeout"] == 120
    # No bearer configured means no auth header is fabricated.
    assert "headers" not in server


def test_mcp_config_includes_bearer_and_timeout():
    cfg = build_mcp_config(
        _settings(mcp_bearer_token="tok", mcp_request_timeout_seconds=60)
    )
    server = cfg["mcpServers"]["openhands-dispatcher"]
    assert server["headers"]["Authorization"] == "Bearer tok"
    assert server["timeout"] == 60


def test_mcp_config_requires_public_base_url():
    with pytest.raises(ValueError):
        build_mcp_config(_settings(mcp_public_base_url=None))


def test_plugin_spec_none_when_unset():
    assert build_plugin_spec(_settings()) is None


def test_plugin_spec_with_ref_and_repo_path():
    spec = build_plugin_spec(
        _settings(
            mcp_plugin_source="github:acme/dispatcher-plugin",
            mcp_plugin_ref="v1",
            mcp_plugin_repo_path="plugins/dispatcher",
        )
    )
    assert spec == {
        "source": "github:acme/dispatcher-plugin",
        "ref": "v1",
        "repo_path": "plugins/dispatcher",
    }


def test_plugin_mcp_json_parses():
    rendered = build_plugin_mcp_json(_settings())
    parsed = json.loads(rendered)
    assert parsed["mcpServers"]["openhands-dispatcher"]["transport"] == "streamable-http"


def test_agent_settings_payload_wraps_mcp_config():
    payload = build_agent_settings_payload(_settings())
    assert "mcp_config" in payload["agent_settings"]


def test_scope_instructions_embed_a_verifiable_token():
    text = build_scope_instructions(
        workflow_id="wf1", task_id="t1", scope_token="tok.sig"
    )
    assert "wf1" in text
    assert "t1" in text
    assert "tok.sig" in text
    assert "request_workflow_verification" in text
    # The instruction must be explicit that it is not a completion.
    assert "does" in text and "COMPLETED" in text
