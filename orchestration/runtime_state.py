"""Durable desired runtime state and the readiness barrier.

The agent-server keeps its settings in ``~/.openhands/settings.json``, which
lives on the container overlay. A pod recreation erases it. The skill-hub
SessionStart hook re-applies the registration, but that hook is ``async`` and
runs *after* the conversation's ``agent_context`` has already been read
(``local_conversation.py`` reads it in ``_ensure_plugins_loaded`` before it
calls ``run_session_start``). A conversation created inside that window
silently loses its skills and MCP servers.

The control plane must not depend on that hook. Before it creates a
conversation it reads the desired state from the durable volume, applies it
with ``PATCH /api/settings`` (idempotent, field-merge), reads it back, and
proceeds only when the invariant actually holds. That closes the race for
every conversation the control plane creates.

The desired state is a JSON file on ``/workspace``:

    {
      "agent_context": {
        "load_public_skills": true,
        "registered_marketplaces": [
          {"name": "pavel-hub", "source": "/workspace/skills-hub",
           "auto_load": true}
        ]
      },
      "mcp_config": {"playwright": {"command": "npx", "args": ["..."],
                                    "enabled": true}}
    }

``mcp_config`` is optional. When absent, MCP is left exactly as the server has
it; the barrier never clears a server it did not set.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .openhands_client import ApiUnavailable, OpenHandsClient


#: The hub writes its desired state here; ``/workspace`` is the durable volume.
DEFAULT_DESIRED_STATE_PATH = Path(
    "/workspace/skills-hub/runtime-desired-state.json"
)


class DesiredStateError(RuntimeError):
    """The desired-state file exists but cannot be used."""


def load_desired_state(path: str | Path) -> dict[str, Any] | None:
    """Read the desired state, or None when no file is configured.

    A missing file is not an error: the barrier is then a no-op and the
    dispatcher behaves exactly as before. A *present but broken* file is an
    error, because silently ignoring it would reintroduce the race.
    """
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DesiredStateError(f"cannot read desired state {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise DesiredStateError(f"desired state must be a JSON object: {p}")
    return data


@dataclass
class ReadinessReport:
    """What the barrier did. Returned so callers and tests can assert on it."""

    ready: bool
    applied: bool = False
    reason: str | None = None
    #: Marketplace names the server confirmed after the write.
    marketplaces: list[str] = field(default_factory=list)
    load_public_skills: bool = False
    load_user_skills: bool = False
    mcp_servers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "applied": self.applied,
            "reason": self.reason,
            "marketplaces": self.marketplaces,
            "load_public_skills": self.load_public_skills,
            "load_user_skills": self.load_user_skills,
            "mcp_servers": self.mcp_servers,
        }


def _context_of(settings: dict[str, Any]) -> dict[str, Any]:
    agent_settings = settings.get("agent_settings") or {}
    context = agent_settings.get("agent_context") or {}
    return context if isinstance(context, dict) else {}


def _mcp_of(settings: dict[str, Any]) -> dict[str, Any]:
    agent_settings = settings.get("agent_settings") or {}
    servers = agent_settings.get("mcp_config") or {}
    return servers if isinstance(servers, dict) else {}


def _marketplace_names(context: dict[str, Any]) -> list[str]:
    entries = context.get("registered_marketplaces") or []
    return [str(e.get("name")) for e in entries if isinstance(e, dict)]


def _invariant_holds(
    settings: dict[str, Any], desired: dict[str, Any]
) -> tuple[bool, str | None]:
    """Check the server actually has what the desired state requires."""
    context = _context_of(settings)
    wanted_context = desired.get("agent_context") or {}

    # Every load flag the desired state turns on must be on. Checked generically
    # so a newly added flag cannot be silently forgotten here.
    for key, value in wanted_context.items():
        if key == "registered_marketplaces":
            continue
        if value and not context.get(key):
            return False, f"{key} not set"

    wanted_marketplaces = {
        str(e.get("name"))
        for e in (wanted_context.get("registered_marketplaces") or [])
        if isinstance(e, dict)
    }
    present = set(_marketplace_names(context))
    missing = wanted_marketplaces - present
    if missing:
        return False, f"marketplaces missing: {sorted(missing)}"

    wanted_mcp = desired.get("mcp_config") or {}
    if wanted_mcp:
        servers = _mcp_of(settings)
        missing_mcp = set(wanted_mcp) - set(servers)
        if missing_mcp:
            return False, f"mcp servers missing: {sorted(missing_mcp)}"

    return True, None


def _diff_for(desired: dict[str, Any]) -> dict[str, Any]:
    diff: dict[str, Any] = {}
    if desired.get("agent_context"):
        diff["agent_context"] = desired["agent_context"]
    if desired.get("mcp_config"):
        diff["mcp_config"] = desired["mcp_config"]
    return diff


class RuntimeReadiness:
    """Applies and verifies the durable desired state before dispatch."""

    def __init__(
        self,
        client: OpenHandsClient,
        desired_state_path: str | Path = DEFAULT_DESIRED_STATE_PATH,
    ):
        self.client = client
        self.desired_state_path = Path(desired_state_path)
        self._report: ReadinessReport | None = None

    def ensure(self, *, force: bool = False) -> ReadinessReport:
        """Make the runtime match the desired state, then verify it.

        Idempotent and cheap after the first success: the verified result is
        cached, so a steady-state tick costs nothing. ``force`` re-checks
        against the server, which a caller uses after a pod recreation.

        Raises ``DesiredStateError`` when the file is present but unusable;
        returns a not-ready report on a transport error so the dispatcher can
        keep the task queued instead of failing it.
        """
        if self._report is not None and self._report.ready and not force:
            # Served from cache: nothing was written on this call, so report
            # that honestly rather than echoing the original write.
            cached = self._report
            return ReadinessReport(
                ready=True,
                applied=False,
                reason=cached.reason,
                marketplaces=list(cached.marketplaces),
                load_public_skills=cached.load_public_skills,
                load_user_skills=cached.load_user_skills,
                mcp_servers=list(cached.mcp_servers),
            )

        try:
            desired = load_desired_state(self.desired_state_path)
        except DesiredStateError:
            raise

        if desired is None:
            # Nothing configured: behave as if there were no barrier at all.
            self._report = ReadinessReport(
                ready=True, reason="no desired state configured"
            )
            return self._report

        diff = _diff_for(desired)
        applied = False
        try:
            if diff:
                self.client.patch_settings({"agent_settings_diff": diff})
                applied = True
            settings = self.client.settings()
        except ApiUnavailable as exc:
            self._report = ReadinessReport(
                ready=False, applied=applied, reason=f"runtime unavailable: {exc}"
            )
            return self._report

        ok, why = _invariant_holds(settings, desired)
        context = _context_of(settings)
        report = ReadinessReport(
            ready=ok,
            applied=applied,
            reason=why,
            marketplaces=_marketplace_names(context),
            load_public_skills=bool(context.get("load_public_skills")),
            load_user_skills=bool(context.get("load_user_skills")),
            mcp_servers=sorted(_mcp_of(settings)),
        )
        # Only cache a success: a failure must be retried on the next tick.
        self._report = report if ok else None
        return report

    def invalidate(self) -> None:
        """Forget the cached result so the next ``ensure`` re-verifies."""
        self._report = None
