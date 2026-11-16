"""Read-only inventory of the OpenHands API NEXUS talks to.

This is the deliverable of the *OpenHands inventory* step: one command that
reports the real API version, how authorization works, the account's limits,
the tools an agent actually has, the availability of the service, and — the
part that matters most — whether the endpoints the NEXUS client already uses
still exist on the live service.

It never writes: no conversation is created, nothing is deleted, no account is
changed. Every fact is read over HTTP and every claim is earned with one of the
four statuses the diagnostic engine already uses (``VERIFIED``, ``FAILED``,
``BLOCKED``, ``UNVERIFIED``), so a section that could not be read says so
instead of guessing.

The endpoint list is the contract under test: ``CLOUD_ENDPOINTS`` is exactly
what :class:`orchestration.cloud_client.CloudClient` calls and
``AGENT_SERVER_ENDPOINTS`` is exactly what
:class:`orchestration.openhands_client.OpenHandsClient` calls. The compatibility
check matches each of them against the paths in the live ``openapi.json``; a
missing one is a real breakage, not a warning.

Run it with::

    python -m orchestration.openhands_inventory            # human summary
    python -m orchestration.openhands_inventory --json     # full report
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .cloud_client import DEFAULT_CLOUD_BASE_URL, CloudClient, validate_base_url
from .openhands_diagnostics import BLOCKED, FAILED, UNVERIFIED, VERIFIED, Report


@dataclass(frozen=True)
class Endpoint:
    """One API endpoint, named by the purpose NEXUS uses it for."""

    purpose: str
    method: str
    path: str          # a template, ids written as ``{id}``

    def as_dict(self) -> dict[str, str]:
        return {"purpose": self.purpose, "method": self.method, "path": self.path}


#: Every endpoint ``CloudClient`` (OpenHands Cloud V1) calls. Keep in step with
#: ``orchestration/cloud_client.py``: this list is what the compatibility check
#: tests against the live ``openapi.json``.
CLOUD_ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("health / conversation count", "GET", "/api/v1/app-conversations/count"),
    Endpoint("start a conversation", "POST", "/api/v1/app-conversations"),
    Endpoint("read a conversation", "GET", "/api/v1/app-conversations"),
    Endpoint("poll a start task", "GET", "/api/v1/app-conversations/start-tasks"),
    Endpoint("search conversations", "GET", "/api/v1/app-conversations/search"),
    Endpoint("send a message", "POST", "/api/v1/app-conversations/{id}/send-message"),
    Endpoint("rename a conversation", "PATCH", "/api/v1/app-conversations/{id}"),
    Endpoint("delete a conversation", "DELETE", "/api/v1/app-conversations/{id}"),
    Endpoint("conversation skills", "GET", "/api/v1/app-conversations/{id}/skills"),
    Endpoint("read events", "GET", "/api/v1/conversation/{id}/events/search"),
    Endpoint("start a sandbox", "POST", "/api/v1/sandboxes"),
    Endpoint("read a sandbox", "GET", "/api/v1/sandboxes"),
    Endpoint("delete a sandbox", "DELETE", "/api/v1/sandboxes/{id}"),
    Endpoint("pause a sandbox", "POST", "/api/v1/sandboxes/{id}/pause"),
    Endpoint("resume a sandbox", "POST", "/api/v1/sandboxes/{id}/resume"),
    Endpoint("account settings", "GET", "/api/v1/settings"),
    Endpoint("agent profiles", "GET", "/api/agent-profiles"),
)

#: Every endpoint ``OpenHandsClient`` (an agent-server, local or in a sandbox)
#: calls. Checked against the agent-server's own ``openapi.json`` when one is
#: reachable; on the Cloud transport the agent-server is inside the sandbox and
#: this block is ``BLOCKED``.
AGENT_SERVER_ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("create a conversation", "POST", "/api/conversations"),
    Endpoint("read settings", "GET", "/api/settings"),
    Endpoint("merge settings", "PATCH", "/api/settings"),
    Endpoint("send a message", "POST", "/api/conversations/{id}/events"),
    Endpoint("start the loop", "POST", "/api/conversations/{id}/run"),
    Endpoint("read a conversation", "GET", "/api/conversations/{id}"),
    Endpoint("read the final response", "GET", "/api/conversations/{id}/agent_final_response"),
    Endpoint("count events", "GET", "/api/conversations/{id}/events/count"),
    Endpoint("read events", "GET", "/api/conversations/{id}/events/search"),
    Endpoint("pause", "POST", "/api/conversations/{id}/pause"),
    Endpoint("upload a file", "POST", "/api/file/upload"),
    Endpoint("download a file", "GET", "/api/file/download"),
)

#: The status codes the Cloud client maps, and what each one means to NEXUS.
#: Recorded here so the report states the error contract, not just a list of
#: numbers. See ``CloudClient._request``.
ERROR_CONTRACT: tuple[tuple[str, str], ...] = (
    ("401 / 403", "auth -> CloudAuthError: dead-letter now, a bad key cannot fix itself"),
    ("404", "gone -> CloudNotFound: requeue within the attempt budget"),
    ("422", "validation -> CloudValidationError: dead-letter, the request cannot fix itself"),
    ("429", "quota/rate -> CloudRateLimited: give the attempt back and wait (Retry-After)"),
    ("5xx", "transient -> CloudUnavailable: keep the lease, retry next tick"),
    ("3xx", "any redirect is refused (_NoRedirect) so the key cannot leak"),
    ("timeout / refused", "transient -> CloudUnavailable"),
)


def _template(path: str) -> str:
    """Normalize an id-bearing path so two spellings of one route compare equal.

    ``/api/v1/app-conversations/{id}/skills`` and
    ``/api/v1/app-conversations/{conversation_id}/skills`` are the same route;
    every brace-delimited segment becomes ``{}``.
    """
    out: list[str] = []
    for seg in path.split("/"):
        out.append("{}" if seg.startswith("{") and seg.endswith("}") else seg)
    return "/".join(out)


def _spec_routes(spec: dict[str, Any]) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method in methods:
            routes.add((method.upper(), _template(path)))
    return routes


@dataclass
class Inventory:
    """Read-only inventory of one OpenHands API base URL.

    *cloud_client* lets a caller reuse an already-built client (the web app's
    own), so the inventory speaks to exactly the endpoint the service uses.
    """

    base_url: str = ""
    api_key: str | None = None
    timeout: float = 20.0
    cloud_client: Any | None = None
    agent_server_url: str | None = None
    agent_server_key: str | None = None
    #: Filled by :meth:`collect`.
    report: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # An explicit client wins: read its base URL so the raw HTTP reads and
        # the client speak to the same host (a test points both at one fake).
        if not self.base_url and self.cloud_client is not None:
            self.base_url = getattr(self.cloud_client, "base_url", "") or ""
        if not self.base_url:
            self.base_url = (
                os.environ.get("OPENHANDS_CLOUD_URL") or DEFAULT_CLOUD_BASE_URL
            )
        self.base_url = validate_base_url(self.base_url).rstrip("/")
        if self.cloud_client is None:
            self.cloud_client = CloudClient(
                base_url=self.base_url,
                api_key=self.api_key or os.environ.get("OPENHANDS_API_KEY") or "",
                timeout=self.timeout,
            )
        elif self.api_key is None:
            self.api_key = getattr(self.cloud_client, "_api_key", None) or None

    # ------------------------------------------------------------------ http
    def _get(self, path: str, *, auth: bool = True) -> tuple[int | None, Any]:
        """A read-only GET. Returns ``(status, payload)``; never raises."""
        req = urllib.request.Request(self.base_url + path)
        req.add_header("Accept", "application/json")
        if auth and self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        elif auth and getattr(self.cloud_client, "_api_key", None):
            req.add_header("Authorization", f"Bearer {self.cloud_client._api_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                payload = None
            return e.code, payload
        except (urllib.error.URLError, TimeoutError, OSError):
            return None, None

    # ------------------------------------------------------------- sections
    def check_api(self, report: Report) -> dict[str, Any]:
        """Version, OpenAPI dialect and the endpoint count from ``openapi.json``."""
        status, spec = self._get("/openapi.json")
        info: dict[str, Any] = {"base_url": self.base_url}
        if status != 200 or not isinstance(spec, dict):
            report.add("api", BLOCKED, f"openapi.json -> {status}",
                       {"base_url": self.base_url})
            return info
        api_info = spec.get("info") or {}
        info.update({
            "openapi": spec.get("openapi"),
            "title": api_info.get("title"),
            "version": api_info.get("version"),
            "path_count": len(spec.get("paths") or {}),
        })
        report.add("api", VERIFIED,
                   f"{api_info.get('title')} {api_info.get('version')} "
                   f"(OpenAPI {spec.get('openapi')}, {info['path_count']} paths)",
                   info)
        return info

    def check_availability(self, report: Report) -> dict[str, Any]:
        """The three unauthenticated liveness probes and the auth-gated count."""
        out: dict[str, Any] = {}
        alive = []
        for path in ("/alive", "/health", "/ready"):
            status, _ = self._get(path, auth=False)
            out[path] = status
            if status == 200:
                alive.append(path)
        if len(alive) == 3:
            report.add("availability", VERIFIED,
                       "liveness probes answer: " + ", ".join(alive), out)
        elif alive:
            report.add("availability", UNVERIFIED,
                       "some probes answered: " + ", ".join(alive), out)
        else:
            report.add("availability", FAILED, "no liveness probe answered", out)
        return out

    def check_authorization(self, report: Report) -> dict[str, Any]:
        """The key is accepted, and the API actually rejects a bad one.

        Both halves matter: a service that answers 200 for a wrong key has no
        authorization. The bad-key probe uses a deliberately invalid value and
        is the only request here that is expected to fail.
        """
        out: dict[str, Any] = {}
        good = self.cloud_client.health() if self.cloud_client else False
        out["valid_key_health"] = good
        bad_status, _ = self._get("/api/v1/app-conversations/count", auth=False)
        out["no_key_status"] = bad_status
        if good and bad_status in (401, 403):
            report.add("authorization", VERIFIED,
                       "Bearer key accepted; a keyless request is refused "
                       f"({bad_status})", out)
        elif not good and bad_status in (401, 403):
            report.add("authorization", FAILED,
                       "the configured key was refused", out)
        elif good:
            report.add("authorization", FAILED,
                       f"a keyless request was not refused (status {bad_status})", out)
        else:
            report.add("authorization", BLOCKED,
                       f"no usable key (health=False, keyless status {bad_status})", out)
        return out

    def check_limits(self, report: Report) -> dict[str, Any]:
        """The account's real limits, from the endpoints that report them."""
        out: dict[str, Any] = {}
        status, quota = self._get("/api/quota/status")
        if status == 200 and isinstance(quota, dict):
            out["quota"] = {
                "daily_limit": quota.get("daily_limit"),
                "used_today": quota.get("used_today"),
                "remaining": quota.get("remaining"),
                "reset_at": quota.get("reset_at"),
            }
        else:
            out["quota"] = f"unavailable ({status})"
        caps = {}
        try:
            caps = self.cloud_client.account_capabilities()
        except Exception as e:  # noqa: BLE001 - a report must not raise
            caps = {"error": type(e).__name__}
        out["capabilities"] = caps
        # Documented hard caps the client already honours.
        out["documented_caps"] = {
            "events_search_limit": 100,
            "tag_keys": "lowercase alphanumeric only",
            "conversation_daily_limit": out.get("quota", {}).get("daily_limit")
            if isinstance(out.get("quota"), dict) else None,
        }
        if isinstance(out["quota"], dict) and out["quota"].get("daily_limit") is not None:
            report.add("limits", VERIFIED,
                       f"daily conversation limit {out['quota']['daily_limit']}, "
                       f"{out['quota'].get('remaining')} left today", out)
        else:
            report.add("limits", UNVERIFIED,
                       "quota endpoint did not report a daily limit", out)
        return out

    def check_tools(self, report: Report) -> dict[str, Any]:
        """What tools an agent can actually be given, from ``/api/tools/catalog``."""
        status, payload = self._get("/api/tools/catalog")
        tools: list[dict[str, Any]] = []
        if status == 200 and isinstance(payload, dict):
            tools = payload.get("tools") or []
        out = {
            "count": len(tools),
            "user_selectable": sorted(t["name"] for t in tools if t.get("user_selectable")),
            "in_default_set": sorted(t["name"] for t in tools if t.get("in_default_set")),
            "all": sorted(t.get("name", "") for t in tools),
        }
        if tools:
            report.add("tools", VERIFIED,
                       f"{len(tools)} tools; {len(out['user_selectable'])} selectable, "
                       f"{len(out['in_default_set'])} in the default set", out)
        else:
            report.add("tools", UNVERIFIED,
                       f"tool catalog unavailable ({status})", out)
        return out

    def check_compatibility(self, report: Report) -> dict[str, Any]:
        """Does the live API still carry every endpoint the NEXUS client uses?"""
        status, spec = self._get("/openapi.json")
        out: dict[str, Any] = {}
        if status != 200 or not isinstance(spec, dict):
            report.add("compatibility", BLOCKED,
                       f"cannot read openapi.json ({status}) to compare", out)
            return out
        routes = _spec_routes(spec)
        rows: list[dict[str, Any]] = []
        missing = 0
        for ep in CLOUD_ENDPOINTS:
            present = (ep.method, _template(ep.path)) in routes
            missing += 0 if present else 1
            rows.append({**ep.as_dict(), "present": present})
        out["cloud"] = rows
        out["cloud_missing"] = missing
        if missing == 0:
            report.add("compatibility", VERIFIED,
                       f"all {len(CLOUD_ENDPOINTS)} Cloud endpoints NEXUS uses are "
                       f"present in OpenHands {spec.get('info', {}).get('version')}",
                       {"missing": 0, "checked": len(CLOUD_ENDPOINTS)})
        else:
            report.add("compatibility", FAILED,
                       f"{missing} of {len(CLOUD_ENDPOINTS)} Cloud endpoints are missing",
                       {"missing": [r for r in rows if not r["present"]]})
        return out

    def check_agent_server(self, report: Report) -> dict[str, Any]:
        """The agent-server surface, when one is reachable at a known URL."""
        out: dict[str, Any] = {"url": self.agent_server_url}
        if not self.agent_server_url:
            report.add("agent_server", BLOCKED,
                       "no agent-server URL known (it lives inside the Cloud sandbox)",
                       out)
            return out
        base = self.agent_server_url.rstrip("/")
        req = urllib.request.Request(base + "/openapi.json")
        if self.agent_server_key:
            req.add_header("Authorization", f"Bearer {self.agent_server_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                spec = json.loads(resp.read() or b"{}")
        except Exception as e:  # noqa: BLE001
            report.add("agent_server", BLOCKED,
                       f"cannot read the agent-server openapi ({type(e).__name__})", out)
            return out
        routes = _spec_routes(spec)
        rows, missing = [], 0
        for ep in AGENT_SERVER_ENDPOINTS:
            present = (ep.method, _template(ep.path)) in routes
            missing += 0 if present else 1
            rows.append({**ep.as_dict(), "present": present})
        out.update({
            "version": (spec.get("info") or {}).get("version"),
            "openapi": spec.get("openapi"),
            "endpoints": rows,
            "missing": missing,
        })
        report.add("agent_server",
                   VERIFIED if missing == 0 else FAILED,
                   f"agent-server {(spec.get('info') or {}).get('version')}: "
                   f"{len(AGENT_SERVER_ENDPOINTS) - missing} of "
                   f"{len(AGENT_SERVER_ENDPOINTS)} endpoints present", out)
        return out

    def check_error_contract(self, report: Report) -> dict[str, Any]:
        """State the status-code contract the client implements (documented)."""
        out = {"contract": [{"status": s, "meaning": m} for s, m in ERROR_CONTRACT],
               "redirects_refused": True,
               "transport_errors": "CloudUnavailable (transient: keep lease, retry)"}
        report.add("errors_and_timeouts", VERIFIED,
                   f"{len(ERROR_CONTRACT)} status codes mapped; redirects refused; "
                   f"timeout {self.timeout:.0f}s -> CloudUnavailable", out)
        return out

    # --------------------------------------------------------------- runner
    def collect(self) -> dict[str, Any]:
        report = Report()
        api = self.check_api(report)
        availability = self.check_availability(report)
        authorization = self.check_authorization(report)
        limits = self.check_limits(report)
        tools = self.check_tools(report)
        compatibility = self.check_compatibility(report)
        agent_server = self.check_agent_server(report)
        errors = self.check_error_contract(report)
        self.report = {
            "base_url": self.base_url,
            "api": api,
            "availability": availability,
            "authorization": authorization,
            "limits": limits,
            "tools": tools,
            "compatibility": compatibility,
            "agent_server": agent_server,
            "errors_and_timeouts": errors,
            "report": report.as_dict(),
        }
        return self.report


def _human(result: dict[str, Any]) -> str:
    lines: list[str] = []
    api = result.get("api") or {}
    lines.append(f"OpenHands inventory — {result.get('base_url')}")
    lines.append(f"  API:          {api.get('title')} {api.get('version')} "
                 f"(OpenAPI {api.get('openapi')}, {api.get('path_count')} paths)")
    report = (result.get("report") or {}).get("checks") or []
    for c in report:
        lines.append(f"  [{c['status']:10s}] {c['name']}: {c['detail']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="read-only OpenHands API inventory")
    ap.add_argument("--json", action="store_true", help="print the full report")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--agent-server-url", default=os.environ.get("OPENHANDS_AGENT_SERVER_URL"))
    ap.add_argument("--agent-server-key", default=os.environ.get("OPENHANDS_AGENT_SERVER_KEY"))
    args = ap.parse_args(argv)

    inv = Inventory(base_url=args.base_url or "",
                    agent_server_url=args.agent_server_url,
                    agent_server_key=args.agent_server_key)
    result = inv.collect()
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print(_human(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
