"""Runtime configuration loaded from environment variables.

Secrets are read from the environment only. They are never written to disk,
never logged, and never returned from HTTP endpoints.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- OpenHands Cloud ---
    # Preferred variable, with a documented fallback to the system-managed key.
    openhands_cloud_api_key: str | None = None
    openhands_api_key: str | None = None
    openhands_api_base: str = "https://app.all-hands.dev"
    openhands_http_timeout_seconds: float = 30.0
    openhands_http_max_retries: int = 3
    openhands_http_backoff_base_seconds: float = 0.5
    openhands_http_backoff_max_seconds: float = 10.0

    # --- Persistence ---
    # Supabase connection strings are read from DATABASE_URL, falling back to
    # SUPABASE_DB_URL. The password is only ever supplied via the environment.
    database_url: str | None = None
    supabase_db_url: str | None = None
    database_connect_timeout_seconds: int = 10
    database_pool_min_size: int = 1
    database_pool_max_size: int = 5
    # Supabase "Transaction pooler" runs PgBouncer in transaction mode, which
    # does not support server-side prepared statements. psycopg 3 prepares
    # statements automatically after a few executions, so this must be disabled
    # when connecting through the pooler (port 6543).
    database_transaction_pooler: bool = True

    # --- Dispatcher loop ---
    service_name: str = "openhands-dispatcher"
    dispatcher_enable_background_loop: bool = True
    dispatcher_poll_interval_seconds: float = 15.0
    dispatcher_batch_size: int = 5
    dispatcher_max_attempts: int = 5
    dispatcher_lease_seconds: int = 120
    dispatcher_start_task_timeout_seconds: int = 900
    dispatcher_worker_id: str | None = None

    # --- Jr bridge (protected endpoint Sr uses to spawn child conversations) ---
    jr_bridge_token: str | None = None
    jr_bridge_enabled: bool = True
    # HMAC key used to mint per-conversation scope tokens. Without it a shared
    # bearer token would let any Sr act on any workflow, so the bridge refuses
    # to mint scoped tokens when this is unset.
    jr_scope_secret: str | None = None
    jr_scope_ttl_seconds: int = 24 * 60 * 60
    # Hard cap on children a single Sr task may spawn.
    jr_max_children_per_workflow: int = 20
    # Repositories Sr is allowed to dispatch work into, comma-separated.
    # Empty = deny all, so a misconfiguration fails closed rather than allowing
    # arbitrary repos. Kept as a string (not list[str]) because pydantic-settings
    # JSON-decodes complex types, which would make a plain "owner/repo" value -
    # or an empty value - crash startup.
    jr_allowed_repositories: str = ""
    # MCP transport (the real Sr -> Jr mechanism). Mounted on the same app at
    # MCP_MOUNT_PATH; disable to run HTTP-bridge-only.
    mcp_enabled: bool = True
    mcp_mount_path: str = "/mcp"
    mcp_server_name: str = "openhands-dispatcher"
    # Public HTTPS base URL the cloud sandbox uses to reach this service. Empty
    # means the Sr conversation cannot be given the tools automatically.
    mcp_public_base_url: str | None = None
    mcp_bearer_token: str | None = None
    mcp_request_timeout_seconds: int = 120
    # Optional git-hosted plugin carrying .mcp.json (see app/bridge_config.py).
    mcp_plugin_source: str | None = None
    mcp_plugin_ref: str | None = None
    mcp_plugin_repo_path: str | None = None

    # --- Monitoring ---
    heartbeat_url: str | None = None
    heartbeat_timeout_seconds: float = 10.0

    log_level: str = "INFO"
    default_repository: str | None = None
    default_branch: str | None = None

    @property
    def resolved_api_key(self) -> str | None:
        """Return the OpenHands API key, preferring the explicit cloud variable."""
        return self.openhands_cloud_api_key or self.openhands_api_key

    @property
    def resolved_database_url(self) -> str | None:
        """PostgreSQL DSN, preferring DATABASE_URL over SUPABASE_DB_URL."""
        return self.database_url or self.supabase_db_url

    @property
    def allowed_repositories(self) -> list[str]:
        """Parsed repository allowlist. Empty means deny all."""
        return [r.strip() for r in self.jr_allowed_repositories.split(",") if r.strip()]

    def redaction_secrets(self) -> list[str]:
        """Secret values that must never appear in logs or API responses."""
        candidates = [
            self.openhands_cloud_api_key,
            self.openhands_api_key,
            self.jr_bridge_token,
            self.jr_scope_secret,
            self.mcp_bearer_token,
            self.database_url,
            self.supabase_db_url,
        ]
        return [c for c in candidates if c]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
