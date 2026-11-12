"""The autonomous lab: dispatch, independent verification, replanning.

This module ties the durable store (``nexus_lab_store``) to real execution and
to *independent* verification. Three pieces, each replaceable so the loop can be
tested without a live OpenHands:

* :class:`OpenHandsExecutor` — starts a conversation for a task and polls it.
  It never decides the task is done; it only reports what the conversation said.
  It hands the agent the task's original text along with the planner's summary
  (``nexus_spec_delivery``), and starts nothing when that cannot be done exactly.
* :class:`GitVerifier` — reads the head of the task's branch through the
  read-only GitHub client and compares it with the commit the agent named. An
  agent's summary is an input to verification, never a substitute for it.
* :class:`LabScheduler` — one tick = reclaim → poll → verify → record →
  claim → authorize → dispatch → decide. This is the only place that moves a
  run forward, and it does so only while the run is ``running``, inside its
  budgets, and only against a repository the project is allowed to use.

The core rule the whole module enforces: a task is `completed_verified` only
when the verifier observed the required state. Otherwise it is
`completed_unverified`, `partial`, `failed` or `blocked`, and a dependent task
stays gated.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .cloud_client import (
    CloudAuthError,
    CloudError,
    CloudNotFound,
    CloudUnavailable,
    DeliveryError,
)
from .cloud_store import NotFound, PostgresStore
from .github_client import GitHubClient, GitHubError
from .nexus_binding import AuthorizationError, authorize_run_task
from .nexus_github_accounts import GitHubAccountService, SecretsError
from .nexus_lab_store import TERMINAL_RUN_STATES, UNFINISHED_TASK_STATES, LabStore
from .nexus_spec_delivery import Delivery, prepare
from .nexus_specs import SpecService
from .openhands_router import AccountRouter, NoUsableAccount

log = logging.getLogger("nexus_lab")

#: Conversation execution states that mean the agent is still working.
_RUNNING = {"idle", "running", "waiting_for_confirmation"}
#: Terminal-bad states.
_BAD = {"error", "stuck"}


@dataclass
class TaskReport:
    """What the executor observed about a task's conversation."""

    state: str                      # running | finished | error | stuck | missing
    final_text: str = ""
    conversation_id: str | None = None
    detail: str = ""

    @property
    def finished(self) -> bool:
        return self.state == "finished"


@dataclass
class Verification:
    """The verifier's verdict, with the evidence that produced it."""

    verdict: str                    # completed_verified | completed_unverified | partial | failed | blocked
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_detail(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "reason": self.reason}


class Dispatched(str):
    """A conversation id that also says how the task was handed to the agent.

    A plain ``str`` everywhere a conversation id is expected; ``delivery`` is
    the evidence the scheduler records on the task (see ``mark_running``), and
    ``account_id`` is the OpenHands account that ran it (``None`` for an
    executor that is not account-aware).
    """

    delivery: dict[str, Any] | None = None
    account_id: str | None = None

    def __new__(cls, conversation_id: str, delivery: dict[str, Any] | None = None,
                account_id: str | None = None):
        self = super().__new__(cls, conversation_id)
        self.delivery = delivery
        self.account_id = account_id
        return self


class Executor(Protocol):
    """Dispatch a task and observe it. No completion decision lives here."""

    def dispatch(self, task: dict) -> str: ...
    def poll(self, task: dict, conversation_id: str) -> TaskReport: ...


class DispatchError(CloudError):
    """A dispatch failed, and the account that failed is known.

    The scheduler uses ``account_id`` to exclude that account on the next
    attempt (failover). It carries the underlying ``CloudError`` message.
    ``permanent`` is forwarded from the wrapped error when it says another
    attempt would end the same way (see ``cloud_client.DeliveryError``), so the
    scheduler can end the task at once instead of retrying it to no effect.
    """

    def __init__(self, message: str, *, account_id: str | None, unavailable: bool,
                 permanent: bool = False):
        super().__init__(message)
        self.account_id = account_id
        self.unavailable = unavailable
        self.permanent = permanent


class Verifier(Protocol):
    def verify(self, task: dict, report: TaskReport) -> Verification: ...


# --------------------------------------------------------------------- executor


class OpenHandsExecutor:
    """Start an OpenHands conversation for a task and read its result.

    The instructions tell the agent to inspect before changing, follow repo
    instructions, run the relevant tests, and report exact commits/files. The
    executor does **not** treat the reply as proof; that is the verifier's job.

    A task is a planner's summary of part of what the user asked for. With
    *specs* the agent is also given the original: inside the message when it is
    short, as a file in the sandbox when it is long. A task that refers to a
    specification is never started without it - ``dispatch`` raises
    :class:`~orchestration.cloud_client.DeliveryError` and the scheduler puts
    the task back with that reason.
    """

    def __init__(self, client: Any, *, poll_interval: float = 2.0,
                 start_poll_attempts: int = 40, specs: SpecService | None = None,
                 router: Any | None = None, client_factory: Any | None = None):
        self.client = client
        self.poll_interval = poll_interval
        self.start_poll_attempts = start_poll_attempts
        self.specs = specs
        #: When set, the executor is account-aware: it asks the router which
        #: OpenHands account runs each task and dispatches through a client for
        #: that account (``client_factory`` builds one from its config). Without
        #: it, the process-wide ``client`` is used, exactly as before.
        self.router = router
        self._client_factory = client_factory

    @staticmethod
    def instructions(task: dict, original: str = "") -> str:
        """The agent's first message. *original* is the block that carries or
        points at the text the task was planned from."""
        acceptance = task.get("acceptance") or []
        criteria = "\n".join(f"- {c}" for c in acceptance) or "- (no explicit criteria)"
        repo = task.get("repository") or "(no repository)"
        branch = task.get("branch") or "(default branch)"
        # The run is named so that the same plan, run again, starts new
        # conversations: the direct transport derives a conversation's id from
        # its title and first message, and a sandbox is reused between runs.
        run = f"NEXUS run {task['run_id']}, task {task['task_key']}.\n" if task.get("run_id") else ""
        return (
            "You are a coding agent working inside a repository checkout.\n"
            f"Repository: {repo}\nBranch: {branch}\n{run}\n"
            f"Task: {task['title']}\n{task.get('description') or ''}\n\n"
            "Acceptance criteria:\n"
            f"{criteria}\n\n"
            + (f"{original}\n\n" if original else "") +
            "Rules:\n"
            "1. Inspect the repository before changing anything.\n"
            "2. Preserve unrelated user changes; do not revert others' work.\n"
            "3. Follow the repository's own instructions (README, AGENTS.md).\n"
            "4. Implement the acceptance criteria.\n"
            "5. Run the relevant tests and report their output.\n"
            "6. If configured to push, commit and push to the authorized branch.\n"
            "7. Finish with a short report: exact commits (SHA), files changed, "
            "tests run and their result, and any blocker. Do not claim success "
            "you did not verify."
        )

    def delivery(self, task: dict) -> Delivery | None:
        """How *task*'s original text reaches the agent, or ``None`` if the task
        was planned from none (a row older than specifications)."""
        if not (task.get("spec_ref") or {}).get("spec_id"):
            return None
        if self.specs is None:
            raise DeliveryError(
                "the task was planned from a specification, and this executor was built "
                "without access to specifications; the agent would not get the task",
                permanent=True)
        delivery = prepare(self.specs, task)
        if delivery is not None and delivery.files and not getattr(
                self.client, "supports_files", False):
            raise DeliveryError(
                f"the task's original text is {delivery.proof['chars']} characters and has to "
                "reach the agent as a file, and this OpenHands transport cannot place a file "
                "in the sandbox. Use OPENHANDS_TRANSPORT=direct", permanent=True)
        return delivery

    def _dispatch_client(self, task: dict) -> tuple[Any, str | None]:
        """The client to dispatch *task* through, and the account id it belongs to.

        Without a router this is the process-wide client and ``None`` (the
        pre-existing behaviour). With a router the account is chosen by the pool
        policy, and a failure to choose raises so the caller can decide.
        """
        if self.router is None:
            return self.client, None
        account = self.router.select_for_task(task)
        return self._client_for_account(str(account["id"]), task), str(account["id"])

    def _client_for_account(self, account_id: str, task: dict | None = None) -> Any:
        if self._client_factory is None:
            # No factory: only the default account can be served by the
            # process-wide client, which is what a single-account deployment has.
            return self.client
        return self._client_factory(account_id, task)

    def _client_for_task(self, task: dict) -> Any:
        """The client bound to the account a running task was dispatched to.

        A poll must talk to the *same* account that started the conversation: a
        different account is a different sandbox and cannot read that
        conversation. Without a router or a bound account, the process client.
        """
        account_id = task.get("openhands_account_id")
        if self.router is None or not account_id:
            return self.client
        return self._client_for_account(str(account_id), task)

    def dispatch(self, task: dict) -> str:
        client, account_id = self._dispatch_client(task)
        try:
            return self._dispatch_with(client, account_id, task)
        except DispatchError:
            raise
        except CloudError as e:
            # Attach the account that failed so the scheduler can exclude it,
            # and keep the error's own "permanent" verdict so a failure another
            # attempt would only repeat is not retried.
            raise DispatchError(f"{type(e).__name__}: {e}", account_id=account_id,
                                unavailable=isinstance(e, (CloudAuthError, CloudUnavailable)),
                                permanent=bool(getattr(e, "permanent", False))) from e

    def _dispatch_with(self, client: Any, account_id: str | None, task: dict) -> str:
        delivery = self.delivery(task)
        # A hand-off (the task was already tried on another account) continues
        # rather than restarts: the new executor is told what is done.
        initial = self._initial_message(task, delivery)
        extra: dict[str, Any] = {}
        if delivery is not None and delivery.files:
            extra["files"] = delivery.files
        start = client.start_conversation(
            initial_message=initial,
            selected_repository=task.get("repository") or None,
            selected_branch=task.get("branch") or None,
            title=f"lab:{task['task_key']}",
            tags={"nexusrun": str(task["run_id"])[:40], "nexustask": task["task_key"][:40]},
            **extra,
        )
        proof: dict[str, Any] | None = None
        if delivery is not None:
            proof = dict(delivery.proof)
            if delivery.files:
                placed = list(getattr(start, "files", ()) or ())
                if len(placed) != len(delivery.files):
                    # The transport said it started the conversation and cannot
                    # show that the files are there. Not a start to build on.
                    raise DeliveryError(
                        "the conversation was created, but the transport did not confirm "
                        "that the specification file is in the sandbox")
                proof["files"] = placed
        # Two-phase start: the conversation id appears only at READY. Wait for
        # it (bounded) so the returned id is a real conversation, not a start
        # task that a later poll would report as "missing". If the client has no
        # start-task API (e.g. a local agent-server), fall back to its id.
        conv_id = getattr(start, "app_conversation_id", None)
        if conv_id:
            return Dispatched(conv_id, proof, account_id)
        if hasattr(client, "get_start_task") and getattr(start, "id", None):
            current = start
            for _ in range(self.start_poll_attempts):
                conv_id = getattr(current, "app_conversation_id", None)
                if conv_id:
                    return Dispatched(conv_id, proof, account_id)
                if getattr(current, "failed", False):
                    raise CloudError(f"start task failed: {getattr(current, 'detail', '')}")
                time.sleep(self.poll_interval)
                nxt = client.get_start_task(current.id)
                if nxt is None:
                    raise CloudError("start task disappeared")
                current = nxt
            raise CloudError("start task not READY within the poll budget")
        return Dispatched(getattr(start, "id", ""), proof, account_id)

    def _initial_message(self, task: dict, delivery: Delivery | None) -> str:
        """The first message: the task's instructions, or a hand-off continuation.

        A task that has already been attempted on an account (``tried_accounts``
        is non-empty, or an account is already bound) is a continuation, so the
        new executor is told what is done instead of starting over.
        """
        already_tried = bool(task.get("tried_accounts")) or bool(task.get("openhands_account_id"))
        if self.router is not None and already_tried:
            context = self.router.handoff_context(task)
            base = self.router.handoff_message(context)
            if delivery is not None:
                return f"{base}\n\n{delivery.block}"
            return base
        return self.instructions(task, delivery.block if delivery else "")

    def poll(self, task: dict, conversation_id: str) -> TaskReport:
        client = self._client_for_task(task)
        try:
            conv = client.get_conversation(conversation_id)
        except CloudNotFound:
            return TaskReport(state="missing", conversation_id=conversation_id,
                              detail="conversation not found")
        status = conv.execution_status or "unknown"
        if status in _RUNNING:
            return TaskReport(state="running", conversation_id=conversation_id)
        if status in _BAD:
            return TaskReport(state=status, conversation_id=conversation_id,
                              detail=status)
        if status == "finished":
            try:
                text = client.final_response(conversation_id) or ""
            except CloudError:
                text = ""
            return TaskReport(state="finished", final_text=text,
                              conversation_id=conversation_id)
        return TaskReport(state=status, conversation_id=conversation_id)


# --------------------------------------------------------------------- verifier


def project_github(store: PostgresStore, fallback: GitHubClient, *,
                   accounts: GitHubAccountService | None = None
                   ) -> Callable[[dict], GitHubClient]:
    """``client_for(task)``: the GitHub identity a task's project reads with.

    A project that has a stored GitHub account bound is read with that account,
    exactly as the studio reads its files; a project without one is read with
    *fallback* (the server's ``GITHUB_TOKEN``). The bound account is never
    swapped for another: if it was revoked, or its token cannot be decrypted,
    the call raises and the task is ``blocked`` - not verified by other means.
    """
    service = accounts or GitHubAccountService(store)

    def client_for(task: dict) -> GitHubClient:
        with store._tx() as cur:
            cur.execute(
                "SELECT a.id, a.principal FROM nexus_projects p "
                "JOIN nexus_github_accounts a ON a.id = p.github_account_id "
                "WHERE p.id=%s", (task["project_id"],))
            row = cur.fetchone()
        if row is None:
            return fallback
        return service.client_for_account(row["principal"], str(row["id"]))

    return client_for


class GitVerifier:
    """Check a task's result against the real repository.

    A task is `completed_verified` only when the conversation finished *and*
    the head of the task's branch, read from GitHub now, is a commit the agent
    named in its report (in full, or by an abbreviation of seven characters or
    more). Everything else is something weaker: a finished conversation that
    names no commit, or a commit that is not the head, is
    `completed_unverified`; a branch that cannot be read is `blocked`.

    What this does **not** establish: that the commit is new, or that it does
    what the task asked. An agent that changed nothing and named the existing
    head is verified; so "verified" means "the report refers to the real,
    current state of the branch", and the acceptance criteria remain the
    agent's own claim.

    *client_for* picks the GitHub identity per task (see ``project_github``).
    """

    def __init__(self, github: GitHubClient, *, require_commit: bool = True,
                 client_for: Callable[[dict], GitHubClient] | None = None):
        self.github = github
        self.require_commit = require_commit
        self.client_for = client_for

    def verify(self, task: dict, report: TaskReport) -> Verification:
        if report.state == "missing":
            return Verification("failed", "conversation vanished",
                                {"state": report.state})
        if report.state in _BAD:
            return Verification("failed", f"conversation {report.state}",
                                {"state": report.state})
        if not report.finished:
            return Verification("blocked", "conversation did not finish",
                                {"state": report.state})
        repository = task.get("repository") or ""
        branch = task.get("branch") or ""
        evidence: dict[str, Any] = {"state": "finished",
                                    "final_text": report.final_text[:2000]}
        if not repository:
            # No repository to inspect: the work cannot be independently
            # verified, so it is unverified rather than verified.
            return Verification("completed_unverified",
                                "no repository bound; cannot verify remote state",
                                evidence)
        try:
            github = self.client_for(task) if self.client_for is not None else self.github
            sha = github.default_branch_sha(repository, branch or None)
        except (GitHubError, ValueError, NotFound, SecretsError) as e:
            return Verification("blocked", f"github unavailable: {type(e).__name__}",
                                {**evidence, "error": str(e)})
        evidence["remote_sha"] = sha
        evidence["branch"] = branch or "(default)"
        if not sha:
            return Verification("completed_unverified",
                                "branch has no readable commit", evidence)
        # The agent should have named the commit it produced; check it is real.
        # Any commit-like token in the report counts, and an abbreviation
        # matches the head it abbreviates: agents write "3f2a1b9", git-style.
        named = _named_shas(report.final_text)
        if named:
            match = next((token for token in named if sha.startswith(token)), None)
            evidence["named_sha"] = match or named[0]
            if match:
                return Verification("completed_verified",
                                    "remote branch is at the reported commit", evidence)
            return Verification("completed_unverified",
                                "reported commit is not the branch head", evidence)
        if not self.require_commit:
            return Verification("completed_unverified",
                                "no commit reported", evidence)
        return Verification("completed_unverified",
                            "no verifiable commit reported by the agent", evidence)


_SHA_LIKE = re.compile(r"\b[0-9a-f]{7,40}\b")


def _named_shas(text: str) -> list[str]:
    """Every 7-40 char hex token in the agent's report that could be a commit."""
    found: list[str] = []
    for match in _SHA_LIKE.finditer(text or ""):
        token = match.group(0)
        # Skip pure-digit runs (sizes, ids) that are not hex letters.
        if any(c in "abcdef" for c in token) and token not in found:
            found.append(token)
    return found


def _extract_sha(text: str) -> str | None:
    """The first commit-like token in the agent's report, if there is one."""
    found = _named_shas(text)
    return found[0] if found else None


# -------------------------------------------------------------------- scheduler


@dataclass
class TickResult:
    """What one scheduler tick did. Small and honest for the UI/logs."""

    reclaimed: int = 0
    claimed: str | None = None
    dispatched: bool = False
    verified: str | None = None
    verdict: str | None = None
    finished: bool = False
    #: The run is alive but ticking cannot move it: it is paused, blocked or
    #: waiting, and nothing of it is in flight. A person has to act first.
    idle: bool = False
    note: str = ""


class LabScheduler:
    """Drive one run forward by one step per tick.

    One tick = reclaim expired leases → poll what is in flight → verify and
    record → claim one claimable task → authorize → dispatch → decide (complete
    the run, or keep going). All state lives in PostgreSQL, so the loop can run
    in a separate worker process and survive a restart.

    What a tick may do depends on the run:

    * a **terminal** run (completed, failed, cancelled) is not touched at all;
    * a run that is alive but **not running** (paused, blocked, waiting) has the
      results of work already in flight collected - that work was started and
      paid for, and leaving it unread would lose the answer - and nothing new is
      started;
    * only a **running** run starts tasks, and only while it has iterations left
      and its deadline has not passed. The store checks that again inside the
      claim itself, so a pause or cancel is never overtaken.
    """

    def __init__(self, store: LabStore, executor: Executor, verifier: Verifier, *,
                 principal: str = "machine:default", worker_id: str = "lab",
                 lease_seconds: int = 900, router: AccountRouter | None = None):
        self.store = store
        self.executor = executor
        self.verifier = verifier
        self.principal = principal
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        #: When set, a dispatch failure is attributed to the account the task
        #: was bound to, so a failover excludes it on the next attempt.
        self.router = router

    @staticmethod
    def _failed_account(error: Exception, task: dict) -> str | None:
        """Which account a dispatch failure belongs to, if it can be told.

        A ``DispatchError`` names the account it tried. Otherwise the account the
        task was already bound to is used; a failure with no account bound is the
        process-wide client, which has no pool identity to exclude.
        """
        named = getattr(error, "account_id", None)
        if named:
            return str(named)
        account_id = task.get("openhands_account_id")
        return str(account_id) if account_id else None

    def tick(self, run_id: str) -> TickResult:
        result = TickResult()
        run = self.store.run_row(run_id)
        if run is None:
            result.finished = True
            result.note = "run not found"
            return result
        if run["state"] in TERMINAL_RUN_STATES:
            result.finished = True
            result.note = f"run is {run['state']}"
            return result
        result.reclaimed = len(self.store.reclaim_expired(run_id))

        # 1. Poll every task already dispatched, so a slow conversation advances.
        in_flight = 0
        for task in self.store.in_flight_tasks(run_id):
            conv = task.get("conversation_id")
            if not conv:
                in_flight += 1
                continue
            report = self.executor.poll(task, conv)
            if report.state == "running":
                if task["running_seconds"] <= task["timeout_seconds"]:
                    in_flight += 1
                    continue
                # The task's own time limit. The conversation is left as it is
                # (its transcript is the explanation); the task is over, and it
                # is not started a second time behind the first one's back.
                verdict = Verification(
                    "failed",
                    f"timed out after {task['timeout_seconds']} s; the conversation was left running",
                    {"state": "running", "conversation_id": conv})
            elif report.state == "missing" and self.router is not None \
                    and task.get("openhands_account_id"):
                # The account that ran this conversation can no longer see it:
                # the executor is gone. Mark the account failed so the next
                # attempt fails over to a different one, and put the task back.
                account_id = str(task["openhands_account_id"])
                self.router.note_failure(task, account_id=account_id,
                                         error="conversation missing: executor gone",
                                         unavailable=True)
                self.store.requeue_for_failover(
                    task["id"], failed_account_id=account_id,
                    error="conversation missing; executor may be gone",
                    requeue=task["attempt"] < task["max_attempts"])
                result.note = "executor gone: task requeued for failover"
                continue
            else:
                verdict = self.verifier.verify(task, report)
            self.store.record_verification(
                task["id"], verdict=verdict.verdict,
                evidence={"reason": verdict.reason, **verdict.evidence},
                detail=verdict.as_detail())
            result.verified = task["task_key"]
            result.verdict = verdict.verdict

        if run["state"] != "running":
            result.idle = in_flight == 0
            result.note = f"run is {run['state']}: nothing new is started"
            return result

        # 2. Claim and dispatch the next claimable task. The claim refuses by
        #    itself when the run stopped running or is out of budget.
        task = self.store.claim_task(run_id, self.worker_id, lease_seconds=self.lease_seconds)
        if task is not None:
            result.claimed = task["task_key"]
            try:
                # The gate every other path to a conversation goes through. It
                # ran when the run was started; the link or the allowlist may
                # have changed since.
                authorize_run_task(self.store.store, project_id=str(task["project_id"]),
                                   repository=task.get("repository") or "",
                                   branch=task.get("branch") or "")
            except AuthorizationError as e:
                reason = f"not authorized: {e}"
                self.store.record_verification(
                    task["id"], verdict="blocked", evidence={"reason": reason},
                    detail={"verdict": "blocked", "reason": reason})
                applied = self.store.set_run_state_internal(
                    run_id, "blocked",
                    last_error=f"task {task['task_key']} was not started: {e}")
                result.finished = applied is not None
                result.note = "blocked: dispatch not authorized"
                return result
            try:
                conversation_id = self.executor.dispatch(task)
            except NoUsableAccount as e:
                # The pool has nothing left for this task (every account tried
                # or unusable). That is not a transient transport error: retrying
                # cannot succeed, so the task is dead-lettered with the reason.
                self.store.record_verification(
                    task["id"], verdict="blocked", evidence={"reason": str(e)},
                    detail={"verdict": "blocked", "reason": str(e)})
                applied = self.store.set_run_state_internal(
                    run_id, "blocked",
                    last_error=f"task {task['task_key']}: no usable OpenHands account")
                result.finished = applied is not None
                result.note = "blocked: no usable account"
                return result
            except (CloudError, GitHubError) as e:
                # A dispatch failure is a provider/transport problem: record the
                # account that failed (so a failover excludes it), release the
                # task so another attempt - or another account - can try, and
                # keep the lease short so recovery is fast. A failure another
                # attempt would only repeat (``permanent``) ends the task now,
                # with its reason, instead of being retried to no effect.
                again = (task["attempt"] < task["max_attempts"]
                         and not getattr(e, "permanent", False))
                failed_account = self._failed_account(e, task)
                unavailable = getattr(e, "unavailable", isinstance(e, CloudAuthError))
                self.store.release_task(task["id"], error=f"{type(e).__name__}: {e}",
                                        requeue=again,
                                        failed_account_id=failed_account)
                if failed_account and self.router is not None:
                    self.router.note_failure(
                        task, account_id=failed_account,
                        error=f"{type(e).__name__}: {e}",
                        unavailable=unavailable)
                result.note = f"dispatch failed: {type(e).__name__}"
                return result
            account_id = getattr(conversation_id, "account_id", None)
            if account_id is None and task.get("openhands_account_id"):
                account_id = str(task["openhands_account_id"])
            # A switch between accounts is recorded as a hand-off, with the
            # context that was passed, so the history of an executor change is
            # durable and auditable. The "from" account is whatever this task
            # was previously bound to or had tried.
            previous = str(task.get("openhands_account_id") or "")
            if not previous and task.get("tried_accounts"):
                previous = str(task["tried_accounts"][-1])
            if (account_id and previous and previous != str(account_id)
                    and self.router is not None):
                context = self.router.handoff_context(task)
                self.store.record_handoff(
                    run_id=run_id, task_id=task["id"],
                    from_account_id=previous,
                    to_account_id=str(account_id),
                    reason="previous account failed", context=context)
            self.store.mark_running(task["id"], conversation_id=conversation_id or None,
                                    delivery=getattr(conversation_id, "delivery", None),
                                    account_id=account_id)
            result.dispatched = True

        # 3. Decide the run's fate.
        outcome = self._decide(run_id)
        if outcome is not None:
            state, last_error, note = outcome
            applied = self.store.set_run_state_internal(run_id, state, last_error=last_error)
            result.finished = applied is not None
            if applied is not None and note:
                result.note = result.note or note
        return result

    def _decide(self, run_id: str) -> tuple[str, str | None, str] | None:
        """The state the run should move to now, or ``None`` to keep going."""
        counts = self.store.count_tasks(run_id)
        total = sum(counts.values())
        if total == 0:
            # A run with nothing in it has not achieved its goal.
            return ("failed", "the run has no tasks", "failed: no tasks")
        if counts.get("completed_verified", 0) == total:
            return ("completed", None, "")
        unfinished = sum(counts.get(state, 0) for state in UNFINISHED_TASK_STATES)
        if unfinished == 0:
            # Nothing left to do and not all verified: the run is blocked, not
            # complete. A person resumes it (retry) or cancels it.
            return ("blocked", "no claimable tasks; some tasks not verified" + self._why(run_id),
                    "blocked: tasks remain unverified")
        if unfinished - counts.get("pending", 0) > 0:
            return None                     # work is in flight; wait for it
        spent = self.store.budget_spent(run_id)
        if spent:
            # Tasks are waiting and the run may not start them any more.
            return ("failed", spent, f"failed: {spent}")
        if not self.store.has_claimable(run_id):
            # A pending task exists but can never be claimed: a dependency ended
            # unverified/failed/blocked. That is a deadlock, not progress, so the
            # run is blocked rather than left spinning.
            return ("blocked", "dependency graph deadlocked: a dependency is not verified"
                    + self._why(run_id), "blocked: dependency not verified")
        return None

    def _why(self, run_id: str) -> str:
        """The first task that could not be started, and why, for the run's error.

        "Blocked" alone sends a person to look through the tasks. When a task
        never started - the specification could not be put in the sandbox, the
        Cloud refused - that reason is the run's reason, and it is said here.
        """
        failed = self.store.first_start_failure(run_id)
        if failed is None:
            return ""
        return f". Task {failed['task_key']} was not started: {failed['last_error']}"

    def _project_of(self, run_id: str) -> str:
        with self.store.store._tx() as cur:
            cur.execute("SELECT project_id FROM nexus_runs WHERE id=%s", (run_id,))
            row = cur.fetchone()
            return str(row["project_id"]) if row else ""

    def run_until_done(self, run_id: str, *, max_ticks: int = 500,
                       sleep: float = 0.0) -> TickResult:
        """Loop ticks until the run finishes or the tick budget is spent."""
        last = TickResult()
        for _ in range(max_ticks):
            last = self.tick(run_id)
            if last.finished or last.idle:
                return last
            if sleep:
                time.sleep(sleep)
        last.note = last.note or "tick budget exhausted"
        return last
