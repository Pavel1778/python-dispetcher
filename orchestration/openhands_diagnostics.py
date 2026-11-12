"""OpenHands diagnostics: check every integration point and say what is true.

Every check returns one of four statuses, and the status is earned by an
observation, never granted:

* ``VERIFIED``   — the check ran against the real thing and it worked.
* ``FAILED``     — the check ran and it did not work. The detail says how.
* ``BLOCKED``    — the check could not run: no credential, no resource, no
                   permission. Nothing is claimed about whether it would work.
* ``UNVERIFIED`` — the check was not run (e.g. the opt-in live probe was not
                   requested). Distinct from ``FAILED``: absence of evidence.

The distinction is the whole point. A report that says ``VERIFIED`` for
something that was never tried is a lie; so is ``FAILED`` for something that
only lacked a key.

The checks that need a live conversation (create, message, events, run a
command, read/write the workspace, finish) run only when *live* is requested
and an OpenHands credential is available. Everything else (database, schema,
queues, leases, worker heartbeat, the account pool) runs against whatever the
process is already configured with, and a fake Cloud server counts as "real" for
the connectivity and account checks — it is a real HTTP server the client speaks
to over a socket, which is what makes those checks meaningful.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .cloud_client import (
    CloudAuthError,
    CloudClient,
    CloudError,
    CloudUnavailable,
)
from .cloud_store import PostgresStore
from .openhands_pool import AccountPool, PoolError
from .nexus_lab_store import LabStore
from .nexus_openhands_accounts import OpenHandsAccountService
from .providers import ProviderCascade

VERIFIED = "VERIFIED"
FAILED = "FAILED"
BLOCKED = "BLOCKED"
UNVERIFIED = "UNVERIFIED"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail,
                "evidence": self.evidence}


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "",
            evidence: dict[str, Any] | None = None) -> Check:
        check = Check(name, status, detail, evidence or {})
        self.checks.append(check)
        return check

    def by_name(self, name: str) -> Check | None:
        return next((c for c in self.checks if c.name == name), None)

    @property
    def counts(self) -> dict[str, int]:
        out = {VERIFIED: 0, FAILED: 0, BLOCKED: 0, UNVERIFIED: 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    @property
    def ok(self) -> bool:
        """True when nothing FAILED. BLOCKED/UNVERIFIED do not make it false."""
        return all(c.status != FAILED for c in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "counts": self.counts,
            "checks": [c.as_dict() for c in self.checks],
        }


class OpenHandsDiagnostics:
    """Run the diagnostic checks and return an honest :class:`Report`."""

    def __init__(self, store: PostgresStore, *, accounts: OpenHandsAccountService,
                 pool: AccountPool, client_factory: Callable[[str, str], Any] | None = None,
                 cloud_client: Any | None = None, lab: LabStore | None = None,
                 live_client_factory: Callable[[str], Any] | None = None,
                 providers: ProviderCascade | None = None,
                 worker_max_age: int = 120):
        self.store = store
        self.accounts = accounts
        self.pool = pool
        self.lab = lab or LabStore(store)
        #: The LLM orchestrator cascade (Gemini → Groq → OpenRouter → OpenHands).
        #: Read only for the provider checks; a live probe is opt-in. Built from
        #: the environment when the caller does not supply one, so the report
        #: always describes the providers this process would actually use.
        self.providers = providers if providers is not None else ProviderCascade()
        #: Builds a per-account Cloud client (base URL + key). Injectable so the
        #: account checks can speak to the fake Cloud server in tests.
        self._client_factory = client_factory or (
            lambda base_url, api_key: CloudClient(base_url=base_url, api_key=api_key,
                                                  timeout=15.0))
        #: The process-wide client, for the account-level API check.
        self._cloud = cloud_client
        #: Builds a client that can create a *live* conversation. Default: the
        #: process transport (``build_cloud_client``), which needs a real key.
        self._live_factory = live_client_factory
        self.worker_max_age = worker_max_age
        #: MCP server names seen by :meth:`check_accounts` (per account id).
        #: The MCP check reuses this so it needs no second round trip on a
        #: non-live report; only a named account the list did not cover falls
        #: back to a real ``get_settings()`` call.
        self._mcp_by_account: dict[str, list[str]] = {}

    # ------------------------------------------------------------- helpers
    def _cloud_client(self) -> Any:
        if self._cloud is None:
            from .cloud_direct import build_cloud_client

            self._cloud = build_cloud_client(self.store)
        return self._cloud

    # --------------------------------------------------------------- checks
    def run(self, principal: str, *, live: bool = False,
            account_id: str | None = None, project_id: str | None = None) -> Report:
        report = Report()
        self.check_database(report)
        self.check_schema(report)
        self.check_accounts(report, principal)
        self.check_pool(report, principal, project_id=project_id)
        self.check_cloud_api(report)
        self.check_queues_and_leases(report, principal)
        self.check_worker(report)
        self.check_providers(report)
        self.check_provider_probe(report, live=live)
        self.check_openhands_tools(report, principal, live=live,
                                   account_id=account_id, project_id=project_id)
        self.check_mcp_configuration(report, principal, account_id=account_id,
                                     project_id=project_id)
        self.check_nexus_tools(report)
        self.check_live_conversation(report, principal, live=live,
                                     account_id=account_id, project_id=project_id)
        return report

    # -- 1. database -------------------------------------------------------
    def check_database(self, report: Report) -> None:
        try:
            self.store.dashboard_counts()
            with self.store._tx() as cur:
                cur.execute("SELECT 1 AS one")
                one = cur.fetchone()["one"]
            report.add("database", VERIFIED, "postgres round-trip ok",
                       {"probe": one})
        except Exception as e:  # noqa: BLE001 - a diagnostic must report, not raise
            report.add("database", FAILED, f"{type(e).__name__}: {e}")

    def check_schema(self, report: Report) -> None:
        try:
            missing = self.store.verify_schema()
        except Exception as e:  # noqa: BLE001
            report.add("schema", FAILED, f"{type(e).__name__}: {e}")
            return
        if missing:
            report.add("schema", FAILED, "missing tables/columns", {"missing": missing})
        else:
            report.add("schema", VERIFIED, "all required tables and columns present")

    # -- 2. accounts -------------------------------------------------------
    def check_accounts(self, report: Report, principal: str) -> None:
        try:
            accounts = self.accounts.list_accounts(principal)
        except Exception as e:  # noqa: BLE001
            report.add("accounts", FAILED, f"{type(e).__name__}: {e}")
            return
        if not accounts:
            report.add("accounts", BLOCKED,
                       "no OpenHands account is configured for this principal",
                       {"configured": 0})
            return
        rows: list[dict] = []
        worst = VERIFIED
        for account in accounts:
            state = account["state"]
            row = {"id": account["id"], "label": account["label"],
                   "state": state, "enabled": account.get("enabled", True),
                   "health": account.get("health", "unknown"),
                   "api_key_hint": account.get("api_key_hint", ""),
                   "max_concurrent_tasks": account.get("max_concurrent_tasks"),
                   "daily_conversation_limit": account.get("daily_conversation_limit"),
                   "connection_ok": account.get("connection_ok")}
            if state != "active" or not account.get("enabled", True):
                row["auth"] = "skipped (not active/enabled)"
                rows.append(row)
                continue
            try:
                key = self.accounts._decrypt(principal, account["id"])
                client = self._client_factory(account["base_url"], key)
                caps = client.account_capabilities()
                row["auth"] = "ok"
                row["llm_key_set"] = bool(caps.get("llm_api_key_set"))
                names = caps.get("mcp_servers") or []
                row["mcp_servers"] = len(names)
                # Kept so the MCP check can report the same names without a
                # second network call (a non-live report must not add one).
                self._mcp_by_account[str(account["id"])] = list(names)
            except CloudAuthError as e:
                row["auth"] = f"rejected: {e}"
                worst = FAILED
            except (CloudUnavailable, CloudError) as e:
                row["auth"] = f"unavailable: {type(e).__name__}"
                if worst != FAILED:
                    worst = BLOCKED
            rows.append(row)
        report.add("accounts", worst, f"{len(accounts)} account(s) probed",
                   {"accounts": rows})

    # -- 3. pool -----------------------------------------------------------
    def check_pool(self, report: Report, principal: str, *,
                   project_id: str | None = None) -> None:
        try:
            config = self.pool.get_config(principal)
        except Exception as e:  # noqa: BLE001
            report.add("account_pool", FAILED, f"{type(e).__name__}: {e}")
            return
        try:
            chosen = self.pool.select(principal, project_id=project_id)
        except PoolError as e:
            report.add("account_pool", BLOCKED, str(e),
                       {"mode": config["selection_mode"],
                        "fallback_order": config["fallback_order"]})
            return
        except Exception as e:  # noqa: BLE001
            report.add("account_pool", FAILED, f"{type(e).__name__}: {e}")
            return
        report.add("account_pool", VERIFIED,
                   f"mode {config['selection_mode']} selects {chosen['label']}",
                   {"mode": config["selection_mode"],
                    "fallback_order": config["fallback_order"],
                    "selected": {"id": chosen["id"], "label": chosen["label"],
                                 "health": chosen.get("health"),
                                 "active_tasks": chosen.get("active_tasks", 0)}})

    # -- 4. cloud API ------------------------------------------------------
    def check_cloud_api(self, report: Report) -> None:
        try:
            client = self._cloud_client()
        except Exception as e:  # noqa: BLE001
            report.add("cloud_api", FAILED, f"{type(e).__name__}: {e}")
            return
        try:
            ok = client.health()
        except CloudAuthError as e:
            report.add("cloud_api", FAILED, f"auth rejected: {e}")
            return
        except (CloudUnavailable, CloudError) as e:
            report.add("cloud_api", BLOCKED, f"{type(e).__name__}: {e}")
            return
        except Exception as e:  # noqa: BLE001
            report.add("cloud_api", FAILED, f"{type(e).__name__}: {e}")
            return
        status = VERIFIED if ok else FAILED
        report.add("cloud_api", status, "reachable" if ok else "health() returned False",
                   {"base_url": getattr(client, "base_url", None)})

    # -- 5. queues and leases ---------------------------------------------
    def check_queues_and_leases(self, report: Report, principal: str) -> None:
        """A real claim/release round trip on a throwaway run.

        Proves the durable queue works: a task is inserted, claimed under a
        lease, released, and claimed again. The throwaway project is deleted in
        a ``finally`` (its goals, runs and tasks cascade), so nothing is left
        behind and no existing row is touched.
        """
        import uuid

        from .nexus_projects import ProjectService

        projects = ProjectService(self.store)
        marker = uuid.uuid4().hex[:8]
        project_id = None
        try:
            project = projects.create(principal, f"diagnostics-{marker}")
            project_id = str(project["id"])
            goal, _ = projects.create_goal(principal, project_id, f"diagnostics {marker}")
            run = self.lab.start_run(principal, project_id, str(goal["id"]),
                                     max_iterations=5,
                                     plan=[{"id": "T1", "title": "probe",
                                            "description": "", "acceptance": [],
                                            "dependencies": []}],
                                     repository="", branch="")
            claimed = self.lab.claim_task(str(run["id"]), "diagnostics", lease_seconds=60)
            if claimed is None:
                raise RuntimeError("claim_task returned no task")
            self.lab.release_task(str(claimed["id"]), error="diagnostics", requeue=True)
            again = self.lab.claim_task(str(run["id"]), "diagnostics", lease_seconds=60)
            if again is None:
                raise RuntimeError("a released task could not be re-claimed")
            self.lab.release_task(str(again["id"]), error="diagnostics", requeue=True)
            report.add("queues_leases", VERIFIED,
                       "claim → release → re-claim round trip ok",
                       {"task_key": "T1"})
        except Exception as e:  # noqa: BLE001
            report.add("queues_leases", FAILED, f"{type(e).__name__}: {e}")
        finally:
            if project_id is not None:
                try:
                    with self.store._tx() as cur:
                        cur.execute("DELETE FROM nexus_projects WHERE id=%s", (project_id,))
                except Exception:  # noqa: BLE001 - cleanup is best effort
                    pass

    # -- 6. AI provider cascade -------------------------------------------
    def check_providers(self, report: Report) -> None:
        """Report the orchestrator cascade's configuration, by presence only.

        This is the *configuration* half of the provider check: which providers
        have a key, in cascade order, and whether any does. It never contacts a
        provider and never returns a key — only names and booleans. ``VERIFIED``
        here means "configured", which is exactly what this check claims; the
        real, earned check is :meth:`check_provider_probe`.

        A separate check keeps "switch the model provider" (this cascade) apart
        from "switch the OpenHands account" (the account pool) — the two are
        different mechanisms and must not be conflated.
        """
        try:
            configured = self.providers.configured()
            order = [p.name for p in self.providers.providers]
        except Exception as e:  # noqa: BLE001 - a diagnostic must report, not raise
            report.add("providers", FAILED, f"{type(e).__name__}: {e}")
            return
        evidence = {"order": order, "configured": configured}
        if configured:
            report.add("providers", VERIFIED,
                       f"configured: {', '.join(configured)} (order: {' → '.join(order)})",
                       evidence)
        else:
            # No key is not a failure of the integration: nothing could be
            # configured in this environment. BLOCKED says so without claiming
            # the providers are broken.
            report.add("providers", BLOCKED,
                       "no orchestrator provider has a key configured", evidence)

    def check_provider_probe(self, report: Report, *, live: bool) -> None:
        """Probe each configured provider for real; opt-in with ``live``.

        Skipped (``UNVERIFIED``) unless *live* is requested, exactly like the
        conversation probe: a read-only request per provider is a real call and
        must not happen on every page load. When it runs, each provider earns
        its own status from an observation:

        * ``VERIFIED``   — the endpoint answered, the key was accepted and (when
                           the provider lists models) the configured model is
                           among them;
        * ``FAILED``     — the key was refused, or the model is not available;
        * ``BLOCKED``    — a transport failure, 5xx or 429: nothing was proved;
        * ``UNVERIFIED`` — no provider has a key, so nothing was probed.

        A provider is never reported connected without this real call — the
        rule the request states explicitly ("do not declare a provider
        connected without a real check").
        """
        if not live:
            report.add("provider_probe", UNVERIFIED,
                       "not requested (pass live=true to probe the configured providers)")
            return
        try:
            results = self.providers.probe_all()
        except Exception as e:  # noqa: BLE001
            report.add("provider_probe", FAILED, f"{type(e).__name__}: {e}")
            return
        rows = []
        probed = [r for r in results if r.kind != "blocked"]
        worst = VERIFIED
        for r in results:
            row = {"provider": r.provider, "model": r.model, "kind": r.kind,
                   "detail": r.detail}
            if r.model_available is not None:
                row["model_available"] = r.model_available
            rows.append(row)
            if r.kind == "blocked":
                continue
            if r.kind in ("auth", "rejected"):
                worst = FAILED
            elif r.kind == "unavailable" and worst != FAILED:
                worst = BLOCKED
        if not probed:
            report.add("provider_probe", UNVERIFIED,
                       "no provider has a key, so nothing was probed",
                       {"providers": rows})
            return
        if worst == VERIFIED:
            detail = f"{len(probed)} provider(s) probed: " + ", ".join(
                f"{r.provider} ok" for r in probed)
        else:
            detail = "; ".join(f"{r.provider}: {r.detail}" for r in probed)
        report.add("provider_probe", worst, detail, {"providers": rows})

    # -- 8. agent tools, MCP and NEXUS tools ------------------------------
    def check_openhands_tools(self, report: Report, principal: str, *,
                              live: bool, account_id: str | None,
                              project_id: str | None) -> None:
        """Which tools the *agent inside a conversation* actually has.

        The request separates the tools the OpenHands agent can call from the
        tools NEXUS exposes, and forbids calling a tool available on faith. So
        this reads the **agent-server's own tool catalog** and reports what it
        says, then maps the request's capability groups (terminal, workspace
        file system, Git, testing, browser) onto it:

        * ``VERIFIED``   — the catalog was read and every required capability is
                           available (Git and testing are proved by the terminal,
                           which is how an agent runs ``git``/``pytest``);
        * ``FAILED``     — the catalog was read and a required capability is
                           missing;
        * ``BLOCKED``    — no catalog could be read: the transport has no
                           agent-server (the Cloud *app* transport), no account or
                           no resource. Nothing is claimed;
        * ``UNVERIFIED`` — not requested. Reading the catalog on the direct
                           transport may provision a sandbox, so it is opt-in
                           with ``live`` exactly like the conversation probe.

        MCP servers are *configured* for the account and checked separately
        (:meth:`check_mcp_configuration`); a configured MCP server is not proof
        its tools are available.
        """
        if not live:
            report.add("openhands_tools", UNVERIFIED,
                       "not requested (pass live=true to read the agent-server tool catalog)")
            return
        from .openhands_tools import (
            ToolCatalogUnsupported,
            capability_report,
            missing_capabilities,
            usable_tool_names,
        )

        try:
            account, client = self._tools_client(principal, account_id, project_id)
        except PoolError as e:
            report.add("openhands_tools", BLOCKED, str(e))
            return
        except Exception as e:  # noqa: BLE001
            report.add("openhands_tools", FAILED, f"{type(e).__name__}: {e}")
            return
        evidence: dict[str, Any] = {"account": (account or {}).get("label", "process transport")}
        try:
            catalog = client.tool_catalog()
        except ToolCatalogUnsupported as e:
            report.add("openhands_tools", BLOCKED, str(e), evidence)
            return
        except CloudAuthError as e:
            # A credential that is absent or rejected means the catalog could
            # not be read at all: BLOCKED, not FAILED. The account check is the
            # one that reports a rejected key as a failure.
            report.add("openhands_tools", BLOCKED, f"auth rejected: {e}", evidence)
            return
        except (CloudUnavailable, CloudError) as e:
            report.add("openhands_tools", BLOCKED, f"{type(e).__name__}: {e}", evidence)
            return
        except Exception as e:  # noqa: BLE001
            report.add("openhands_tools", FAILED, f"{type(e).__name__}: {e}", evidence)
            return

        tools = catalog.get("tools") or {}
        coverage = capability_report(tools)
        missing = missing_capabilities(coverage)
        evidence.update({
            "tools_usable": usable_tool_names(tools),
            "capabilities": {k: v["available"] for k, v in coverage.items()},
            "capability_evidence": coverage,
            "missing_required": missing,
        })
        if missing:
            report.add("openhands_tools", FAILED,
                       "required capabilities missing: " + ", ".join(missing), evidence)
        else:
            report.add("openhands_tools", VERIFIED,
                       f"{len(tools)} tool(s) reported; terminal, workspace, git "
                       f"and testing available", evidence)

    def check_mcp_configuration(self, report: Report, principal: str, *,
                                account_id: str | None,
                                project_id: str | None) -> None:
        """The MCP servers *configured* for the account — names, never bodies.

        This is the configuration half of the MCP question. ``VERIFIED`` here
        means the account reports MCP servers configured, which is exactly what
        this check claims; it does **not** claim a tool was exercised (only an
        observed ``ActionEvent`` proves that). A secret value inside an MCP
        server's configuration is never read: only the server *names* are
        returned.

        The names come from :meth:`check_accounts`, which already probed each
        account with ``account_capabilities()``; reusing them keeps a non-live
        report from making a second network call. A named account the list did
        not cover (a different principal, or one skipped as inactive) is read
        directly — that is the only case that adds a request.
        """
        from .openhands_tools import mcp_server_names

        try:
            account = self._pick_account(principal, account_id, project_id)
        except PoolError:
            account = None
        except Exception as e:  # noqa: BLE001
            report.add("mcp_configuration", FAILED, f"{type(e).__name__}: {e}")
            return

        if account is not None:
            cached = self._mcp_by_account.get(str(account["id"]))
            if cached is not None:
                self._report_mcp(report, account["label"], cached)
                return
            # The list did not cover this account: read it directly, once.
            try:
                client = self._live_client(principal, account)
                settings = client.get_settings()
            except CloudAuthError as e:
                report.add("mcp_configuration", BLOCKED, f"auth rejected: {e}",
                           {"account": account["label"]})
                return
            except (CloudUnavailable, CloudError) as e:
                report.add("mcp_configuration", BLOCKED, f"{type(e).__name__}: {e}",
                           {"account": account["label"]})
                return
            except Exception as e:  # noqa: BLE001
                report.add("mcp_configuration", FAILED, f"{type(e).__name__}: {e}",
                           {"account": account["label"]})
                return
            names = mcp_server_names(settings if isinstance(settings, dict) else {})
            self._report_mcp(report, account["label"], names)
            return

        # No stored account: use the client the caller already injected (the one
        # every other check uses), so a single-key deployment still gets an
        # answer. Never build a fresh client from the environment here — a
        # non-live report must not reach a host nobody configured for it.
        if self._cloud is None:
            report.add("mcp_configuration", BLOCKED,
                       "no account is configured and no process client was supplied")
            return
        try:
            settings = self._cloud.get_settings()
        except CloudAuthError as e:
            report.add("mcp_configuration", BLOCKED, f"auth rejected: {e}")
            return
        except (CloudUnavailable, CloudError) as e:
            report.add("mcp_configuration", BLOCKED, f"{type(e).__name__}: {e}")
            return
        except Exception as e:  # noqa: BLE001
            report.add("mcp_configuration", BLOCKED, f"{type(e).__name__}: {e}")
            return
        names = mcp_server_names(settings if isinstance(settings, dict) else {})
        self._report_mcp(report, "process transport", names)

    @staticmethod
    def _report_mcp(report: Report, label: str, names: list[str]) -> None:
        evidence = {"account": label, "mcp_servers": names}
        if names:
            report.add("mcp_configuration", VERIFIED,
                       f"{len(names)} MCP server(s) configured (configuration only, "
                       f"not exercised)", evidence)
        else:
            report.add("mcp_configuration", BLOCKED,
                       "no MCP server is configured for this account", evidence)

    def check_nexus_tools(self, report: Report) -> None:
        """The tools **NEXUS** exposes — separate from the agent's own tools.

        Two surfaces, both real and both read from the code that serves them:

        * ``mcp`` — NEXUS's own MCP server tools (stdio and ``POST /mcp``);
        * ``client_tools`` — the ``sr_`` bridge delivered into a conversation as
          ``ClientToolSpec`` and answered over the event stream.

        Neither is an agent-server built-in, and the agent's built-ins are not
        NEXUS tools. Reporting them together would be exactly the conflation the
        request warns against, so this check is local and names each surface.
        """
        from .openhands_tools import nexus_tool_names

        try:
            names = nexus_tool_names()
        except Exception as e:  # noqa: BLE001
            report.add("nexus_tools", FAILED, f"{type(e).__name__}: {e}")
            return
        evidence = {"mcp": names["mcp"], "client_tools": names["client_tools"]}
        report.add("nexus_tools", VERIFIED,
                   f"NEXUS exposes {len(names['mcp'])} MCP tool(s) and "
                   f"{len(names['client_tools'])} client tool(s) to agents",
                   evidence)

    def _tools_client(self, principal: str, account_id: str | None,
                      project_id: str | None) -> tuple[dict | None, Any]:
        """``(account, client)`` for the tool/MCP checks.

        Prefers the named (or pool-selected) account's execution client so the
        catalog is the one that account's agent would see; falls back to the
        process-wide transport when no account is configured, so a single-key
        deployment still gets an answer. ``account`` is ``None`` for the
        process transport.
        """
        try:
            account = self._pick_account(principal, account_id, project_id)
        except PoolError:
            if account_id is not None:
                raise
            return None, self._cloud_client()
        return account, self._live_client(principal, account)

    # -- 9. worker heartbeat ----------------------------------------------
    def check_worker(self, report: Report) -> None:
        try:
            live = self.lab.live_workers(max_age_seconds=self.worker_max_age)
            with self.store._tx() as cur:
                cur.execute("SELECT count(*) AS n FROM nexus_runs "
                            "WHERE state IN ('running','paused','waiting_for_approval','blocked')")
                active_runs = cur.fetchone()["n"]
        except Exception as e:  # noqa: BLE001
            report.add("lab_worker", FAILED, f"{type(e).__name__}: {e}")
            return
        evidence = {"live_workers": [w["worker_id"] for w in live],
                    "active_runs": active_runs,
                    "max_age_seconds": self.worker_max_age}
        if live:
            report.add("lab_worker", VERIFIED,
                       f"{len(live)} live worker(s)", evidence)
        elif active_runs:
            # Work is waiting and nobody is ticking: this is the defect the
            # heartbeat was added to expose.
            report.add("lab_worker", FAILED,
                       "active run(s) exist but no worker heartbeat is fresh",
                       evidence)
        else:
            report.add("lab_worker", UNVERIFIED,
                       "no live worker, but no active run needs one", evidence)

    # -- 7. live conversation ---------------------------------------------
    def check_live_conversation(self, report: Report, principal: str, *,
                                live: bool, account_id: str | None,
                                project_id: str | None) -> None:
        """The deep probe: create a conversation and exercise the tools.

        Skipped (``UNVERIFIED``) unless *live* is requested. With no usable
        account it is ``BLOCKED``. The probe creates exactly one conversation
        and deletes it; it never touches an existing one.
        """
        if not live:
            report.add("live_conversation", UNVERIFIED,
                       "not requested (pass live=true to run the end-to-end probe)")
            return
        try:
            account = self._pick_account(principal, account_id, project_id)
        except PoolError as e:
            report.add("live_conversation", BLOCKED, str(e))
            return
        try:
            client = self._live_client(principal, account)
        except Exception as e:  # noqa: BLE001
            report.add("live_conversation", FAILED, f"{type(e).__name__}: {e}")
            return
        self._run_live_probe(report, principal, account, client)

    def _pick_account(self, principal: str, account_id: str | None,
                      project_id: str | None) -> dict:
        if account_id is not None:
            for a in self.accounts.list_accounts(principal):
                if str(a["id"]) == str(account_id):
                    return a
            raise PoolError("named account not found")
        return self.pool.select(principal, project_id=project_id)

    def _live_client(self, principal: str, account: dict) -> Any:
        if self._live_factory is not None:
            return self._live_factory(account["id"])
        # The probe must run on the account it selected, with that account's
        # base URL, key, transport and model. Building the process-wide client
        # here would probe whatever OPENHANDS_* happens to name, not the account
        # the report is about - a live probe that passes while the account is
        # broken is worse than no probe at all.
        return self.accounts.execution_client_for_account(principal, str(account["id"]))

    def _run_live_probe(self, report: Report, principal: str, account: dict,
                        client: Any) -> None:
        """One conversation that writes a file, runs a command and finishes."""
        marker = f"NEXUS-DIAG-{int(time.time())}"
        evidence: dict[str, Any] = {"account": account["label"], "marker": marker}
        conversation_id = None
        try:
            start = client.start_conversation(
                initial_message=(
                    f"Run this shell command and reply with its output: "
                    f"echo {marker}. Then write a file named nexus_diag.txt "
                    f"containing the text {marker} and confirm. Keep it to one step."
                ),
                title=f"nexus-diagnostics:{marker}",
                tags={"nexusdiag": marker[:40]},
            )
            conversation_id = getattr(start, "app_conversation_id", None) or getattr(start, "id", None)
            evidence["conversation_id"] = conversation_id
            if not conversation_id:
                report.add("live_conversation", FAILED,
                           "conversation was created without an id", evidence)
                return
            report.add("conversation_create", VERIFIED, "conversation created", evidence)
        except (CloudAuthError, CloudUnavailable, CloudError) as e:
            # A quota/limit refusal is a missing resource, not a broken
            # integration: BLOCKED says "could not run", FAILED says "ran and
            # did not work". Keep the two apart.
            text = str(e).lower()
            resource_limited = any(tok in text for tok in
                                   ("429", "quota", "limit", "rate", "capacity"))
            status = BLOCKED if resource_limited else FAILED
            report.add("live_conversation", status,
                       f"create failed: {type(e).__name__}: {e}", evidence)
            return
        except Exception as e:  # noqa: BLE001
            report.add("live_conversation", FAILED,
                       f"create failed: {type(e).__name__}: {e}", evidence)
            return

        try:
            final_text, state = self._poll_to_finish(client, conversation_id)
            evidence["execution_state"] = state
            evidence["final_text"] = (final_text or "")[:500]
            if state == "finished":
                report.add("message_and_events", VERIFIED,
                           "message delivered; events read; conversation finished",
                           {"state": state})
                ran = marker in (final_text or "")
                report.add("run_command", VERIFIED if ran else UNVERIFIED,
                           "the agent echoed the marker" if ran
                           else "conversation finished; marker not seen in the report",
                           {"marker": marker})
            else:
                report.add("message_and_events", FAILED,
                           f"conversation ended in state {state!r}", evidence)
                report.add("run_command", FAILED, f"state {state!r}", evidence)
        except Exception as e:  # noqa: BLE001
            report.add("message_and_events", FAILED, f"{type(e).__name__}: {e}", evidence)
            report.add("run_command", BLOCKED, "not reached", evidence)
        finally:
            try:
                client.delete_conversation(conversation_id)
                evidence["deleted"] = True
            except Exception as e:  # noqa: BLE001
                evidence["deleted"] = f"cleanup failed: {type(e).__name__}"

        # Workspace read/write is only provable on a transport that can place a
        # file in the sandbox (the direct transport's agent-server file API).
        if getattr(client, "supports_files", False):
            self._probe_workspace(report, client, conversation_id, marker)
        else:
            report.add("workspace_rw", BLOCKED,
                       "this transport cannot place/read a file in the sandbox")

    def _poll_to_finish(self, client: Any, conversation_id: str,
                        attempts: int = 60, interval: float = 2.0
                        ) -> tuple[str | None, str]:
        last = "unknown"
        for _ in range(attempts):
            conv = client.get_conversation(conversation_id)
            last = conv.execution_status or "unknown"
            if last in ("finished", "error", "stuck"):
                if last == "finished":
                    try:
                        return client.final_response(conversation_id), last
                    except CloudError:
                        return "", last
                return None, last
            time.sleep(interval)
        return None, last

    def _probe_workspace(self, report: Report, client: Any, conversation_id: str,
                         marker: str) -> None:
        """Upload a file to the sandbox and read it back byte for byte."""
        import hashlib

        from .cloud_client import SandboxFile

        data = f"nexus diagnostics {marker}\n".encode()
        item = SandboxFile(name=f"nexus_diag_{marker}.txt", data=data,
                           sha256=hashlib.sha256(data).hexdigest())
        try:
            start = client.start_conversation(
                initial_message="Stand by.", title=f"nexus-diagnostics-ws:{marker}",
                files=[item])
            placed = list(getattr(start, "files", ()) or ())
            ws_id = getattr(start, "app_conversation_id", None) or getattr(start, "id", None)
            try:
                if placed and placed[0].get("sha256") == item.sha256:
                    report.add("workspace_rw", VERIFIED,
                               "file uploaded and read back with a matching SHA-256",
                               {"path": placed[0].get("path"), "bytes": placed[0].get("bytes")})
                else:
                    report.add("workspace_rw", FAILED,
                               "the sandbox did not confirm the uploaded file",
                               {"placed": placed})
            finally:
                if ws_id:
                    try:
                        client.delete_conversation(ws_id)
                    except Exception:  # noqa: BLE001 - cleanup is best effort
                        pass
        except Exception as e:  # noqa: BLE001
            report.add("workspace_rw", FAILED, f"{type(e).__name__}: {e}")
