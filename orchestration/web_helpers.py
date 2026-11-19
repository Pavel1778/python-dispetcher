"""Shared, side-effect-free helpers for the HTTP layer.

Extracted from ``web_server.py`` (strangler refactoring) so the server module
keeps routing, auth and handlers only. Everything here is deterministic and
either pure or self-contained (the login throttle). Imported by
``orchestration.web_server``; the names are re-exported there so existing
callers and tests that import from ``orchestration.web_server`` keep working.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any

from .nexus_specs import max_spec_chars

log = logging.getLogger("web_helpers")


def title_from_message(text: str, *, limit: int = 60) -> str:
    """A conversation title made from the first line of its first message.

    Cut at a word boundary so the history never shows half a word, and marked
    with an ellipsis when something was dropped.
    """
    stripped = (text or "").strip()
    line = " ".join(stripped.splitlines()[0].split()) if stripped else ""
    if len(line) <= limit:
        return line
    cut = line[:limit]
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,.;:-") + "…"


def _task_brief(task: Any) -> dict[str, Any]:
    """A compact task view for list endpoints (no events or evidence payload)."""
    return {
        "id": task.id,
        "goal": task.goal,
        "state": task.state.value,
        "attempt": task.attempt,
        "max_attempts": task.max_attempts,
        "project_id": task.project_id,
        "workflow_id": task.workflow_id,
        "repository": task.selected_repository,
        "branch": task.selected_branch,
        "updated_at": task.updated_at,
    }

class BodyTooLarge(ValueError):
    """The request body is larger than its route accepts. Answered with 413."""


#: JSON bodies under ``/api/v1/projects`` unless the route carries a task text.
_PROJECT_BODY_BYTES = 4 * 1024 * 1024
_TEXT_ROUTE = re.compile(
    r"/api/v1/projects/[^/]+/(?:goals|conversations/[^/]+/messages)\Z")


def _json_body_cap(path: str) -> int | None:
    """How many bytes of JSON a POST to *path* may carry.

    The two routes that take a task text accept NEXUS_SPEC_MAX_CHARS characters
    of it, however the client encoded them: JSON may write any character as
    ``\\uXXXX`` (six bytes) and a character outside the basic plane as two of
    those, so the byte limit is twelve per character plus room for the rest of
    the body. The limit on the text itself is checked on the decoded text, in
    characters (``nexus_specs.check_text``); this one only bounds what is read.
    """
    if not path.startswith("/api/v1/projects"):
        return None
    if _TEXT_ROUTE.fullmatch(path):
        return max(_PROJECT_BODY_BYTES, max_spec_chars() * 12 + 64 * 1024)
    return _PROJECT_BODY_BYTES

def _int_env(name: str, default: int) -> int:
    """Read a positive integer from the environment, or *default*."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
    return value if value > 0 else default

def trusted_hop_count() -> int:
    """How many trusted proxies sit in front of this process.

    ``X-Forwarded-For`` is client-controlled on the left: a caller can prepend
    any value, and a proxy only appends. So the *n*-th address from the right,
    where *n* is the number of proxies we actually run behind, is the first one
    a trusted hop wrote. Reading the leftmost value (the naive approach) lets an
    attacker rotate the header and defeat the login rate limit entirely.

    ``TRUSTED_PROXY_HOPS`` defaults to 1, which matches a single platform load
    balancer such as Render's. Treat that default as an assumption to verify,
    not a fact: Render documents Cloudflare in front of its load balancers and
    does not publish how many ``X-Forwarded-For`` entries a deployment adds. Set
    it to 0 to ignore the header completely (a direct connection, or a
    deployment with no proxy), and raise it for each additional proxy of your
    own in front of the app.
    """
    raw = os.environ.get("TRUSTED_PROXY_HOPS", "1").strip()
    try:
        hops = int(raw)
    except ValueError:
        log.warning("TRUSTED_PROXY_HOPS=%r is not an integer; using 1", raw)
        return 1
    return max(0, hops)

def client_ip_from_values(values: list[str] | None, peer: str | None, hops: int) -> str:
    """Return the client IP, trusting only the rightmost *hops* addresses.

    *values* is every ``X-Forwarded-For`` header line, in order. A caller may
    send the header more than once, and each proxy appends its own line, so the
    lines must be joined before the list is indexed: reading only the first line
    (``headers.get``) hands the key back to the caller, who simply repeats the
    header. With ``hops=0`` the header is ignored and the socket peer is used.
    A list shorter than *hops* cannot be trusted (a proxy that should have
    appended did not), so the socket peer is used instead of a spoofable value.
    """
    peer = peer or "unknown"
    if hops <= 0 or not values:
        return peer
    joined = ",".join(v for v in values if v)
    addresses = [part.strip() for part in joined.split(",") if part.strip()]
    if len(addresses) < hops:
        log.warning(
            "X-Forwarded-For has %d address(es) but %d proxy hop(s) are "
            "configured; using the socket peer", len(addresses), hops,
        )
        return peer
    return addresses[len(addresses) - hops]

def client_ip_from(forwarded_for: str | None, peer: str | None, hops: int) -> str:
    """Single-header convenience wrapper around :func:`client_ip_from_values`."""
    return client_ip_from_values([forwarded_for] if forwarded_for else [], peer, hops)

class _LoginThrottle:
    """Cap login attempts, per client IP and globally.

    Two ceilings, because the client IP is not fully trustworthy:

    * **Per IP** caps a single source. It relies on the resolved client IP, so it
      is only as good as the proxy configuration (see ``trusted_hop_count``).
    * **Global** caps the whole endpoint regardless of the claimed IP. It is the
      backstop for the case where the IP is spoofable - a platform that
      overwrites rather than appends ``X-Forwarded-For``, or a header we
      misconfigured. It cannot be bypassed by any request header.

    In-memory on purpose: this is a backstop against online guessing, not the
    source of truth, so losing it on restart is acceptable.
    """

    def __init__(self, max_attempts: int = 30, window_seconds: float = 60.0,
                 global_max_attempts: int = 120, global_window_seconds: float = 60.0):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.global_max_attempts = global_max_attempts
        self.global_window_seconds = global_window_seconds
        self._hits: dict[str, list[float]] = {}
        self._global: list[float] = []
        self._lock = threading.Lock()

    def check(self, ip: str) -> int:
        """Record an attempt for *ip*. Return seconds to wait, or 0 if allowed."""
        now = time.time()
        cutoff = now - self.window_seconds
        g_cutoff = now - self.global_window_seconds
        with self._lock:
            self._global = [t for t in self._global if t > g_cutoff]
            if len(self._global) >= self.global_max_attempts:
                return int(self._global[0] + self.global_window_seconds - now) + 1
            hits = [t for t in self._hits.get(ip, ()) if t > cutoff]
            if len(hits) >= self.max_attempts:
                self._hits[ip] = hits
                return int(hits[0] + self.window_seconds - now) + 1
            hits.append(now)
            self._hits[ip] = hits
            self._global.append(now)
            if len(self._hits) > 10_000:  # bound memory under a spray of IPs
                self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > cutoff}
            return 0

    def reset(self, ip: str) -> None:
        with self._lock:
            self._hits.pop(ip, None)

#: Content types worth compressing: text formats whose gzipped form is much
#: smaller. Fonts (already compressed) and images are excluded.
_COMPRESSIBLE_PREFIXES = ("text/", "application/javascript", "application/json",
                          "image/svg+xml", "application/manifest+json")


def _is_compressible(content_type: str) -> bool:
    base = content_type.split(";", 1)[0].strip().lower()
    return base.startswith(_COMPRESSIBLE_PREFIXES)


def _accepts_gzip(header: str) -> bool:
    """Whether the client accepts gzip, honouring an explicit ``q=0`` refusal.

    A plain ``"gzip" in header`` is wrong twice over: it matches ``gzip;q=0``
    (the client saying *no*) and it matches the substring inside another token.
    Parsing the list is a few lines and removes both.
    """
    for part in header.split(","):
        token, _, params = part.strip().partition(";")
        if token.strip().lower() not in ("gzip", "x-gzip", "*"):
            continue
        q = 1.0
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    q = float(value.strip())
                except ValueError:
                    q = 0.0
        if q > 0:
            return True
    return False
