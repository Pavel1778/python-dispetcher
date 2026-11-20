"""Durable lab worker: a separate process that drives autonomous runs.

The worker is deliberately *not* a thread in the web server: a browser request
must not be what advances a run, and a web-server restart must not stop one.
Run it with::

    python -m orchestration.lab_worker --worker-id lab-1

It loops over every *active* run (from any project), ticks the scheduler for
each, and sleeps. Because all state is in PostgreSQL and claims are leases, N
workers can run at once. A worker that dies between claiming a task and
starting its conversation is recovered when the lease expires
(``reclaim_expired``); a conversation that was started is simply followed by
whichever worker ticks next.

A deployment with one process can run the same loop inside the web server
instead: ``NEXUS_LAB_INLINE=1`` (see ``web_server.App``). Both may run at once.

The worker holds no per-run state in memory, so a restart is a non-event: it
re-reads the active runs and continues. That is the property the recovery test
asserts.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from typing import Any, Callable

from .cloud_direct import build_cloud_client
from .cloud_store import PostgresStore
from .github_client import GitHubClient
from .nexus_binding import ProjectRepositoryService
from .nexus_github_accounts import GitHubAccountService
from .nexus_lab import (
    GitVerifier, LabScheduler, OpenHandsExecutor, git_credential_for, project_github,
)
from .nexus_lab_store import ACTIVE_RUN_STATES, LabStore
from .nexus_policy import install_log_redaction
from .nexus_reports import ReportService
from .nexus_specs import SpecService

log = logging.getLogger("lab_worker")

#: How often the worker deletes OpenHands conversations of ended runs that
#: are owed a deletion, and how often it looks through the runs for
#: conversations that have become owed one. Seconds.
CLEANUP_INTERVAL_SECONDS = 30.0
CLEANUP_SWEEP_SECONDS = 600.0


def build_evidence_service(store: PostgresStore) -> Any | None:
    """The Evidence Trust Engine wired for the lab, or ``None``.

    Returns ``None`` when no signer is configured (``NEXUS_EVIDENCE_PRIVATE_KEY``
    unset), so the worker's loop stays exactly as it was: no evidence is written
    and none is faked. When a signer exists, task results are signed and can be
    re-verified with the trusted keyring.
    """
    from .evidence_trust import EvidenceTrustEngine, Keyring, signer_from_env
    from .nexus_evidence import EvidenceService

    signer = signer_from_env()
    if signer is None:
        return None
    return EvidenceService(store, EvidenceTrustEngine(Keyring.from_env(), signer))


def active_run_ids(store: PostgresStore) -> list[str]:
    """Every run the worker should service, across all projects.

    A running run is always serviced; a paused, waiting, or blocked run is
    serviced while it still has in-flight work or an abandoned conversation to
    stop, and is picked up immediately as soon as it returns to ``running``.
    """
    with store._tx() as cur:
        cur.execute(
            "SELECT r.id FROM nexus_runs r "
            "WHERE r.state = 'running' "
            "   OR (r.state = ANY(%s) AND EXISTS ("
            "         SELECT 1 FROM nexus_run_tasks t WHERE t.run_id = r.id "
            "            AND (t.state IN ('dispatched','running','verifying') "
            "                 OR (t.abandoned <> '[]'::jsonb "
            "                     AND jsonb_array_length(t.abandoned) > 0)))) "
            " OR EXISTS (SELECT 1 FROM nexus_dispatch_intents i WHERE i.run_id=r.id "
            "            AND i.state IN ('sending','uncertain','accepted','identified')) "
            " OR EXISTS (SELECT 1 FROM nexus_run_attestations_due a WHERE a.run_id=r.id) "
            " OR EXISTS (SELECT 1 FROM nexus_task_attestations_due a WHERE a.run_id=r.id) "
            "ORDER BY r.created_at",
            (list(ACTIVE_RUN_STATES - {"running"}),))
        return [str(r["id"]) for r in cur.fetchall()]


class LabWorker:
    """The loop: for each active run, tick the scheduler; sleep; repeat."""

    def __init__(self, store: PostgresStore, *, worker_id: str, poll_interval: float = 5.0,
                 lease_seconds: int = 900, client: Any | None = None,
                 github: GitHubClient | None = None,
                 github_accounts: GitHubAccountService | None = None,
                 heartbeat_seconds: int = 60, account_router: Any | None = None,
                 evidence: Any | None = None, discover_router: bool = True,
                 quality_gate: Any | None = None,
                 stopping: Callable[[], bool] | None = None):
        self.store = store
        self.lab = LabStore(store)
        self.client = client if client is not None else build_cloud_client(store)
        self.github = github if github is not None else GitHubClient()
        # The account router: when present, each task is dispatched through the
        # OpenHands account the pool policy selects, and a failed account is
        # switched out for another with a context hand-off. Without it the
        # process-wide client runs every task, as before.
        self.account_router = account_router
        #: Whether a worker that has no router asks, on every pass, whether
        #: the deployment has gained a stored connection since (see
        #: ``_find_router``). Off for a caller that wants the server's own key
        #: used whatever is stored.
        self.discover_router = discover_router
        # The EVIDENCE stage: when given, every settled task result is signed
        # into the Evidence Trust Engine (``nexus_evidence``). Built from the
        # environment by ``main`` (a signer only when the key is configured),
        # so a deployment without one keeps the loop exactly as it was.
        self.evidence = evidence
        # The QUALITY GATE: when given a ``SelfQa``, a task the Git verifier
        # accepted is then run against the project's own checks at that exact
        # commit; a failing required check reworks it. Off by default - a
        # deployment that has not opted in keeps the loop exactly as it was.
        self.quality_gate = quality_gate
        # The executor reads a task's original text from the store, to hand it
        # to the agent along with the planner's summary.
        exec_poll = min(2.0, max(0.01, float(poll_interval)))
        exec_attempts = max(150, int(round(300.0 / exec_poll)))
        self.executor = OpenHandsExecutor(
            self.client, specs=SpecService(store), router=account_router,
            client_factory=self._client_for_account if account_router else None,
            poll_interval=exec_poll, start_poll_attempts=exec_attempts)
        # A conversation is deleted with the key it was started with, whether
        # or not that connection is still given work (``OpenHandsExecutor.remove``).
        self.executor.account_client = self._client_for_account
        # A completed run's report: written with the completion, delivered to
        # the chat the run was started from.
        self.reports = ReportService(store)
        # A run that ends without completing (cancelled, failed) is reported by
        # the store itself, on the transaction that settles it - never as a
        # success.
        self.lab.reports = self.reports
        # A branch is read with the GitHub account its project is bound to, as
        # the studio reads it; the server token only where no account is bound.
        self.verifier = GitVerifier(self.github, client_for=project_github(
            store, self.github, accounts=github_accounts))
        # A task that must push needs a Git credential in its sandbox; without
        # one the push is refused and the task can never verify. The credential
        # is resolved from the same identity the repository is read with, and
        # handed over only to a transport that supports it (the direct one).
        # A deployment with no GitHub credential at all keeps the old behaviour:
        # the task runs and ends ``completed_unverified`` honestly.
        self.executor.git_secrets_for = git_credential_for(
            store, accounts=github_accounts,
            fallback_token=(os.environ.get("GITHUB_TOKEN") or ""))
        self.worker_id = worker_id
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        #: A worker whose heartbeat is older than this reads as dead in the
        #: diagnostics; it is written on register and every tick.
        self.heartbeat_seconds = heartbeat_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        #: The owner of the loop is stopping (the web server runs this worker
        #: inside its own process and has its own stop). Asked together with
        #: ``_stop`` by a start that is on its way: see ``LabScheduler``.
        self._stopping = stopping
        #: When the conversations of ended runs are next deleted, and when the
        #: runs are next looked through for conversations that have become
        #: owed a deletion (``_cleanup``). On the monotonic clock; both are due
        #: on the first pass, so a restart does not put the work off.
        self._cleanup_at = 0.0
        self._cleanup_sweep_at = 0.0

    def _client_for_account(self, account_id: str, task: dict | None = None) -> Any:
        """A client for one OpenHands account, built from its stored config.

        The transport is per account, so an account can be ``direct`` while the
        process default is ``app`` (or the reverse). The key is decrypted inside
        the account service and used immediately; it is never logged. The
        principal comes from the task's project, because accounts are stored per
        principal and the worker is not the account's owner.
        """
        from .nexus_openhands_accounts import OpenHandsAccountService

        # Which account a task may use was decided by the router; the worker
        # owns none of them, so the connection is read by its id.
        return OpenHandsAccountService(self.store).execution_client_internal(account_id)

    def scheduler(self) -> LabScheduler:
        return LabScheduler(self.lab, self.executor, self.verifier,
                            worker_id=self.worker_id, lease_seconds=self.lease_seconds,
                            router=self.account_router, reports=self.reports,
                            evidence=self.evidence, quality_gate=self.quality_gate,
                            stopping=self.stopping)

    def stopping(self) -> bool:
        """Whether this worker has been told to stop."""
        return self._stop.is_set() or bool(self._stopping is not None and self._stopping())

    def _find_router(self) -> None:
        """Start using stored connections once the deployment has any.

        ``build_account_router`` answers "none" for a deployment with no
        stored connection, and the worker then runs everything under the
        server's own key. That answer used to be final for the life of the
        process: a connection added in the studio an hour later was not used
        until a restart. It is asked again here, cheaply, on every pass.
        """
        if self.account_router is not None or not self.discover_router:
            return
        from .openhands_router import build_account_router

        try:
            router = build_account_router(self.store)
        except Exception:  # noqa: BLE001 - asked again on the next pass
            log.debug("could not look for stored OpenHands connections", exc_info=True)
            return
        if router is None:
            return
        log.info("stored OpenHands connections found: tasks are routed through them")
        self.account_router = router
        self.executor.router = router
        self.executor._client_factory = self._client_for_account

    def tick_once(self) -> dict[str, Any]:
        """One pass over every active run. Returns a small summary for logs."""
        self._find_router()
        sched = self.scheduler()
        summary: dict[str, Any] = {"runs": 0, "advanced": 0, "errors": []}
        for run_id in active_run_ids(self.store):
            if self.stopping():
                # Told to stop: the run in hand was finished, the rest are the
                # next process's. Nothing about them is in this one.
                summary["stopping"] = True
                break
            summary["runs"] += 1
            try:
                result = sched.tick(run_id)
                if result.claimed or result.verified or result.finished or result.followed:
                    summary["advanced"] += 1
            except Exception as e:  # noqa: BLE001 - one bad run must not stop the loop
                log.exception("run %s tick failed", run_id)
                summary["errors"].append(f"{run_id}: {type(e).__name__}: {e}")
        # What is owed whatever the runs are doing, and survives a restart
        # because it is rows: stop signals to conversations that must not go
        # on (a cancelled run's, among others), reports that are written and
        # not yet in their chat, and a look at executors that were written off.
        for name, step in (("stops", sched.deliver_stops),
                           ("reports", self.reports.deliver_due),
                           ("executors", self._reprobe),
                           ("cleanup", lambda: self._cleanup(sched))):
            if self.stopping():
                summary["stopping"] = True
                break                    # rows, all of it: owed by the next process too
            try:
                done = step()
                if done and any(done.values() if isinstance(done, dict) else [done]):
                    summary[name] = done
            except Exception as e:  # noqa: BLE001 - the pass goes on
                log.exception("%s step failed", name)
                summary["errors"].append(f"{name}: {type(e).__name__}: {e}")
        # The heartbeat is written even when nothing advanced, so an idle worker
        # still reads as alive (a lease alone cannot tell idle from dead).
        last_error = summary["errors"][-1] if summary["errors"] else None
        self.lab.heartbeat(self.worker_id, error=last_error)
        return summary

    def _cleanup(self, sched: LabScheduler) -> dict[str, int] | None:
        """Delete the OpenHands conversations of runs that are over, a few
        at a time (``LabScheduler.remove_conversations``).

        Not on every pass: a pass comes every few seconds and this is not
        urgent. Deletions that are owed are made every
        ``CLEANUP_INTERVAL_SECONDS``; the runs are looked through for
        conversations that have become owed every ``CLEANUP_SWEEP_SECONDS``.
        """
        now = time.monotonic()
        if now < self._cleanup_at:
            return None
        self._cleanup_at = now + CLEANUP_INTERVAL_SECONDS
        sweep = now >= self._cleanup_sweep_at
        if sweep:
            self._cleanup_sweep_at = now + CLEANUP_SWEEP_SECONDS
        done = sched.remove_conversations(sweep=sweep)
        if any(done.values()):
            log.info("OpenHands conversations of ended runs: %s", done)
        # What is still owed is not news on every pass; what was done is.
        return {key: value for key, value in done.items() if key != "pending"}

    def _reprobe(self) -> int:
        """Ask connections marked unavailable whether they answer again."""
        if self.account_router is None:
            return 0
        return len([item for item in self.account_router.reprobe() if item["healthy"]])

    def run_forever(self) -> None:
        # The loop outlives the database. A pass that fails because the store
        # is briefly unreachable (a restart, a pooler recycling its clients, a
        # network reset) is logged and retried on the next pass, exactly as the
        # web server's own loop does; before this, one transient
        # ``StoreUnavailable`` ended the process, and with it every run the
        # worker was the only one advancing. Registration is retried until it
        # sticks, and the final deregistration is best effort: a worker that
        # cannot reach the database on the way out must still exit.
        registered = False
        try:
            while not self._stop.is_set():
                if not registered:
                    try:
                        self.lab.register_worker(self.worker_id, kind="worker")
                        registered = True
                    except Exception:  # noqa: BLE001 - retried next pass
                        log.exception("could not register worker %s; will retry", self.worker_id)
                if registered:
                    try:
                        summary = self.tick_once()
                    except Exception as e:  # noqa: BLE001 - a database blip must not stop the worker
                        log.exception("lab pass failed")
                        summary = {"runs": 0, "advanced": 0,
                                   "errors": [f"{type(e).__name__}: {e}"]}
                else:
                    summary = {"runs": 0, "advanced": 0, "errors": []}
                if summary["advanced"] or summary["errors"]:
                    log.info("tick: %s", summary)
                self.wait(self.poll_interval)
        finally:
            self._stop_worker_best_effort()

    def wake(self) -> None:
        """Wake the worker loop immediately when new work is ready."""
        self._wake.set()

    def wait(self, timeout: float | None = None) -> None:
        """Wait for *timeout* seconds, or until woken or told to stop."""
        if self._stop.is_set():
            return
        self._wake.wait(timeout)
        if not self._stop.is_set():
            self._wake.clear()

    def run_once(self) -> dict[str, Any]:
        """One pass, registered so the pass is visible to the diagnostics.

        ``--once`` is the cron-style deployment (and the health check): it must
        leave the same trace a long-lived worker does - a worker row and a
        heartbeat - or the run list would look unserviced to
        ``GET /api/openhands/diagnostics`` while the cron is in fact advancing
        it. A single pass cannot stay "live", so the row is marked stopped as it
        leaves; the heartbeat it wrote is what a caller inspects immediately.
        """
        try:
            self.lab.register_worker(self.worker_id, kind="worker")
        except Exception as e:  # noqa: BLE001 - a pass that cannot register still runs
            log.warning("could not register worker %s: %s: %s",
                        self.worker_id, type(e).__name__, e)
        try:
            return self.tick_once()
        finally:
            self._stop_worker_best_effort()

    def _stop_worker_best_effort(self) -> None:
        try:
            self.lab.stop_worker(self.worker_id)
        except Exception:  # noqa: BLE001 - the process is stopping; best effort
            log.debug("could not record that worker %s stopped", self.worker_id,
                      exc_info=True)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()


def build_quality_gate(store: PostgresStore | None = None, *,
                       github: GitHubClient | None = None,
                       github_accounts: GitHubAccountService | None = None,
                       fallback_token: str | None = None) -> Any | None:
    """The Self-QA gate for this deployment.

    **Per project by default.** Each project carries its own mode
    (``nexus_projects.quality_gate``, migration 0042): ``auto`` (the default -
    run only when the repository declares ``.nexus-qa.json``), ``on`` (always)
    or ``off`` (never). So a project that declares its checks is gated without
    any deployment-wide switch, which is what the roadmap asks for; a project
    that declares nothing is not slowed down by a default run it did not ask
    for.

    ``NEXUS_SELFQA_GATE`` remains an explicit *deployment-wide override*: set
    to a true value it forces every project's checks to run (mode ``on``),
    regardless of the project's own mode; set to a false value it turns the
    gate off everywhere. This keeps an operator's existing switch meaningful
    while the per-project setting becomes the default.

    Without a *store* the gate cannot read a project's mode, so it falls back
    to the old behaviour: ``SelfQa`` only when ``NEXUS_SELFQA_GATE`` is set.
    That is the single-project caller (a test, the CLI) and is deliberately
    unchanged.

    Running a project's checks clones its repository and runs arbitrary
    commands inside it; the engine strips NEXUS secrets from the child
    environment (``nexus_selfqa._child_env``), and a Git credential for a
    private repository is resolved per project and handed to git through the
    environment only, never argv or the URL.
    """
    from .nexus_selfqa import ProjectQualityGate, SelfQa

    def _seconds(name: str, default: float) -> float:
        """A positive timeout from the environment, or the default.

        A junk or non-positive value is the default, never zero: a zero timeout
        would fail every check and call it the project's fault.
        """
        try:
            value = float(os.environ.get(name, "").strip())
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    def _engine() -> SelfQa:
        from .nexus_selfqa import DEFAULT_CHECK_TIMEOUT, DEFAULT_TOTAL_TIMEOUT
        return SelfQa(
            check_timeout=_seconds("NEXUS_SELFQA_CHECK_TIMEOUT", DEFAULT_CHECK_TIMEOUT),
            total_timeout=_seconds("NEXUS_SELFQA_TOTAL_TIMEOUT", DEFAULT_TOTAL_TIMEOUT),
        )

    raw = os.environ.get("NEXUS_SELFQA_GATE", "").strip().lower()
    forced_on = raw in ("1", "true", "yes", "on")
    forced_off = raw in ("0", "false", "no", "off")
    if store is None:
        return _engine() if forced_on else None
    if forced_off:
        return None

    accounts = github_accounts
    fallback = (fallback_token or os.environ.get("GITHUB_TOKEN", "")).strip()
    reader = github if github is not None else GitHubClient()
    links = ProjectRepositoryService(store)
    # The same per-repository identity the Git verifier uses, built once.
    identity_for = project_github(store, reader, accounts=accounts)

    def mode_for(project_id: str) -> str:
        if forced_on:
            return "on"
        return quality_gate_mode(store, project_id)

    def declares(repository: str, *, branch: str | None = None,
                 project_id: str = "") -> bool | None:
        """Read the declaration with the identity that reads the repository.

        So a private repository is read with an account that can see it, and a
        revoked one is an error rather than a silent read as somebody else.
        """
        from .nexus_selfqa import declares_checks

        client = reader
        if project_id:
            try:
                client = identity_for({"project_id": project_id,
                                       "repository": repository})
            except Exception as e:  # noqa: BLE001 - read as the server, honestly
                log.debug("no project identity to read %s: %s", repository,
                          type(e).__name__)
        return declares_checks(repository, branch=branch, client=client)

    def token_for(project_id: str) -> str:
        """A Git credential for cloning the project's repository, or ""."""
        if accounts is None or not project_id:
            return fallback
        try:
            with store._tx() as cur:
                full_name = links.primary_repository(project_id, cur=cur) or ""
                if not full_name:
                    return fallback
                principal, account_id = links.account_for_repository(
                    project_id, full_name, cur=cur)
            if account_id is None or principal is None:
                return fallback
            return accounts.resolve_token(principal, account_id) or fallback
        except Exception as e:  # noqa: BLE001 - an anonymous clone is not fatal
            log.debug("no quality-gate credential for %s: %s", project_id,
                      type(e).__name__)
            return fallback

    return ProjectQualityGate(_engine(), mode_for=mode_for, declares=declares,
                              token_for=token_for)


def quality_gate_mode(store: PostgresStore, project_id: str) -> str:
    """A project's quality-gate mode, or ``auto`` when it cannot be read.

    Read straight from the table rather than through ``ProjectService``: the
    worker is not a project member and must not be made to pretend to be one to
    read a policy that belongs to the project itself.
    """
    from .nexus_selfqa import normalise_mode

    if not project_id:
        return "auto"
    try:
        with store._tx() as cur:
            cur.execute("SELECT quality_gate FROM nexus_projects WHERE id=%s",
                        (project_id,))
            row = cur.fetchone()
    except Exception as e:  # noqa: BLE001 - an unreadable mode is the default
        log.debug("could not read the quality-gate mode of %s: %s", project_id,
                  type(e).__name__)
        return "auto"
    return normalise_mode(row["quality_gate"] if row else None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NEXUS autonomous lab worker")
    parser.add_argument("--worker-id", default=f"lab-{os.getpid()}")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--lease-seconds", type=int, default=900)
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Scrub credential-looking values from every log record (see nexus_policy).
    install_log_redaction()
    store = PostgresStore()
    # An account router when the deployment has stored OpenHands accounts, so
    # the worker uses the pool; None (the pre-existing path) when it has none.
    from .openhands_router import build_account_router
    router = build_account_router(store, principal="machine:default")
    # The GitHub identity the quality gate reads a declaration with and clones
    # with: the stored accounts, falling back to the server's own token.
    github = GitHubClient()
    github_accounts = GitHubAccountService(store)
    worker = LabWorker(store, worker_id=args.worker_id,
                       poll_interval=args.poll_interval, lease_seconds=args.lease_seconds,
                       account_router=router, evidence=build_evidence_service(store),
                       quality_gate=build_quality_gate(
                           store, github=github, github_accounts=github_accounts))
    if router is not None:
        log.info("OpenHands account pool is active")
    if worker.evidence is not None:
        log.info("Evidence Trust Engine is active: task results are signed")
    if worker.quality_gate is not None:
        log.info("Self-QA gate is active: accepted commits are checked")

    def _stop(signum, frame):  # graceful shutdown
        log.info("signal %s: stopping after the current pass", signum)
        worker.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        if args.once:
            # A single pass still registers and heartbeats, so a cron-style
            # deployment is visible to the diagnostics exactly as a long-lived
            # worker is; the row is marked stopped as the pass leaves.
            log.info("lab worker %s: one pass", args.worker_id)
            log.info("tick: %s", worker.run_once())
        else:
            log.info("lab worker %s starting", args.worker_id)
            worker.run_forever()
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
