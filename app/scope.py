"""Per-conversation scope tokens.

A shared bearer token proves "this caller may talk to the bridge", but it does
not prove *which* workflow the caller is allowed to act on. Since one bridge
serves every Sr conversation, a bare bearer token would let any Sr act on any
other Sr's workflow if it guessed ids.

To close that, the dispatcher mints a short-lived HMAC token per Sr
conversation and embeds it in the Sr system prompt. Every bridge tool call must
present it, and the token is verified against the claimed workflow/task ids.
The signing key never leaves the dispatcher process.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass

# Tokens are short-lived: a Sr conversation only needs to spawn children while
# it is running. Re-minting on restart is handled by recover().
DEFAULT_TTL_SECONDS = 24 * 60 * 60


class ScopeError(ValueError):
    """Raised when a scope token is missing, malformed, expired, or mismatched."""


@dataclass(frozen=True)
class Scope:
    workflow_id: str
    task_id: str
    expires_at: int


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(secret: str, payload: str) -> str:
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return _b64e(digest)


def mint_scope_token(
    secret: str,
    *,
    workflow_id: str,
    task_id: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """Create ``<payload>.<signature>`` binding a workflow/task pair."""
    if not secret:
        raise ScopeError("scope signing secret is not configured")
    expires_at = int((now if now is not None else time.time()) + ttl_seconds)
    payload = f"{workflow_id}:{task_id}:{expires_at}"
    encoded = _b64e(payload.encode())
    return f"{encoded}.{_sign(secret, encoded)}"


def verify_scope_token(
    secret: str,
    token: str,
    *,
    workflow_id: str,
    task_id: str,
    now: float | None = None,
) -> Scope:
    """Verify a token and confirm it authorises exactly this workflow/task."""
    if not secret:
        raise ScopeError("scope signing secret is not configured")
    if not token or "." not in token:
        raise ScopeError("malformed scope token")
    encoded, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(secret, encoded)):
        raise ScopeError("bad scope token signature")
    try:
        payload = _b64d(encoded).decode()
    except Exception as exc:  # noqa: BLE001 - any decode failure is a bad token
        raise ScopeError("malformed scope token payload") from exc
    parts = payload.split(":")
    if len(parts) != 3:
        raise ScopeError("malformed scope token payload")
    token_workflow, token_task, expires_raw = parts
    try:
        expires_at = int(expires_raw)
    except ValueError as exc:
        raise ScopeError("malformed scope token expiry") from exc
    if expires_at < int(now if now is not None else time.time()):
        raise ScopeError("scope token expired")
    if token_workflow != workflow_id or token_task != task_id:
        raise ScopeError("scope token does not authorise this workflow/task")
    return Scope(workflow_id=workflow_id, task_id=task_id, expires_at=expires_at)
