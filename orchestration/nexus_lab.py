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
    CloudRateLimited,
    CloudUnavailable,
    DeliveryError,
)
from .cloud_store import NotFound, PostgresStore
from .github_client import GitHubClient, GitHubError
from .nexus_binding import AuthorizationError, authorize_run_task
from .nexus_github_accounts import GitHubAccountService, SecretsError
from .nexus_evidence import LabEvidence
from .nexus_lab_store import TERMINAL_RUN_STATES, UNFINISHED_TASK_STATES, LabStore
from .nexus_spec_delivery import Delivery, prepare, prepare_request
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
    """

    def __init__(self, message: str, *, account_id: str | None, unavailable: bool,
                 permanent: bool = False, refused: CloudRateLimited | None = None):
        super().__init__(message)
        self.account_id = account_id
        self.unavailable = unavailable
        #: Another attempt would fail the same way (see ``DeliveryError``).
        self.permanent = permanent
        #: The 429, when that is what this was: the account answered and would
        #: not take the start. Not a failure of the account or of the task.
        self.refused = refused


def _last_account(task: dict) -> str:
    """The account *task* was last on: the one it ran on, else the last one
    that failed or refused it. Empty when it has not been anywhere yet."""
    bound = task.get("openhands_account_id")
    if bound:
        return str(bound)
    tried = task.get("tried_accounts") or []
    return str(tried[-1]) if tried else ""


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
    short, as a file in the sandbox when it is long, and part by part where the
    transport cannot place a file (``nexus_spec_delivery``). A task that refers
    to a specification is never started without it - ``dispatch`` raises
    :class:`~orchestration.cloud_client.DeliveryError` and the scheduler puts
    the task back with that reason.

    A conversation that is created in two steps (the ``app`` transport: a start
    task, then the conversation) is waited for here, up to
    ``start_poll_attempts * poll_interval`` seconds - five minutes. On the live
    service a start took 33 and 49 seconds when it was measured (2026-11-09).
    Giving up early is not cheap: the start task goes on to become a
    conversation nobody follows, and the task is started again beside it.
    """

    def __init__(self, client: Any, *, poll_interval: float = 2.0,
                 start_poll_attempts: int = 150, specs: SpecService | None = None,
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
            "4. Implement the acceptance criteria: read and change the files that are "
            "needed, use Git, and install only the dependencies the repository already "
            "allows (its lock file or manifest) - nothing else.\n"
            "5. Run the relevant tests and report their output. If a test fails, read "
            "the failure, find the cause and fix it, then run the tests again.\n"
            + OpenHandsExecutor._hand_in(task) +
            "7. Finish with a short report: the full SHA of the branch head after your "
            "push, files changed, tests run and their result, and any blocker. Do not "
            "claim success you did not verify."
        )

    @staticmethod
    def _hand_in(task: dict) -> str:
        """Rule 6: where the work has to end up for it to count.

        ``GitVerifier`` accepts a task only when the head of the task's branch
        on GitHub is a commit the report names. The rule used to read "if
        configured to push, commit and push": an agent that judged it was not
        configured committed in its sandbox, reported that honestly, and its
        task could never verify - nor could anything that depended on it.
        """
        if not task.get("repository"):
            return "6. Commit your work.\n"
        branch = (f"branch {task['branch']}" if task.get("branch")
                  else "the default branch")
        return (
            f"6. Commit your work and push it to {branch} of {task['repository']} - the "
            "branch this task is authorized for. If the branch has moved, pull and push "
            "again; never force-push. Your result is checked on GitHub, not in your "
            "sandbox: it is accepted only when the head of that branch is a commit your "
            "report names. Work that is committed and not pushed does not count. If "
            "nothing had to change, name the current head. If the push is refused, say "
            "so and quote the error.\n")

    def delivery(self, task: dict, client: Any | None = None) -> Delivery | None:
        """How *task*'s original text reaches the agent, or ``None`` when there
        is none to give. *client* is the one the task is dispatched through:
        whether a file can be placed depends on its transport, and with an
        account pool that is the account's, not the process's."""
        if not (task.get("spec_ref") or {}).get("spec_id"):
            # A run planned before specifications existed: its goal holds the
            # request, and the agent is given that.
            return prepare_request(self.specs, task) if self.specs is not None else None
        if self.specs is None:
            raise DeliveryError(
                "the task was planned from a specification, and this executor was built "
                "without access to specifications; the agent would not get the task",
                permanent=True)
        return prepare(self.specs, task, files=bool(getattr(
            client if client is not None else self.client, "supports_files", False)))

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

    def stop(self, account_id: str | None, conversation_id: str,
             project_id: str | None = None) -> bool:
        """Stop an abandoned conversation for real, on the account that owns it.

        Called when a switch or a failover left a conversation behind: it may
        still be running in its sandbox, editing the task's checkout, and it must
        not go on changing the working copy beside the new executor. The client's
        ``stop_conversation`` pauses the agent-server's run loop and keeps the
        transcript.

        Returns whether the caller may treat the conversation as stopped. On a
        transport that cannot stop a conversation at all
        (``supports_conversation_stop`` is ``False``: the Cloud V1
        app-conversation surface pauses only a whole sandbox) there is nothing to
        wait for, so this returns ``True`` - holding the branch forever for a
        stop that can never happen would strand the task. Where a stop is
        possible, ``False`` (the pause failed) leaves the entry in the task's
        ``abandoned`` list so the branch stays held and the caller retries.
        """
        if self.router is not None and account_id:
            client = self._client_for_account(
                str(account_id), {"project_id": project_id} if project_id else None)
        else:
            client = self.client
        if not getattr(client, "supports_conversation_stop", False):
            # No conversation-scoped stop on this transport; nothing was stopped
            # and nothing can be. The branch lock is the guarantee, as before.
            return True
        stop = getattr(client, "stop_conversation", None)
        if not callable(stop):
            return True
        try:
            return bool(stop(conversation_id))
        except CloudError:
            return False

    def dispatch(self, task: dict) -> str:
        client, account_id = self._dispatch_client(task)
        try:
            if (self.router is not None and account_id is not None
                    and self.router.should_verify_authorization(task, account_id)):
                # Step 6 of the failover sequence: confirm the account we are
                # switching to actually accepts its key before starting work on
                # it. A rejected key raises CloudAuthError here and is turned
                # into a DispatchError naming this account, so the scheduler
                # excludes it and fails over again - rather than starting a
                # conversation that would only fail inside the sandbox.
                self.router.ensure_authorized(task, account_id, client)
            return self._dispatch_with(client, account_id, task)
        except DispatchError:
            raise
        except CloudError as e:
            if account_id is None:
                # No pool, no account to hold the failure against: the error
                # goes up as it is, and its kind decides what happens next (a
                # refusal waits, a permanent failure is not tried again).
                raise
            # Attach the account that failed so the scheduler can exclude it.
            # A refusal (429) is an account at work saying "not now": it is
            # passed on as what it is, and the account is not marked as down.
            refused = e if isinstance(e, CloudRateLimited) else None
            raise DispatchError(
                f"{type(e).__name__}: {e}", account_id=account_id,
                unavailable=(refused is None
                             and isinstance(e, (CloudAuthError, CloudUnavailable))),
                permanent=bool(getattr(e, "permanent", False)), refused=refused) from e

    def _dispatch_with(self, client: Any, account_id: str | None, task: dict) -> str:
        delivery = self.delivery(task, client)
        # A hand-off (the task was already tried on another account) continues
        # rather than restarts: the new executor is told what is done.
        initial = self._initial_message(task, delivery, account_id)
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

    def _initial_message(self, task: dict, delivery: Delivery | None,
                         account_id: str | None = None) -> str:
        """The first message: the task's instructions, or a hand-off continuation.

        A start on a different account from the one the task was last on is a
        continuation: the new executor is told what is done instead of starting
        over. A start on the same account - another attempt after a result that
        did not verify - is an ordinary start with the task's instructions;
        nothing was handed over, and "the previous account is no longer
        available" would not be true.
        """
        previous = _last_account(task)
        if self.router is not None and previous and previous != str(account_id or ""):
            context = self.router.handoff_context(task)
            # The continuation is held to the same rule as a first start: the
            # work counts when it is on the task's branch on GitHub.
            base = self.router.handoff_message(context) + "\n" + self._hand_in(task).rstrip("\n")
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
        status = conv.execution_status
        if not status or status in _RUNNING:
            # No status is not a verdict. The service leaves the field empty
            # while it cannot ask the sandbox (it is still starting, or cannot
            # be reached for a moment); settling the task on that would end it
            # seconds after it began. It is asked again on the next pass, and
            # the task's own time limit bounds the wait - the rule the workflow
            # dispatcher has always applied (``CloudDispatcher._outcome``).
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


def refused_start_wait(error: CloudRateLimited) -> float:
    """Seconds to wait before asking again after a 429.

    What the service asked for (``Retry-After``), kept between half a minute
    and an hour. Where it named a daily limit - nothing will change for hours -
    a quarter of an hour, so the task starts soon after the limit is lifted
    without asking every minute until then. Otherwise a minute.
    """
    if error.retry_after is not None:
        return min(3600.0, max(30.0, error.retry_after))
    if "daily" in (error.code or "").lower() or "daily" in str(error).lower():
        return 900.0
    return 60.0


def _span(seconds: float) -> str:
    return f"{round(seconds / 60)} min" if seconds >= 90 else f"{round(seconds)} s"


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
                 lease_seconds: int = 900, router: AccountRouter | None = None,
                 start_wait: Callable[[CloudRateLimited], float] | None = None,
                 evidence: Any | None = None, acceptance: Any | None = None):
        self.store = store
        self.executor = executor
        self.verifier = verifier
        self.principal = principal
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        #: How long a task waits after the service refused to start it.
        self.start_wait = start_wait or refused_start_wait
        #: When set, a dispatch failure is attributed to the account the task
        #: was bound to, so a failover excludes it on the next attempt.
        self.router = router
        #: The EVIDENCE stage. When given an
        #: :class:`~orchestration.nexus_evidence.EvidenceService`, every settled
        #: task result is also signed into the Evidence Trust Engine, so a run's
        #: outcome is independently re-verifiable and not only a task column.
        #: ``None`` (a deployment with no evidence key) leaves the loop exactly
        #: as it was - the record is skipped, never faked.
        self.evidence = evidence
        #: The ACCEPTANCE stage: the run's terminal decision is signed into the
        #: same Trust Engine over the tasks' own verdicts (``LabEvidence``), so a
        #: run's outcome is an attested ACCEPTED/REJECTED, not the scheduler's
        #: word. Built on demand, so the loop always attests what it decided; it
        #: records nothing without a signer.
        self.acceptance = acceptance if acceptance is not None else LabEvidence(
            store.store, principal=principal)

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

    def _record_evidence(self, task: dict, verdict: Verification, report: TaskReport) -> None:
        """EVIDENCE stage: sign a settled task result into the Trust Engine.

        Best effort and never fatal. The task's own verdict is already written;
        this only *adds* a signed, re-verifiable record. A deployment without an
        evidence key (``evidence is None``) skips it, and a signing failure is
        logged, not raised - a broken key must not stall a run, and must not be
        mistaken for a task failure. The record is never fabricated: when there
        is no signer, nothing is stored.
        """
        if self.evidence is None:
            return
        try:
            self.evidence.record_task_result(task, verdict, report)
        except Exception as e:  # noqa: BLE001 - evidence must not stop the loop
            log.warning("could not record evidence for task %s: %s",
                        task.get("task_key"), type(e).__name__)

    def _start_refused(self, task: dict, error: Exception, refused: CloudRateLimited) -> None:
        """The service is working and will not take this start now (429).

        Nothing was started, so nothing is spent: the task goes back with the
        attempt it had, and the run with its iteration. Without an account pool
        the task waits and is claimed again. With one, the account that refused
        is set aside for this task and another takes it at once; when there is
        no other left, the task waits and is then offered to the whole pool
        again. The account is not written off in either case - it answered - so
        other work still goes to it.
        """
        why = f"{type(refused).__name__}: {refused}"
        account = self._failed_account(error, task) if self.router is not None else None
        elsewhere = False
        if account:
            self.router.pool.record_failure(self.router.principal_for_task(task), account,
                                            why, unavailable=False)
            try:
                self.router.select_for_task(
                    {**task, "tried_accounts": [*(task.get("tried_accounts") or []), account]})
                elsewhere = True
            except NoUsableAccount:
                pass
        if elsewhere:
            self.store.postpone_task(
                task["id"], seconds=0.0, failed_account_id=account,
                error=("OpenHands did not take the start on this account; another account "
                       f"takes the task (this is not counted as an attempt). {why}"),
                lease_token=task.get("lease_token"))
            return
        wait = self.start_wait(refused)
        self.store.postpone_task(
            task["id"], seconds=wait, forget_tried=account is not None,
            error=(f"OpenHands did not take the start; trying again in {_span(wait)} "
                   f"(this is not counted as an attempt). {why}"),
            lease_token=task.get("lease_token"))

    def _stop_abandoned(self, run_id: str) -> None:
        """Stop every conversation a switch or failover left behind.

        A switch records the old conversation in the task's ``abandoned`` list.
        Until each is stopped, ``claim_task`` holds the task's branch, so a new
        executor cannot start beside the old writer. This stops them one by one
        and drops each from the list only once the executor reports it stopped;
        a transport that cannot stop a conversation reports success at once (so
        the branch is not held forever), a failure leaves the entry for the next
        tick and keeps the branch held.
        """
        stop = getattr(self.executor, "stop", None)
        for entry in self.store.pending_stops(run_id):
            conversation_id = entry["conversation_id"]
            if not callable(stop):
                # An executor that cannot stop a conversation (a test double, a
                # transport with no stop) - drop the entry so the branch is not
                # held by something that can never be stopped.
                self.store.mark_abandoned_stopped(entry["task_id"], conversation_id)
                continue
            try:
                stopped = stop(entry.get("account_id"), conversation_id,
                               entry.get("project_id"))
            except Exception as e:  # noqa: BLE001 - one bad stop must not stop the tick
                log.warning("could not stop abandoned conversation %s: %s",
                            conversation_id, type(e).__name__)
                continue
            if stopped:
                self.store.mark_abandoned_stopped(entry["task_id"], conversation_id)
            else:
                log.info("abandoned conversation %s could not be stopped; branch stays held",
                         conversation_id)

    def tick(self, run_id: str) -> TickResult:
        result = TickResult()
        run = self.store.run_row(run_id)
        if run is None:
            result.finished = True
            result.note = "run not found"
            return result
        if run["state"] in TERMINAL_RUN_STATES:
            # A run an operator ended (cancelled) or one that failed is already
            # terminal, so the loop stops touching it - but its ACCEPTANCE
            # decision must still be attested, and this is the pass that sees
            # it. Idempotent, so a settled run costs one read and no write.
            self._record_run_acceptance(run_id, run["state"], run.get("last_error"))
            result.finished = True
            result.note = f"run is {run['state']}"
            return result
        result.reclaimed = len(self.store.reclaim_expired(run_id))

        # 0. Stop every conversation a switch or failover left behind, so a task
        #    cannot have two writers on its branch at once. The branch stays held
        #    (``_BRANCH_FREE``) until this succeeds.
        self._stop_abandoned(run_id)

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
                    requeue=task["attempt"] < task["max_attempts"],
                    lease_token=task["lease_token"])
                result.note = "executor gone: task requeued for failover"
                continue
            else:
                verdict = self.verifier.verify(task, report)
            # The account produced a terminal result, so it is working: mark it
            # healthy again. Without this an account excluded by one transient
            # failure would never come back into the pool.
            if self.router is not None and report.state in ("finished", "error", "stuck"):
                self.router.note_success(task)
            self.store.record_verification(
                task["id"], verdict=verdict.verdict,
                evidence={"reason": verdict.reason, **verdict.evidence},
                detail=verdict.as_detail(), lease_token=task["lease_token"])
            self._record_evidence(task, verdict, report)
            result.verified = task["task_key"]
            result.verdict = verdict.verdict

        if run["state"] != "running":
            result.idle = in_flight == 0
            result.note = f"run is {run['state']}: nothing new is started"
            return result

        # 2. Claim and dispatch the next claimable task. The claim refuses by
        #    itself when the run stopped running or is out of budget. Any
        #    conversation the poll above just abandoned is stopped first, so a
        #    failover in this same tick is not held back by the branch lock.
        self._stop_abandoned(run_id)
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
                    detail={"verdict": "blocked", "reason": reason},
                    lease_token=task["lease_token"])
                self._record_evidence(task, Verification("blocked", reason),
                                      TaskReport(state="blocked"))
                applied = self.store.set_run_state_internal(
                    run_id, "blocked",
                    last_error=f"task {task['task_key']} was not started: {e}")
                result.finished = applied is not None
                result.note = "blocked: dispatch not authorized"
                return result
            if not self.store.holds_lease(task["id"], task["lease_token"]):
                # The claim was taken away between the claim and now (the lease
                # expired and another worker reclaimed the task, or an operator
                # switched it). Starting a conversation here would be a second
                # executor on the same work; the new holder starts it instead.
                result.note = "claim lost before dispatch; not started"
                return result
            try:
                conversation_id = self.executor.dispatch(task)
            except NoUsableAccount as e:
                # The pool has nothing left for this task (every account tried
                # or unusable). That is not a transient transport error: retrying
                # cannot succeed, so the task is dead-lettered with the reason.
                self.store.record_verification(
                    task["id"], verdict="blocked", evidence={"reason": str(e)},
                    detail={"verdict": "blocked", "reason": str(e)},
                    lease_token=task["lease_token"])
                self._record_evidence(task, Verification("blocked", str(e)),
                                      TaskReport(state="blocked"))
                applied = self.store.set_run_state_internal(
                    run_id, "blocked",
                    last_error=f"task {task['task_key']}: no usable OpenHands account")
                result.finished = applied is not None
                result.note = "blocked: no usable account"
                return result
            except (CloudError, GitHubError) as e:
                refused = e if isinstance(e, CloudRateLimited) else getattr(e, "refused", None)
                if refused is not None:
                    self._start_refused(task, e, refused)
                    result.note = "start refused: rate limited"
                    return result
                # A dispatch failure is a provider/transport problem: record the
                # account that failed (so a failover excludes it), release the
                # task so another attempt - or another account - can try, and
                # keep the lease short so recovery is fast. A failure that
                # another attempt would only repeat ends the task now, with its
                # reason.
                failed_account = self._failed_account(e, task)
                unavailable = getattr(e, "unavailable", isinstance(e, CloudAuthError))
                again = (task["attempt"] < task["max_attempts"]
                         and not getattr(e, "permanent", False))
                self.store.release_task(task["id"], error=f"{type(e).__name__}: {e}",
                                        requeue=again, failed_account_id=failed_account,
                                        lease_token=task["lease_token"])
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
            previous = _last_account(task)
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
                                    account_id=account_id,
                                    lease_token=task["lease_token"])
            result.dispatched = True

        # 3. Decide the run's fate.
        outcome = self._decide(run_id)
        if outcome is not None:
            state, last_error, note = outcome
            applied = self.store.set_run_state_internal(run_id, state, last_error=last_error)
            result.finished = applied is not None
            if applied is not None and note:
                result.note = result.note or note
            if applied is not None and state in TERMINAL_RUN_STATES:
                # ACCEPTANCE: the run's terminal decision is attested over the
                # tasks' own verdicts - ACCEPTED only when every task verified,
                # otherwise REJECTED/NOT_READY - never asserted by the scheduler.
                self._record_run_acceptance(run_id, state, last_error)
        return result

    # ------------------------------------------------- evidence / acceptance
    def _record_run_acceptance(self, run_id: str, state: str,
                               last_error: str | None) -> None:
        """Attest the run's terminal decision. Best effort; never fatal.

        The task-level EVIDENCE records are written by ``_record_evidence``
        (one per settled verdict). This is the stage after them: the run's own
        ACCEPTANCE decision, earned from those verdicts rather than asserted.
        """
        try:
            run = self.store.run_row(run_id)
            if run is None:
                return
            tasks = self.store.list_tasks_internal(run_id)
            self.acceptance.accept_run(run_id=run_id, project_id=str(run["project_id"]),
                                       state=state, tasks=tasks, last_error=last_error)
        except Exception:  # noqa: BLE001 - evidence is a record, not the work
            log.exception("could not record acceptance for run %s", run_id)

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
        """The first task that stopped the run, and why, for the run's error.

        "Blocked: a dependency is not verified" alone sends a person to look
        through the tasks. The reason is known: a task never started (the
        specification could not be put in the sandbox, the Cloud refused), or
        it ran and did not verify (its commit is not the branch head, it ran
        out of time). That reason is the run's reason, and it is said here.
        """
        failed = self.store.first_start_failure(run_id)
        if failed is not None:
            return f". Task {failed['task_key']} was not started: {failed['last_error']}"
        unverified = self.store.first_unverified(run_id)
        if unverified is not None:
            return (f". Task {unverified['task_key']} ended {unverified['state']}: "
                    f"{unverified['reason']}")
        return ""

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
