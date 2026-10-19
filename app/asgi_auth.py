"""ASGI bearer-token guard for the mounted MCP endpoint.

Kept dependency-free (stdlib only) so it can be unit-tested without the
optional ``mcp`` package installed, and applied by ``app.main`` around the
mounted MCP sub-app.

The scope token remains the *authorisation* boundary for individual tool calls
(see ``app/scope.py``). This guard is defence in depth: it stops unauthenticated
callers from even reaching the MCP handshake when ``MCP_BEARER_TOKEN`` is set.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Awaitable, Callable
from typing import Any


class BearerTokenMiddleware:
    """Reject requests whose ``Authorization`` header is not ``Bearer <token>``."""

    def __init__(self, app: Any, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}"

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        provided = ""
        for key, value in scope.get("headers") or []:
            if key.lower() == b"authorization":
                provided = value.decode("latin-1")
                break

        # Constant-time comparison: a plain ``!=`` leaks the token byte by byte.
        if not hmac.compare_digest(provided, self._expected):
            body = json.dumps({"detail": "unauthorized"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self._app(scope, receive, send)
