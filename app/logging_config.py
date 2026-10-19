"""Structured JSON logging with defence-in-depth secret redaction.

Two independent layers protect secrets:

1. A ``RedactionFilter`` scrubs known secret values (and bearer tokens) from
   every rendered log record.
2. Callers use :func:`safe_extra` so only whitelisted fields are emitted.

Never log raw API responses from endpoints that may carry credentials.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any, Iterable

_REDACTED = "<redacted>"

# Matches "Authorization: Bearer xxx" and bare bearer tokens in free text.
_BEARER_RE = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]+)")
_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9._\-]{6,}\b")

# Field names that must never be emitted verbatim.
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "token",
        "secret",
        "password",
        "sandbox_api_key",
        "session_api_key",
        "service_role",
        "service_role_key",
        "database_url",
        "dsn",
    }
)


def scrub_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove known secret values and token-shaped substrings from text."""
    if not text:
        return text
    for secret in secrets:
        if secret:
            text = text.replace(secret, _REDACTED)
    text = _BEARER_RE.sub(lambda m: m.group(1) + _REDACTED, text)
    text = _TOKEN_RE.sub(_REDACTED, text)
    return text


def scrub_mapping(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Recursively redact sensitive keys and values in a JSON-like structure."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in _SENSITIVE_KEYS:
                out[key] = _REDACTED
            else:
                out[key] = scrub_mapping(item, secrets)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub_mapping(v, secrets) for v in value]
    if isinstance(value, str):
        return scrub_text(value, secrets)
    return value


class RedactionFilter(logging.Filter):
    """Scrub secrets from the final rendered message of every record."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def add_secrets(self, secrets: Iterable[str]) -> None:
        for s in secrets:
            if s and s not in self._secrets:
                self._secrets.append(s)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = scrub_text(str(record.getMessage()), self._secrets)
            record.args = ()
        except Exception:  # pragma: no cover - never break logging
            pass
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> RedactionFilter:
    """Install a JSON handler with redaction. Idempotent across re-imports."""
    root = logging.getLogger()
    root.setLevel(level.upper())

    redaction = RedactionFilter(secrets)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(redaction)
    root.addHandler(handler)
    return redaction


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def safe_extra(**fields: Any) -> dict[str, Any]:
    """Build a logging ``extra`` mapping, used as ``logger.info(msg, **safe_extra(...))``.

    Only pass non-sensitive identifiers (ids, statuses, counts, durations).
    """
    cleaned = {k: v for k, v in fields.items() if v is not None}
    return {"extra": {"extra_fields": cleaned}}
