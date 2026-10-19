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
    database_url: str | None = None
    database_connect_timeout_seconds: int = 10
    database_pool_min_size: int = 1
    database_pool_max_size: int = 5

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

    def redaction_secrets(self) -> list[str]:
        """Secret values that must never appear in logs or API responses."""
        candidates = [
            self.openhands_cloud_api_key,
            self.openhands_api_key,
            self.jr_bridge_token,
            self.database_url,
        ]
        return [c for c in candidates if c]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
