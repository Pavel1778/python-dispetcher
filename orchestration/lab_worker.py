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
from typing import Any

from .cloud_direct import build_cloud_client
from .cloud_store import PostgresStore
from .github_client import GitHubClient
from .nexus_github_accounts import GitHubAccountService
from .nexus_lab import GitVerifier, LabScheduler, OpenHandsExecutor, project_github
from .nexus_lab_store import ACTIVE_RUN_STATES, LabStore
from .nexus_policy import install_log_redaction
from .nexus_specs import SpecService

log = logging.getLogger("lab_worker")


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

    A run is serviced while it is active; a paused or waiting run is included so
    a resume is picked up without a restart, but the scheduler itself decides
    what is claimable.
    """
    with store._tx() as cur:
        cur.execute("SELECT id FROM nexus_runs WHERE state = ANY(%s) "
                    "ORDER BY created_at", (list(ACTIVE_RUN_STATES),))
        return [str(r["id"]) for r in cur.fetchall()]


class LabWorker:
    """The loop: for each active run, tick the scheduler; sleep; repeat."""

    def __init__(self, store: PostgresStore, *, worker_id: str, poll_interval: float = 5.0,
                 lease_seconds: int = 900, client: Any | None = None,
                 github: GitHubClient | None = None,
                 github_accounts: GitHubAccountService | None = None,
                 heartbeat_seconds: int = 60, account_router: Any | None = None,
                 evidence: Any | None = None):
        self.store = store
        self.lab = LabStore(store)
        self.client = client if client is not None else build_cloud_client(store)
        self.github = github if github is not None else GitHubClient()
        # The account router: when present, each task is dispatched through the
        # OpenHands account the pool policy selects, and a failed account is
        # switched out for another with a context hand-off. Without it the
        # process-wide client runs every task, as before.
        self.account_router = account_router
        # The EVIDENCE stage: when given, every settled task result is signed
        # into the Evidence Trust Engine (``nexus_evidence``). Built from the
        # environment by ``main`` (a signer only when the key is configured),
        # so a deployment without one keeps the loop exactly as it was.
        self.evidence = evidence
        # The executor reads a task's original text from the store, to hand it
        # to the agent along with the planner's summary.
        self.executor = OpenHandsExecutor(
            self.client, specs=SpecService(store), router=account_router,
            client_factory=self._client_for_account if account_router else None)
        # A branch is read with the GitHub account its project is bound to, as
        # the studio reads it; the server token only where no account is bound.
        self.verifier = GitVerifier(self.github, client_for=project_github(
            store, self.github, accounts=github_accounts))
        self.worker_id = worker_id
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        #: A worker whose heartbeat is older than this reads as dead in the
        #: diagnostics; it is written on register and every tick.
        self.heartbeat_seconds = heartbeat_seconds
        self._stop = threading.Event()

    def _client_for_account(self, account_id: str, task: dict | None = None) -> Any:
        """A client for one OpenHands account, built from its stored config.

        The transport is per account, so an account can be ``direct`` while the
        process default is ``app`` (or the reverse). The key is decrypted inside
        the account service and used immediately; it is never logged. The
        principal comes from the task's project, because accounts are stored per
        principal and the worker is not the account's owner.
        """
        from .nexus_openhands_accounts import OpenHandsAccountService

        service = OpenHandsAccountService(self.store)
        principal = (self.account_router.principal_for_task(task)
                     if self.account_router and task
                     else (self.account_router.principal if self.account_router
                           else "machine:default"))
        return service.execution_client_for_account(principal, account_id)

    def scheduler(self) -> LabScheduler:
        return LabScheduler(self.lab, self.executor, self.verifier,
                            worker_id=self.worker_id, lease_seconds=self.lease_seconds,
                            router=self.account_router, evidence=self.evidence)

    def tick_once(self) -> dict[str, Any]:
        """One pass over every active run. Returns a small summary for logs."""
        sched = self.scheduler()
        summary: dict[str, Any] = {"runs": 0, "advanced": 0, "errors": []}
        for run_id in active_run_ids(self.store):
            summary["runs"] += 1
            try:
                result = sched.tick(run_id)
                if result.claimed or result.verified or result.finished:
                    summary["advanced"] += 1
            except Exception as e:  # noqa: BLE001 - one bad run must not stop the loop
                log.exception("run %s tick failed", run_id)
                summary["errors"].append(f"{run_id}: {type(e).__name__}: {e}")
        # The heartbeat is written even when nothing advanced, so an idle worker
        # still reads as alive (a lease alone cannot tell idle from dead).
        last_error = summary["errors"][-1] if summary["errors"] else None
        self.lab.heartbeat(self.worker_id, error=last_error)
        return summary

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
                self._stop.wait(self.poll_interval)
        finally:
            self._stop_worker_best_effort()

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
    worker = LabWorker(store, worker_id=args.worker_id,
                       poll_interval=args.poll_interval, lease_seconds=args.lease_seconds,
                       account_router=router, evidence=build_evidence_service(store))
    if router is not None:
        log.info("OpenHands account pool is active")
    if worker.evidence is not None:
        log.info("Evidence Trust Engine is active: task results are signed")

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
