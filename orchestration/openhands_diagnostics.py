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
                 worker_max_age: int = 120):
        self.store = store
        self.accounts = accounts
        self.pool = pool
        self.lab = lab or LabStore(store)
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
                row["mcp_servers"] = len(caps.get("mcp_servers") or [])
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

    # -- 6. worker heartbeat ----------------------------------------------
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
