"""Configuration resolution: key fallback, DSN fallback, redaction list."""

from __future__ import annotations

from app.config import Settings


def test_api_key_prefers_cloud_variable():
    s = Settings(openhands_cloud_api_key="sk-cloud", openhands_api_key="sk-fallback")
    assert s.resolved_api_key == "sk-cloud"


def test_api_key_falls_back_to_system_variable():
    s = Settings(openhands_cloud_api_key=None, openhands_api_key="sk-fallback")
    assert s.resolved_api_key == "sk-fallback"


def test_database_url_prefers_database_url():
    s = Settings(database_url="postgresql://a", supabase_db_url="postgresql://b")
    assert s.resolved_database_url == "postgresql://a"


def test_database_url_falls_back_to_supabase_db_url():
    s = Settings(database_url=None, supabase_db_url="postgresql://b")
    assert s.resolved_database_url == "postgresql://b"


def test_redaction_secrets_includes_all_secrets():
    s = Settings(
        openhands_cloud_api_key="sk-cloud",
        openhands_api_key="sk-fallback",
        jr_bridge_token="bridge",
        database_url="postgresql://user:pw@host/db",
    )
    secrets = s.redaction_secrets()
    assert "sk-cloud" in secrets
    assert "sk-fallback" in secrets
    assert "bridge" in secrets
    assert "postgresql://user:pw@host/db" in secrets
