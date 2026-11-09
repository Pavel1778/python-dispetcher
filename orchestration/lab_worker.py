"""Durable lab worker: a separate process that drives autonomous runs.

The worker is deliberately *not* a thread in the web server: a browser request
must not be what advances a run, and a web-server restart must not stop one.
Run it with::

    python -m orchestration.lab_worker --worker-id lab-1

It loops over every *active* run (from any project), ticks the scheduler for
each, and sleeps. Because all state is in PostgreSQL and claims are leases, N
workers can run at once and a worker that dies mid-task is recovered by the next
tick (``reclaim_expired``).

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
from .nexus_lab import GitVerifier, LabScheduler, OpenHandsExecutor
from .nexus_lab_store import ACTIVE_RUN_STATES, LabStore

log = logging.getLogger("lab_worker")


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
                 github: GitHubClient | None = None):
        self.store = store
        self.lab = LabStore(store)
        self.client = client if client is not None else build_cloud_client(store)
        self.github = github if github is not None else GitHubClient()
        self.executor = OpenHandsExecutor(self.client)
        self.verifier = GitVerifier(self.github)
        self.worker_id = worker_id
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self._stop = threading.Event()

    def scheduler(self) -> LabScheduler:
        return LabScheduler(self.lab, self.executor, self.verifier,
                            worker_id=self.worker_id, lease_seconds=self.lease_seconds)

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
        return summary

    def run_forever(self) -> None:
        while not self._stop.is_set():
            summary = self.tick_once()
            if summary["advanced"] or summary["errors"]:
                log.info("tick: %s", summary)
            self._stop.wait(self.poll_interval)

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
    store = PostgresStore()
    worker = LabWorker(store, worker_id=args.worker_id,
                       poll_interval=args.poll_interval, lease_seconds=args.lease_seconds)

    def _stop(signum, frame):  # graceful shutdown
        log.info("signal %s: stopping after the current pass", signum)
        worker.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        if args.once:
            log.info("tick: %s", worker.tick_once())
        else:
            log.info("lab worker %s starting", args.worker_id)
            worker.run_forever()
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
