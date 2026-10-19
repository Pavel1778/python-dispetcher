"""FastAPI service exposing /health, /ready, and the protected Jr bridge.

Health semantics (deliberately conservative):
* ``/health`` is a liveness probe: the process is up. It does NOT prove tasks
  are being processed.
* ``/ready`` checks dependencies (database + API key) and reports whether a
  full dispatch cycle has succeeded recently. It never leaks secrets.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings, get_settings
from .dispatcher.core import Dispatcher, JrBridgeUnavailableError
from .logging_config import configure_logging, get_logger, safe_extra
from .openhands_client import OpenHandsClient
from .store.base import StateStore, StoreUnavailableError
from .store.memory import InMemoryStateStore
from .store.postgres import PostgresStateStore

logger = get_logger(__name__)


class _Runtime:
    """Holds wired dependencies so tests can override them."""

    def __init__(self) -> None:
        self.settings: Settings | None = None
        self.store: StateStore | None = None
        self.client: OpenHandsClient | None = None
        self.dispatcher: Dispatcher | None = None
        self.loop_task: asyncio.Task[Any] | None = None
        self.last_heartbeat_ok: bool | None = None
        self.heartbeat_error: str | None = None


runtime = _Runtime()


def build_store(settings: Settings) -> StateStore:
    if settings.database_url:
        return PostgresStateStore(
            settings.database_url,
            pool_min=settings.database_pool_min_size,
            pool_max=settings.database_pool_max_size,
        )
    logger.warning("DATABASE_URL not set; using in-memory store (state is not durable)")
    return InMemoryStateStore()


async def send_heartbeat(settings: Settings) -> bool:
    """POST to the Better Stack heartbeat URL, if configured."""
    if not settings.heartbeat_url:
        return False
    try:
        async with httpx.AsyncClient(timeout=settings.heartbeat_timeout_seconds) as client:
            resp = await client.get(settings.heartbeat_url)
            ok = 200 <= resp.status_code < 300
            runtime.last_heartbeat_ok = ok
            runtime.heartbeat_error = None if ok else f"HTTP {resp.status_code}"
            return ok
    except httpx.HTTPError as exc:
        runtime.last_heartbeat_ok = False
        runtime.heartbeat_error = type(exc).__name__
        return False


async def dispatcher_loop(settings: Settings) -> None:
    """Background loop. Heartbeats only after a full successful cycle."""
    assert runtime.dispatcher is not None
    interval = settings.dispatcher_poll_interval_seconds
    while True:
        try:
            stats = await runtime.dispatcher.run_cycle()
            if runtime.dispatcher.last_cycle_succeeded:
                logger.info("dispatch cycle complete", **safe_extra(**stats))
                if settings.heartbeat_url:
                    await send_heartbeat(settings)
            else:
                logger.warning("dispatch cycle incomplete", **safe_extra(**stats))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - loop must survive any error
            logger.error("dispatch loop error", **safe_extra(error=type(exc).__name__))
        await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = runtime.settings or get_settings()
    runtime.settings = settings
    configure_logging(settings.log_level, settings.redaction_secrets())

    store = runtime.store or build_store(settings)
    runtime.store = store
    try:
        await store.connect()
        if isinstance(store, PostgresStateStore):
            applied = await store.apply_migrations()
            if applied:
                logger.info("applied migrations", **safe_extra(files=applied))
    except StoreUnavailableError:
        # Do not crash the web process: /ready will report not-ready.
        logger.error("state store unavailable at startup")

    client = runtime.client or OpenHandsClient(
        settings.resolved_api_key,
        base_url=settings.openhands_api_base,
        timeout_seconds=settings.openhands_http_timeout_seconds,
        max_retries=settings.openhands_http_max_retries,
        backoff_base_seconds=settings.openhands_http_backoff_base_seconds,
        backoff_max_seconds=settings.openhands_http_backoff_max_seconds,
    )
    runtime.client = client

    dispatcher = runtime.dispatcher or Dispatcher(
        settings=settings, store=store, client=client
    )
    runtime.dispatcher = dispatcher

    try:
        await dispatcher.recover()
    except Exception as exc:  # noqa: BLE001
        logger.warning("recovery failed", **safe_extra(error=type(exc).__name__))

    if settings.dispatcher_enable_background_loop:
        runtime.loop_task = asyncio.create_task(dispatcher_loop(settings))

    try:
        yield
    finally:
        if runtime.loop_task is not None:
            runtime.loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runtime.loop_task
            runtime.loop_task = None
        await client.aclose()
        await store.close()


app = FastAPI(title="OpenHands Dispatcher", version=__version__, lifespan=lifespan)


# --- schemas ---

class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


class ReadyResponse(BaseModel):
    ready: bool
    database: bool
    api_key_configured: bool
    dispatcher_cycle_ok: bool
    last_cycle_at: str | None = None
    details: dict[str, str] = Field(default_factory=dict)


class JrTaskRequest(BaseModel):
    workflow_id: str
    parent_task_id: str
    title: str
    prompt: str
    idempotency_key: str
    selected_repository: str | None = None
    selected_branch: str | None = None


class JrTaskResponse(BaseModel):
    task_id: str
    created: bool
    status: str


# --- endpoints ---

@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness only. A 200 here does NOT mean tasks are being processed."""
    return HealthResponse(
        status="ok", service=(runtime.settings or get_settings()).service_name, version=__version__
    )


@app.get("/ready", response_model=ReadyResponse)
async def ready() -> ReadyResponse:
    settings = runtime.settings or get_settings()
    details: dict[str, str] = {}

    db_ok = False
    if runtime.store is not None:
        try:
            db_ok = await runtime.store.healthcheck()
        except Exception as exc:  # noqa: BLE001
            details["database"] = type(exc).__name__
    else:
        details["database"] = "store not initialised"

    api_key_ok = bool(settings.resolved_api_key)
    if not api_key_ok:
        details["api_key"] = "not configured"

    cycle_ok = bool(runtime.dispatcher and runtime.dispatcher.last_cycle_succeeded)
    if runtime.dispatcher and runtime.dispatcher.last_cycle_at:
        details["last_cycle_at"] = runtime.dispatcher.last_cycle_at.isoformat()
    if runtime.dispatcher and not cycle_ok:
        details["dispatcher"] = "no successful cycle yet"

    is_ready = db_ok and api_key_ok
    return ReadyResponse(
        ready=is_ready,
        database=db_ok,
        api_key_configured=api_key_ok,
        dispatcher_cycle_ok=cycle_ok,
        last_cycle_at=(
            runtime.dispatcher.last_cycle_at.isoformat()
            if runtime.dispatcher and runtime.dispatcher.last_cycle_at
            else None
        ),
        details=details,
    )


def require_bridge_token(
    authorization: str | None = Header(default=None),
) -> None:
    settings = runtime.settings or get_settings()
    if not settings.jr_bridge_enabled:
        raise HTTPException(status_code=404, detail="Jr bridge disabled")
    if not settings.jr_bridge_token:
        raise HTTPException(status_code=503, detail="Jr bridge not configured")
    expected = f"Bearer {settings.jr_bridge_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


@app.post("/jr/tasks", response_model=JrTaskResponse)
async def create_jr_task(
    payload: JrTaskRequest, _: None = Depends(require_bridge_token)
) -> JrTaskResponse:
    """Protected bridge a Sr task calls to spawn a Jr child conversation."""
    if runtime.dispatcher is None:
        raise HTTPException(status_code=503, detail="dispatcher not initialised")
    try:
        task, created = await runtime.dispatcher.create_junior_task(
            workflow_id=payload.workflow_id,
            parent_task_id=payload.parent_task_id,
            title=payload.title,
            prompt=payload.prompt,
            idempotency_key=payload.idempotency_key,
            selected_repository=payload.selected_repository,
            selected_branch=payload.selected_branch,
        )
    except JrBridgeUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StoreUnavailableError as exc:
        raise HTTPException(status_code=503, detail="state store unavailable") from exc
    return JrTaskResponse(task_id=task.id, created=created, status=task.status)
