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

What happens to a task that did not verify depends on the run's policy
(``nexus_lab_store.RUN_POLICIES``). A ``bounded`` run records the verdict and
stops when nothing more can be started; a person resumes it. A ``durable`` run
tries again by itself: in the same conversation where the agent can still put
right what is missing, in a new one otherwise; a bounded number of times in a
row, then after a pause that grows (``nexus_retry``). It ends when every task
verified and the repositories were looked at once more, or when a person
cancels it - and a cancelled run's conversations are told to stop.
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
from .github_client import GitHubAuthError, GitHubClient, GitHubError, GitHubUnavailable
from . import nexus_retry
from .nexus_binding import AuthorizationError, ProjectRepositoryService, authorize_run_task
from .nexus_github_accounts import GitHubAccountService, SecretsError
from .nexus_evidence import LabEvidence
from .nexus_lab_store import (
    IN_FLIGHT_TASK_STATES,
    TERMINAL_RUN_STATES,
    UNFINISHED_TASK_STATES,
    LabStore,
)
from .nexus_spec_delivery import Delivery, prepare, prepare_request
from .nexus_specs import SpecService
from .openhands_router import AccountRouter, NoUsableAccount

log = logging.getLogger("nexus_lab")

#: Conversation execution states that mean the agent is still working.
_RUNNING = {"idle", "running", "waiting_for_confirmation"}
#: Terminal-bad states.
_BAD = {"error", "stuck"}
#: States in which a conversation does nothing more by itself: what a stop
#: signal is confirmed by. ``idle`` is not one of them - an idle agent may be
#: about to run.
_STOPPED = {"paused", "finished", "error", "stuck", "deleting"}

#: How a task that did not verify may be tried again (``Verification.retry``).
#:   followup - the conversation finished; say what is missing in it
#:   restart  - the conversation is no use any more; start another
#:   reverify - the work may be fine, the check could not be made; check later
#:   never    - trying again cannot change the outcome
RETRY_HINTS = ("followup", "restart", "reverify", "never")


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
    #: How the task may be tried again when it did not verify (``RETRY_HINTS``).
    #: Empty lets the scheduler decide from what the conversation was.
    retry: str = ""

    def as_detail(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "reason": self.reason}


class Dispatched(str):
    """A conversation id that also says how the task was handed to the agent.

    A plain ``str`` everywhere a conversation id is expected; ``delivery`` is
    the evidence the scheduler records on the task (see ``mark_running``),
    ``account_id`` is the OpenHands account that ran it (``None`` for an
    executor that is not account-aware), and ``kind`` is what the conversation
    is to the task: a ``start``, a ``continue`` in the conversation it already
    had, a ``handoff`` to another executor, or an orphan that was ``adopted``.
    """

    delivery: dict[str, Any] | None = None
    account_id: str | None = None
    kind: str = "start"

    def __new__(cls, conversation_id: str, delivery: dict[str, Any] | None = None,
                account_id: str | None = None, kind: str = "start"):
        self = super().__new__(cls, conversation_id)
        self.delivery = delivery
        self.account_id = account_id
        self.kind = kind
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

    #: A result that did not verify can be put right in the conversation that
    #: produced it (``dispatch`` continues ``task['resume_conversation_id']``).
    can_continue = True

    def __init__(self, client: Any, *, poll_interval: float = 2.0,
                 start_poll_attempts: int = 150, specs: SpecService | None = None,
                 router: Any | None = None, client_factory: Any | None = None,
                 resume_poll_attempts: int = 15):
        self.client = client
        self.poll_interval = poll_interval
        self.start_poll_attempts = start_poll_attempts
        #: How many polls a continued conversation is given to be seen at work
        #: again before the continuation is taken as not having happened.
        self.resume_poll_attempts = resume_poll_attempts
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
            f"Repository: {repo}\nBranch: {branch}\n{run}"
            + OpenHandsExecutor._project_repositories(task) + "\n"
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
    def _project_repositories(task: dict) -> str:
        """What the agent is told when its project has several repositories:
        which they are, which is the main one, which one this task is for, and
        that it changes that one only. Empty for a project with one repository
        (the message is then what it always was).

        ``task["project_repositories"]`` is put there by the scheduler just
        before the start (``ProjectRepositoryService.context``).
        """
        repositories = task.get("project_repositories") or []
        if len(repositories) < 2:
            return ""
        mine = task.get("repository") or ""
        lines = ["", "This project has several repositories:"]
        for item in repositories:
            note = (item.get("role_note") or "").strip()
            lines.append(
                f"- {item['full_name']}, branch {item.get('branch') or '(default)'}"
                + (" (the main repository)" if item.get("is_primary") else "")
                + (f" - {note}" if note else "")
                + ("  <- this task" if item["full_name"] == mine else ""))
        lines.append(
            f"This task changes {mine or 'the repository named above'} only. The other "
            "repositories are changed by other tasks of the same run, each in a workspace "
            "of its own: you may read them to understand an interface, and you do not "
            "commit or push to them. If your task cannot be finished without a change "
            "in another repository, say exactly which change in your report.")
        return "\n".join(lines) + "\n"

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
        if account is None:
            # No stored executor serves this project: the server's own key.
            return self.client, None
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
        """Stop a conversation for real, on the account that owns it.

        Called for a conversation that must not go on: a switch or a failover
        left it behind, its run was cancelled, it ran out of time. It may still
        be running in its sandbox, editing the task's checkout. The client's
        ``stop_conversation`` stops the agent and keeps the transcript.

        Returns whether the caller may treat the conversation as stopped:

        * the transport took the stop;
        * the transport did not, and there is nothing left to stop - the
          conversation is gone, or already in a state in which it does nothing
          more by itself. (A failover away from a conversation that has
          *vanished* would otherwise hold its branch for ever, waiting for a
          stop nobody can take.)
        * the transport cannot stop a conversation at all
          (``supports_conversation_stop`` is false or absent: a test double, a
          transport with no such call). There is nothing to wait for, and
          holding the branch for a stop that can never happen would strand the
          task.

        ``False`` - the stop was not taken and the conversation may still be at
        work, or could not be asked - leaves what is owed in place, so the
        branch stays held and the caller tries again.
        """
        client = self._stop_client(account_id, project_id)
        if not getattr(client, "supports_conversation_stop", False):
            # No conversation-scoped stop on this transport; nothing was stopped
            # and nothing can be. The branch lock is the guarantee, as before.
            return True
        stop = getattr(client, "stop_conversation", None)
        if not callable(stop):
            return True
        try:
            if stop(conversation_id):
                return True
        except CloudError:
            pass
        try:
            state = self._state_of(client, conversation_id)
        except Exception:  # noqa: BLE001 - could not be asked: still owed
            return False
        return state == "missing" or state in _STOPPED

    def _stop_client(self, account_id: str | None, project_id: str | None = None) -> Any:
        """The client of the account a conversation runs on."""
        if self.router is not None and account_id:
            return self._client_for_account(
                str(account_id), {"project_id": project_id} if project_id else None)
        return self.client

    def dispatch(self, task: dict) -> str:
        """Give *task* to an agent and return the conversation that has it.

        Three ways, in this order:

        * the task asks to be **continued** where it was
          (``resume_conversation_id``): the agent of that conversation is told
          what is missing. If the conversation cannot be continued - it is
          gone, its sandbox does not answer - a new one is started instead;
        * the task's claim was **lost with a worker** (``reclaimed``): the
          conversation that worker may have started is looked for by its tags
          and adopted, so the same work is not started twice;
        * otherwise a **new conversation** is started.
        """
        continued = self._continue(task)
        if continued is not None:
            return continued
        client, account_id = self._dispatch_client(task)
        adopted = self._adopt(client, account_id, task)
        if adopted is not None:
            return adopted
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
        previous = _last_account(task)
        kind = ("handoff" if self.router is not None and previous
                and previous != str(account_id or "") else "start")
        start = client.start_conversation(
            initial_message=initial,
            selected_repository=task.get("repository") or None,
            selected_branch=task.get("branch") or None,
            title=f"lab:{task['task_key']}",
            tags=self._tags(task),
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
            return Dispatched(conv_id, proof, account_id, kind)
        if hasattr(client, "get_start_task") and getattr(start, "id", None):
            current = start
            for _ in range(self.start_poll_attempts):
                conv_id = getattr(current, "app_conversation_id", None)
                if conv_id:
                    return Dispatched(conv_id, proof, account_id, kind)
                if getattr(current, "failed", False):
                    raise CloudError(f"start task failed: {getattr(current, 'detail', '')}")
                time.sleep(self.poll_interval)
                nxt = client.get_start_task(current.id)
                if nxt is None:
                    raise CloudError("start task disappeared")
                current = nxt
            raise CloudError("start task not READY within the poll budget")
        return Dispatched(getattr(start, "id", ""), proof, account_id, kind)

    @staticmethod
    def _tags(task: dict) -> dict[str, str]:
        """What a conversation is tagged with, so that it can be found again:
        the run, the task, and which attempt of which cycle it is."""
        return {"nexusrun": str(task["run_id"])[:40], "nexustask": task["task_key"][:40],
                "nexusattempt": f"c{int(task.get('cycle') or 0)}a{int(task.get('attempt') or 0)}"}

    # ------------------------------------------------- continue, adopt, stop
    def _continue(self, task: dict) -> Dispatched | None:
        """Say what is missing in the conversation the task already had.

        Returns the conversation once it has taken the message, and ``None``
        when the message could not be given to it (it is gone, its sandbox does
        not answer) - nothing was sent then, and the caller starts a new
        conversation. A refusal (429) is raised: the service is working and
        will take the message later.

        After the message is in, the conversation is watched for a moment
        until it is seen at work again. The service may go on reporting
        ``finished`` for an instant after it took the message; a poll in that
        instant would read the previous answer as the new one.
        """
        conversation_id = task.get("resume_conversation_id")
        if not conversation_id:
            return None
        client = self._client_for_task(task)
        if not hasattr(client, "send_message"):
            return None
        account_id = str(task["openhands_account_id"]) if task.get("openhands_account_id") else None
        try:
            client.send_message(str(conversation_id), self.follow_up(task), run=True)
        except CloudRateLimited:
            raise
        except CloudError as e:
            log.info("conversation %s cannot be continued (%s); task %s gets a new one",
                     conversation_id, type(e).__name__, task["task_key"])
            return None
        for _ in range(max(1, self.resume_poll_attempts)):
            try:
                status = client.get_conversation(str(conversation_id)).execution_status
            except CloudError:
                break
            if status != "finished":
                break
            time.sleep(self.poll_interval)
        return Dispatched(str(conversation_id), None, account_id, "continue")

    @staticmethod
    def _marker(client: Any, conversation_id: str) -> str | None:
        probe = getattr(client, "activity_marker", None)
        if probe is None:
            return None
        try:
            return probe(str(conversation_id))
        except CloudError:
            return None

    def follow_up(self, task: dict) -> str:
        """What the agent is told when its result was not accepted."""
        why = (task.get("resume_note") or "the result could not be verified").strip()
        return (
            "NEXUS checked your result and cannot accept it yet.\n"
            f"Reason: {why}\n\n"
            "Put right what is missing in this same workspace - what you did so far "
            "is still here; do not start over.\n"
            + self._hand_in(task).replace("6. ", "", 1) +
            "Then finish with the report again: the full SHA of the branch head after "
            "your push, files changed, tests run and their result, and any blocker. Do "
            "not claim success you did not verify.")

    def _adopt(self, client: Any, account_id: str | None, task: dict) -> Dispatched | None:
        """The conversation a lost worker started for this task, if there is one.

        A claim that ran out with no conversation recorded (``reclaimed``) is
        one of two things: the worker died before it started anything, or after
        - and then a conversation is at work that nobody follows. Starting
        another beside it would put two agents on one branch. Conversations
        carry the run and the task as tags, so the second case can be seen.
        """
        evidence = task.get("evidence") or []
        last = evidence[-1] if evidence else None
        if not (isinstance(last, dict) and last.get("reclaimed")):
            return None
        search = getattr(client, "search_conversations", None)
        if search is None:
            return None
        try:
            found, _next = search(limit=50, tag=("nexusrun", str(task["run_id"])[:40]))
        except CloudError as e:
            log.warning("could not look for an orphaned conversation of task %s: %s",
                        task["task_key"], type(e).__name__)
            return None
        known = {str(item.get("conversation_id")) for item in evidence
                 if isinstance(item, dict) and item.get("conversation_id")}
        for conversation in found:
            if (conversation.tags or {}).get("nexustask") != task["task_key"][:40]:
                continue
            if str(conversation.id) in known:
                continue                   # an attempt that is already over
            if conversation.execution_status in _STOPPED - {"finished"}:
                continue
            log.info("task %s: adopted conversation %s started by a lost worker",
                     task["task_key"], conversation.id)
            return Dispatched(str(conversation.id), None, account_id, "adopted")
        return None

    def conversation_state(self, target: dict) -> str:
        """What *target*'s conversation is doing: its execution status,
        ``paused`` when the sandbox that would run it is not running, and
        ``missing`` when it is gone. *target* names the ``conversation_id``,
        the ``project_id`` and, with a pool, the ``openhands_account_id`` it
        runs on.

        A cloud conversation is stopped by pausing its sandbox, and the
        service reports no execution status at all for a conversation whose
        sandbox is not running - it has nothing to ask. Read alone, that
        empty status would look like "still at work" for ever; the sandbox's
        own status is what says the agent has stopped.
        """
        return self._state_of(self._client_for_task(target), str(target["conversation_id"]))

    @staticmethod
    def _state_of(client: Any, conversation_id: str) -> str:
        try:
            conversation = client.get_conversation(conversation_id)
        except CloudNotFound:
            return "missing"
        status = conversation.execution_status or ""
        sandbox = (getattr(conversation, "sandbox_status", None) or "").upper()
        if status not in _STOPPED and sandbox in ("PAUSED", "MISSING", "ERROR"):
            return "paused"
        return status

    def progress(self, task: dict, conversation_id: str) -> str | None:
        """A value that changes while the conversation moves, or ``None`` when
        that cannot be told (see ``activity_marker`` of the clients)."""
        return self._marker(self._client_for_task(task), conversation_id)

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
            context = self.router.handoff_context(
                task, last_error=task.get("resume_note") or task.get("last_error") or "")
            # The continuation is held to the same rule as a first start: the
            # work counts when it is on the task's branch on GitHub.
            base = self.router.handoff_message(context) + "\n" + self._hand_in(task).rstrip("\n")
            if delivery is not None:
                return f"{base}\n\n{delivery.block}"
            return base
        text = self.instructions(task, delivery.block if delivery else "")
        earlier = self._earlier(task)
        return f"{earlier}\n\n{text}" if earlier else text

    @staticmethod
    def _earlier(task: dict) -> str:
        """What a new conversation is told about the attempts before it.

        A task that is started again is not started from nothing: whatever an
        earlier attempt pushed is on the branch. The agent is told that an
        attempt was made, why it was not accepted, and to look at the branch
        first and go on from what is there.
        """
        note = (task.get("resume_note") or "").strip()
        if not note:
            return ""
        which = int(task.get("attempt") or 0) + int(task.get("cycle") or 0) * int(
            task.get("max_attempts") or 0)
        head = f" The branch head was {task['base_sha']} when this attempt began." \
            if task.get("base_sha") else ""
        return (
            f"This task has been attempted before (this is start number {max(which, 2)}). "
            f"The last attempt was not accepted: {note}\n"
            "Before changing anything, look at what is already on the branch: work an "
            "earlier attempt pushed is there. Keep it, and do only what is still missing."
            + head)

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
    """``client_for(task)``: the GitHub identity a task's repository is read with.

    A project may have several repositories from different GitHub accounts, so
    the identity is found per repository: the account of the link that ties
    the task's repository to its project
    (``nexus_project_repositories.github_account_id``) - a task on
    ``account-A/frontend`` is read with account A's token, one on
    ``account-B/backend`` with account B's. A repository whose link names no
    account is read with the project's account, exactly as the studio reads
    its files; a project without one is read with *fallback* (the server's
    ``GITHUB_TOKEN``).

    The account is never swapped for another: if it was revoked, or its token
    cannot be decrypted, the call raises and the result is not judged by other
    means.
    """
    service = accounts or GitHubAccountService(store)
    links = ProjectRepositoryService(store)

    def client_for(task: dict) -> GitHubClient:
        principal, account_id = links.account_for_repository(
            str(task["project_id"]), task.get("repository") or "")
        if account_id is None:
            return fallback
        return service.client_for_account(principal, account_id)

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

    def _github(self, task: dict) -> GitHubClient:
        return self.client_for(task) if self.client_for is not None else self.github

    def head(self, task: dict) -> str | None:
        """The head of the task's branch now, or ``None`` when it cannot be
        read. What an attempt starts from; never a reason to hold a task back."""
        repository = task.get("repository") or ""
        if not repository:
            return None
        try:
            return self._github(task).default_branch_sha(repository, task.get("branch") or None)
        except (GitHubError, ValueError, NotFound, SecretsError):
            return None

    def verify(self, task: dict, report: TaskReport) -> Verification:
        if report.state == "missing":
            return Verification("failed", "conversation vanished",
                                {"state": report.state}, retry="restart")
        if report.state in _BAD:
            return Verification("failed", f"conversation {report.state}",
                                {"state": report.state}, retry="restart")
        if not report.finished:
            return Verification("blocked", "conversation did not finish",
                                {"state": report.state}, retry="restart")
        repository = task.get("repository") or ""
        branch = task.get("branch") or ""
        evidence: dict[str, Any] = {"state": "finished",
                                    "final_text": report.final_text[:2000]}
        if not repository:
            # No repository to inspect: the work cannot be independently
            # verified, so it is unverified rather than verified. Doing the
            # work again would not change that.
            return Verification("completed_unverified",
                                "no repository bound; cannot verify remote state",
                                evidence, retry="never")
        try:
            github = self._github(task)
            sha = github.default_branch_sha(repository, branch or None)
        except (GitHubError, ValueError, NotFound, SecretsError) as e:
            # The agent's work is not in question; the look at GitHub failed.
            return Verification("blocked", f"github unavailable: {type(e).__name__}",
                                {**evidence, "error": str(e)}, retry="reverify")
        evidence["remote_sha"] = sha
        evidence["branch"] = branch or "(default)"
        if task.get("base_sha"):
            # Whether the branch moved while the task was at work. A task that
            # named the head it started from changed nothing; that is said in
            # the run's report, not hidden behind "verified".
            evidence["base_sha"] = task["base_sha"]
            evidence["changed"] = bool(sha) and sha != task["base_sha"]
        if not sha:
            return Verification("completed_unverified",
                                "branch has no readable commit", evidence, retry="followup")
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
                                "reported commit is not the branch head", evidence,
                                retry="followup")
        if not self.require_commit:
            return Verification("completed_unverified",
                                "no commit reported", evidence, retry="followup")
        return Verification("completed_unverified",
                            "no verifiable commit reported by the agent", evidence,
                            retry="followup")

    def confirm(self, tasks: list[dict], *, bases: dict[tuple[str, str], str] | None = None
                ) -> dict[str, Any]:
        """Look at the repositories once more, when every task has verified.

        Each task was accepted when the head of its branch was a commit its
        agent named. Since then other tasks pushed, and so may anyone else. A
        run is reported as done only if what each task was accepted for is
        still in its branch now: the accepted commit is the head, or an
        ancestor of it.

        Returns::

            {"unavailable": "<why GitHub could not be asked>" or "",
             "reopen": [{"task_id", "task_key", "reason"}],
             "repositories": [{"repository", "branch", "head_sha", "base_sha",
                               "tasks": [task keys], "unchanged": [task keys],
                               "commits": [...], "files": [...],
                               "total_commits", "files_listed", "checks": {...}}]}

        ``reopen`` lists tasks whose accepted commit is gone from the branch -
        their work has to be done again. ``unavailable`` means nothing can be
        said now; the caller asks again later. *bases* is, per ``(repository,
        branch)``, the head the run started from; with it the commits and
        files between then and now are listed (GitHub's own data, not an
        agent's account of it). ``checks`` is what the repository's checks say
        about the head, where it runs any.
        """
        groups: dict[tuple[str, str], list[dict]] = {}
        for task in tasks:
            if task.get("repository"):
                groups.setdefault((task["repository"], task.get("branch") or ""), []).append(task)
        out: dict[str, Any] = {"unavailable": "", "reopen": [], "repositories": []}
        for (repository, branch), members in groups.items():
            try:
                github = self._github(members[0])
                head = github.default_branch_sha(repository, branch or None)
            except (GitHubError, ValueError, NotFound, SecretsError) as e:
                out["unavailable"] = f"{repository}: {type(e).__name__}: {e}"
                continue
            if not head:
                out["unavailable"] = f"{repository}: branch {branch or '(default)'} has no head"
                continue
            entry: dict[str, Any] = {
                "repository": repository, "branch": branch, "head_sha": head,
                "base_sha": (bases or {}).get((repository, branch)),
                "tasks": [task["task_key"] for task in members], "unchanged": [],
                "commits": [], "files": [], "total_commits": 0, "files_listed": 0,
                "checks": None}
            contained: dict[str, bool | None] = {}
            for task in members:
                accepted = _accepted_sha(task)
                if not accepted:
                    continue
                if accepted not in contained:
                    contained[accepted] = self._contains(github, repository, accepted, head)
                if contained[accepted] is None:
                    out["unavailable"] = (f"{repository}: could not compare {accepted[:12]} "
                                          f"with the branch head")
                elif contained[accepted] is False:
                    out["reopen"].append({
                        "task_id": str(task["id"]), "task_key": task["task_key"],
                        "reason": (f"the commit this task was accepted for ({accepted[:12]}) "
                                   f"is no longer in branch {branch or '(default)'} of "
                                   f"{repository} (its head is {head[:12]})")})
                if _unchanged(task):
                    entry["unchanged"].append(task["task_key"])
            base = entry["base_sha"]
            if base and base != head:
                try:
                    between = github.compare(repository, base, head)
                    entry.update(commits=between["commits"], files=between["files"],
                                 total_commits=between["total_commits"],
                                 files_listed=between["files_listed"],
                                 comparison=between["status"])
                except (GitHubError, ValueError) as e:
                    entry["comparison_error"] = f"{type(e).__name__}: {e}"
            try:
                entry["checks"] = github.check_summary(repository, head)
            except (GitHubError, ValueError) as e:
                entry["checks_error"] = f"{type(e).__name__}: {e}"
            out["repositories"].append(entry)
        return out

    @staticmethod
    def _contains(github: GitHubClient, repository: str, commit: str, head: str) -> bool | None:
        """Whether *commit* is *head* or an ancestor of it; ``None`` when
        GitHub could not say."""
        if head.startswith(commit) or commit.startswith(head):
            return True
        try:
            status = github.compare(repository, commit, head, max_commits=1, max_files=1)["status"]
        except GitHubUnavailable:
            return None
        except GitHubAuthError:
            return None
        except (GitHubError, ValueError):
            # GitHub answers 404 when it does not know the commit: it is not
            # in this repository any more.
            return False
        return status in ("ahead", "identical")


def _accepted_sha(task: dict) -> str:
    """The commit a verified task was accepted for: the branch head the
    verifier read when it accepted it."""
    for item in reversed(task.get("evidence") or []):
        if isinstance(item, dict) and item.get("remote_sha") and item.get("named_sha"):
            return str(item["remote_sha"])
    return ""


def _unchanged(task: dict) -> bool:
    """Whether the task was accepted without the branch having moved."""
    for item in reversed(task.get("evidence") or []):
        if isinstance(item, dict) and item.get("remote_sha") and "changed" in item:
            return item["changed"] is False
    return False


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


def refused_start_wait(error: CloudRateLimited, refusals: int = 0) -> float:
    """Seconds to wait before asking again after a 429.

    What the service asked for (``Retry-After``), kept between half a minute
    and an hour. Without it the wait doubles with every refusal in a row
    (*refusals* is how many came before this one): from a minute - or, where
    the service named a daily limit and nothing will change for hours, from a
    quarter of an hour - up to an hour.
    """
    daily = "daily" in (error.code or "").lower() or "daily" in str(error).lower()
    return nexus_retry.refused_wait(retry_after=error.retry_after, daily=daily,
                                    refusals=refusals)


def _span(seconds: float) -> str:
    return nexus_retry.span(seconds)


#: How often a running conversation is looked at for signs of life, and how
#: long it may show none before it is taken for hung. ``NEXUS_TASK_STALL_SECONDS``
#: changes the second; 0 switches the check off.
PROGRESS_INTERVAL_SECONDS = 60.0
DEFAULT_STALL_SECONDS = 1800

#: How long a result waits to be checked again when GitHub could not be asked.
REVERIFY_SECONDS = 60.0

#: A stop signal is tried this many times before it is given up as
#: unconfirmed; the pause between tries grows from ``STOP_RETRY_SECONDS``.
MAX_STOP_ATTEMPTS = 40
STOP_RETRY_SECONDS = 15.0
#: Once the signal is in, how soon the conversation is looked at again.
STOP_OBSERVE_SECONDS = 5.0


def stall_seconds() -> int:
    import os

    raw = os.environ.get("NEXUS_TASK_STALL_SECONDS", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_STALL_SECONDS
    except ValueError:
        value = DEFAULT_STALL_SECONDS
    return max(0, value)


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
    #: waiting, and nothing of it is in flight. A person has to act first - or,
    #: for a durable run that waits out a pause, time has to pass (``waiting``).
    idle: bool = False
    #: The run has nothing in flight and nothing it may start yet: it is
    #: between two tries. It will go on by itself.
    waiting: bool = False
    #: A task that did not verify was sent back to be tried again.
    retried: str | None = None
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
      and (for a bounded run) its deadline has not passed. The store checks that
      again inside the claim itself, so a pause or cancel is never overtaken.

    A ``durable`` run does not stop at a task that failed: see the module
    docstring and ``_retry``. *reports*, when given, writes the run's report in
    the transaction that completes it and delivers it to the chat the run came
    from (``nexus_reports.ReportService``).
    """

    def __init__(self, store: LabStore, executor: Executor, verifier: Verifier, *,
                 principal: str = "machine:default", worker_id: str = "lab",
                 lease_seconds: int = 900, router: AccountRouter | None = None,
                 start_wait: Callable[..., float] | None = None,
                 reports: Any | None = None,
                 cycle_wait: Callable[..., float] | None = None,
                 evidence: Any | None = None, acceptance: Any | None = None):
        self.store = store
        self.executor = executor
        self.verifier = verifier
        self.principal = principal
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        #: How long a task waits after the service refused to start it.
        self.start_wait = start_wait or refused_start_wait
        #: How long a task waits between two work cycles: ``(cycle, seed=...)``.
        self.cycle_wait = cycle_wait or nexus_retry.cycle_wait
        #: When set, a dispatch failure is attributed to the account the task
        #: was bound to, so a failover excludes it on the next attempt.
        self.router = router
        self.reports = reports
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

    def _refused_wait(self, refused: CloudRateLimited, task: dict) -> float:
        try:
            return float(self.start_wait(refused, int(task.get("refusals") or 0)))
        except TypeError:                  # a caller's own rule that takes the error only
            return float(self.start_wait(refused))

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
            self.router.pool.record_failure(self.router._owner(account, task), account,
                                            why, unavailable=False)
            try:
                elsewhere = self.router.select_for_task(
                    {**task, "tried_accounts": [*(task.get("tried_accounts") or []), account]}
                ) is not None
            except NoUsableAccount:
                pass
        if elsewhere:
            self.store.postpone_task(
                task["id"], seconds=0.0, failed_account_id=account,
                error=("OpenHands did not take the start on this account; another account "
                       f"takes the task (this is not counted as an attempt). {why}"),
                lease_token=task.get("lease_token"))
            return
        wait = self._refused_wait(refused, task)
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

    # ------------------------------------------------------------- the tick
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
        durable = run.get("policy") == "durable"
        result.reclaimed = len(self.store.reclaim_expired(run_id))

        # 0. Stop every conversation a switch or failover left behind, so a task
        #    cannot have two writers on its branch at once. The branch stays held
        #    (``_BRANCH_FREE``) until this succeeds.
        self._stop_abandoned(run_id)

        # 1. Poll every task already dispatched, so a slow conversation advances.
        in_flight = self._collect(run, durable, result)

        if run["state"] != "running":
            result.idle = in_flight == 0
            result.note = f"run is {run['state']}: nothing new is started"
            return result

        # 2. Claim and dispatch the next claimable task. The claim refuses by
        #    itself when the run stopped running, pauses or is out of budget. Any
        #    conversation the poll above just abandoned is stopped first, so a
        #    failover in this same tick is not held back by the branch lock.
        self._stop_abandoned(run_id)
        task = self.store.claim_task(run_id, self.worker_id, lease_seconds=self.lease_seconds)
        if task is not None:
            result.claimed = task["task_key"]
            if self._start(run, durable, task, result):
                return result

        # 3. Decide the run's fate.
        outcome = self._decide(run_id, durable)
        if outcome is not None:
            state, last_error, note = outcome
            if state == "completed":
                # Completed by ``_complete``, together with its report (and
                # attested there).
                result.finished = True
            else:
                applied = self.store.set_run_state_internal(run_id, state,
                                                            last_error=last_error)
                result.finished = applied is not None
                if applied is not None and note:
                    result.note = result.note or note
                if applied is not None and state in TERMINAL_RUN_STATES:
                    # ACCEPTANCE: the run's terminal decision is attested over the
                    # tasks' own verdicts - ACCEPTED only when every task verified,
                    # otherwise REJECTED/NOT_READY - never asserted by the scheduler.
                    self._record_run_acceptance(run_id, state, last_error)
        elif durable and in_flight == 0 and task is None:
            # Nothing at work and nothing that may be started this instant: the
            # run is between two tries, or pausing. It goes on by itself.
            result.waiting = True
        return result

    # ----------------------------------------------- what is already at work
    def _collect(self, run: dict, durable: bool, result: TickResult) -> int:
        """Read the conversations under way; returns how many still are."""
        run_id = str(run["id"])
        in_flight = 0
        for task in self.store.in_flight_tasks(run_id):
            conv = task.get("conversation_id")
            if not conv or task.get("held"):
                in_flight += 1
                continue
            report = self.executor.poll(task, conv)
            if report.state == "running":
                over = self._over_limit(task, conv, durable)
                if over is None:
                    in_flight += 1
                    continue
                outcome, reason = over
                if durable:
                    # The conversation is told to stop before the task is given
                    # to anyone else: two agents must not work one branch.
                    self.store.request_stop(task, conv, reason=reason)
                    self._retry(task, Verification(
                        "failed", reason, {"state": "running", "conversation_id": conv},
                        retry="restart"), report, result, outcome=outcome)
                    continue
                # The task's own time limit. The conversation is left as it is
                # (its transcript is the explanation); the task is over, and it
                # is not started a second time behind the first one's back.
                verdict = Verification("failed", reason,
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
                again = task["attempt"] < task["max_attempts"]
                if durable and not again:
                    self._retry(task, Verification(
                        "failed", "conversation missing; executor may be gone",
                        {"state": "missing", "conversation_id": conv}, retry="restart"),
                        report, result, outcome="vanished")
                else:
                    self.store.requeue_for_failover(
                        task["id"], failed_account_id=account_id,
                        error="conversation missing; executor may be gone", requeue=again,
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
            if durable and verdict.verdict != "completed_verified":
                if self._retry(task, verdict, report, result):
                    if (verdict.retry or "") == "reverify":
                        in_flight += 1
                    else:
                        # EVIDENCE: an attempt that was not accepted is a result
                        # too, and the signed trail must show it - the task goes
                        # back to work, the record of why stays.
                        self._record_evidence(task, verdict, report)
                    continue
            self.store.record_verification(
                task["id"], verdict=verdict.verdict,
                evidence={"reason": verdict.reason, **verdict.evidence},
                detail=verdict.as_detail(), lease_token=task["lease_token"])
            self._record_evidence(task, verdict, report)
            result.verified = task["task_key"]
            result.verdict = verdict.verdict
        return in_flight

    def _over_limit(self, task: dict, conversation_id: str,
                    durable: bool) -> tuple[str, str] | None:
        """Why a running conversation is given up, or ``None`` to leave it.

        The task's own time limit applies to every run. A durable run also
        watches for a conversation that shows no sign of life: it is asked,
        once a minute, for a value that changes while it works, and when that
        has stood still for ``stall_seconds()`` the conversation is hung. A
        transport that cannot give such a value is never judged by it.
        """
        if task["running_seconds"] > task["timeout_seconds"]:
            return ("timed_out", f"timed out after {task['timeout_seconds']} s; the "
                    + ("conversation was told to stop" if durable
                       else "conversation was left running"))
        limit = stall_seconds()
        probe = getattr(self.executor, "progress", None)
        if not durable or not limit or probe is None:
            return None
        checked = task.get("checked_seconds")
        if checked is not None and checked < PROGRESS_INTERVAL_SECONDS:
            return None
        try:
            marker = probe(task, conversation_id)
        except CloudError:
            marker = None
        moved = self.store.note_progress(task["id"], marker)
        if marker is None or moved or task["quiet_seconds"] <= limit:
            return None
        return ("stalled", f"no activity for {_span(task['quiet_seconds'])}; the "
                           "conversation was told to stop")

    # ----------------------------------------------------------- trying again
    def _retry(self, task: dict, verdict: Verification, report: TaskReport,
               result: TickResult, *, outcome: str | None = None) -> bool:
        """A durable run's answer to a task that did not verify.

        Returns ``False`` when the verdict is to be recorded as it is - trying
        again cannot change it (``never``), or the operator's ceiling on
        cycles is reached. Otherwise the task is sent back and ``True`` is
        returned:

        * ``reverify`` - the check itself could not be made: the task stays as
          it is and is looked at again in a minute; nothing is spent;
        * attempts left in the cycle - tried again on the next pass, in the
          same conversation when that can still put it right (``followup``)
          and the conversation has not already been continued once, else in a
          new one;
        * none left - the task waits (``nexus_retry.cycle_wait``) and then
          starts its next cycle.
        """
        hint = verdict.retry or ("followup" if report.finished else "restart")
        if hint == "never":
            return False
        if hint == "reverify":
            self.store.hold_task(
                task["id"], seconds=REVERIFY_SECONDS,
                reason=f"the result is waiting to be checked: {verdict.reason}")
            result.note = "result held: the check could not be made"
            return True
        evidence = {"reason": verdict.reason, **verdict.evidence}
        note = self._rework_note(task, verdict)
        continued = (task.get("execution_kind") or "") == "continue"
        same_place = (hint == "followup" and not continued
                      and bool(getattr(self.executor, "can_continue", False)))
        if continued and report.conversation_id:
            # A conversation that was told what is missing and still did not
            # deliver is left behind for a new one - and told to stop, so that
            # it cannot wake up beside its successor.
            self.store.request_stop(task, report.conversation_id,
                                    reason="left for a new conversation: " + verdict.reason)
        if task["attempt"] < task["max_attempts"]:
            self.store.retry_task(
                task["id"], verdict=verdict.verdict, evidence=evidence,
                detail=verdict.as_detail(), note=note, continue_conversation=same_place,
                outcome=outcome, lease_token=task.get("lease_token"))
            result.retried = task["task_key"]
            result.note = "not verified: trying again"
            return True
        ceiling = nexus_retry.max_cycles()
        cycle = int(task.get("cycle") or 0) + 1
        if ceiling and cycle >= ceiling:
            verdict.reason = (f"{verdict.reason} (given up after {cycle} work cycles; "
                              "NEXUS_RETRY_MAX_CYCLES)")
            return False
        wait = self.cycle_wait(cycle, seed=str(task["id"]))
        self.store.retry_task(
            task["id"], verdict=verdict.verdict, evidence=evidence,
            detail=verdict.as_detail(), continue_conversation=same_place, outcome=outcome,
            wait_seconds=wait, lease_token=task.get("lease_token"),
            note=(f"{note} Попытки цикла исчерпаны ({task['max_attempts']}); следующий цикл "
                  f"через {_span(wait)}."))
        result.retried = task["task_key"]
        result.note = f"not verified: next cycle in {_span(wait)}"
        return True

    @staticmethod
    def _rework_note(task: dict, verdict: Verification) -> str:
        """What the next attempt is told about the one that was not accepted."""
        evidence = verdict.evidence or {}
        branch = task.get("branch") or "the default branch"
        reason = verdict.reason
        if reason == "reported commit is not the branch head":
            named, head = evidence.get("named_sha") or "?", evidence.get("remote_sha") or "?"
            return (f"the commit your report names ({named}) is not the head of {branch} on "
                    f"GitHub (the head is {head}). Push your work to that branch - pull first "
                    "if it has moved, never force-push - and name the new head.")
        if reason in ("no verifiable commit reported by the agent", "no commit reported"):
            head = evidence.get("remote_sha")
            return ("your report names no commit. Push your work and give the full SHA of "
                    f"the head of {branch} after your push"
                    + (f" (the head is now {head})." if head else "."))
        if reason == "branch has no readable commit":
            return (f"{branch} has no commit that can be read on GitHub. Push your work to "
                    "that branch and name its head.")
        return reason

    # ------------------------------------------------------------ a new start
    def _start(self, run: dict, durable: bool, task: dict, result: TickResult) -> bool:
        """Dispatch a claimed task. Returns ``True`` when the tick ends here."""
        run_id = str(run["id"])
        try:
            # The gate every other path to a conversation goes through. It
            # ran when the run was started; the link or the allowlist may
            # have changed since.
            authorize_run_task(self.store.store, project_id=str(task["project_id"]),
                               repository=task.get("repository") or "",
                               branch=task.get("branch") or "")
        except AuthorizationError as e:
            reason = f"not authorized: {e}"
            if durable:
                # Whatever stands in the way can be put right - a repository
                # switched back on, a link restored. The task waits and asks
                # again; nothing was started, so nothing is spent.
                self._set_aside(task, reason, result)
                return True
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
            return True
        if not self.store.holds_lease(task["id"], task["lease_token"]):
            # The claim was taken away between the claim and now (the lease
            # expired and another worker reclaimed the task, an operator
            # switched it, the run was cancelled). Starting a conversation
            # here would be a second executor on the same work; whoever holds
            # the task now starts it instead.
            result.note = "claim lost before dispatch; not started"
            return True
        # Where the branch stands before this attempt: what the agent starts
        # from, and what "changed" is later measured against.
        head = getattr(self.verifier, "head", None)
        base_sha = head(task) if head is not None else None
        if base_sha:
            task = {**task, "base_sha": base_sha}
        if task.get("repository"):
            # A project with several repositories: the agent is told which they
            # are and which one is its own.
            task = {**task, "project_repositories": ProjectRepositoryService(
                self.store.store).context(str(task["project_id"]))}
        try:
            conversation_id = self.executor.dispatch(task)
        except NoUsableAccount as e:
            # The pool has nothing left for this task (every account tried
            # or unusable).
            if durable:
                self._reprobe(task)
                self._set_aside(task, f"no usable OpenHands executor: {e}", result,
                                forget_tried=True)
                return True
            # That is not a transient transport error: retrying cannot succeed,
            # so the task is dead-lettered with the reason.
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
            return True
        except (CloudError, GitHubError) as e:
            refused = e if isinstance(e, CloudRateLimited) else getattr(e, "refused", None)
            if refused is not None:
                self._start_refused(task, e, refused)
                result.note = "start refused: rate limited"
                return True
            # A dispatch failure is a provider/transport problem: record the
            # account that failed (so a failover excludes it), release the
            # task so another attempt - or another account - can try, and
            # keep the lease short so recovery is fast. A failure that
            # another attempt would only repeat ends the task now, with its
            # reason - in a bounded run. A durable one waits and tries again.
            failed_account = self._failed_account(e, task)
            unavailable = getattr(e, "unavailable", isinstance(e, CloudAuthError))
            permanent = bool(getattr(e, "permanent", False))
            again = task["attempt"] < task["max_attempts"] and not permanent
            error = f"{type(e).__name__}: {e}"
            if durable and not again:
                cycle = int(task.get("cycle") or 0) + 1
                ceiling = nexus_retry.max_cycles()
                if ceiling and cycle >= ceiling:
                    self.store.release_task(task["id"], error=error, requeue=False,
                                            failed_account_id=failed_account,
                                            lease_token=task["lease_token"])
                else:
                    wait = self.cycle_wait(cycle, seed=str(task["id"]))
                    self.store.wait_task(
                        task["id"], seconds=wait, new_cycle=True,
                        failed_account_id=failed_account,
                        reason=f"{error} Следующая попытка через {_span(wait)}.",
                        lease_token=task["lease_token"])
            else:
                self.store.release_task(task["id"], error=error, requeue=again,
                                        failed_account_id=failed_account,
                                        lease_token=task["lease_token"])
            if failed_account and self.router is not None:
                self.router.note_failure(
                    task, account_id=failed_account, error=error, unavailable=unavailable)
            result.note = f"dispatch failed: {type(e).__name__}"
            return True
        account_id = getattr(conversation_id, "account_id", None)
        if account_id is None and task.get("openhands_account_id"):
            account_id = str(task["openhands_account_id"])
        # A switch between accounts is recorded as a hand-off, with the
        # context that was passed, so the history of an executor change is
        # durable and auditable. The "from" account is whatever this task
        # was previously bound to or had tried.
        previous = _last_account(task)
        kind = getattr(conversation_id, "kind", "start")
        if (account_id and previous and previous != str(account_id)
                and self.router is not None):
            context = self.router.handoff_context(task)
            self.store.record_handoff(
                run_id=run_id, task_id=task["id"],
                from_account_id=previous,
                to_account_id=str(account_id),
                reason="previous account failed", context=context)
            if kind == "start":
                kind = "handoff"
        self.store.mark_running(task["id"], conversation_id=conversation_id or None,
                                delivery=getattr(conversation_id, "delivery", None),
                                account_id=account_id, base_sha=base_sha, kind=kind,
                                lease_token=task["lease_token"])
        result.dispatched = True
        return False

    def _set_aside(self, task: dict, reason: str, result: TickResult, *,
                   forget_tried: bool = False) -> None:
        """A start that could not even be made: the task waits and asks again.

        Each time in a row the wait is longer (the task's cycle counts them),
        so a blocker that lasts costs a look now and then, not a busy loop.
        """
        cycle = int(task.get("cycle") or 0) + 1
        wait = self.cycle_wait(cycle, seed=str(task["id"]))
        self.store.wait_task(
            task["id"], seconds=wait, new_cycle=True, give_back=True,
            forget_tried=forget_tried,
            reason=f"{reason}. Следующая проверка через {_span(wait)}.",
            lease_token=task.get("lease_token"))
        result.note = f"waiting: {reason}"

    def _reprobe(self, task: dict) -> None:
        """Ask the executors that were written off whether they are back."""
        reprobe = getattr(self.router, "reprobe", None) if self.router is not None else None
        if reprobe is None:
            return
        try:
            reprobe(task)
        except Exception:  # noqa: BLE001 - a health probe must not stop the loop
            log.debug("executor re-probe failed", exc_info=True)

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

    # ---------------------------------------------------------- the decision
    def _decide(self, run_id: str, durable: bool = False) -> tuple[str, str | None, str] | None:
        """The state the run should move to now, or ``None`` to keep going.

        ``("completed", None, "")`` means the run *has been* completed: a run
        whose tasks have all verified is completed by ``_complete``, which looks
        at the repositories once more and writes the report in the same
        transaction. Any other state is for the caller to apply.
        """
        counts = self.store.count_tasks(run_id)
        total = sum(counts.values())
        if total == 0:
            # A run with nothing in it has not achieved its goal.
            return ("failed", "the run has no tasks", "failed: no tasks")
        if counts.get("completed_verified", 0) == total:
            return ("completed", None, "") if self._complete(run_id) else None
        unfinished = sum(counts.get(state, 0) for state in UNFINISHED_TASK_STATES)
        if unfinished == 0:
            # Nothing left to do and not all verified: the run is blocked, not
            # complete. A person resumes it (retry) or cancels it.
            return ("blocked", "no claimable tasks; some tasks not verified" + self._why(run_id),
                    "blocked: tasks remain unverified")
        if sum(counts.get(state, 0) for state in IN_FLIGHT_TASK_STATES) > 0:
            return None                     # work is in flight; wait for it
        spent = self.store.budget_spent(run_id)
        if durable:
            if self.store.blocked_ancestry(run_id) and not self.store.has_claimable(run_id):
                # Everything that is left waits for a task that will not be
                # tried again. Only a person can move this.
                return ("blocked", "dependency graph deadlocked: a dependency is not verified"
                        + self._why(run_id), "blocked: dependency not verified")
            if spent and not self.store.run_pausing(run_id):
                # The task starts of this cycle are used. The run pauses and
                # goes on with them restored - it is slowed down, not stopped.
                row = self.store.run_row(run_id) or {}
                wait = self.cycle_wait(int(row.get("cycle") or 0) + 1, seed=str(run_id))
                self.store.pause_for_budget(
                    run_id, seconds=wait,
                    reason=f"{spent}; следующий цикл через {_span(wait)}")
            return None
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

    def _complete(self, run_id: str) -> bool:
        """Every task has verified: confirm it against the repositories as
        they are now, then complete the run and write its report as one fact.
        Returns whether the run is completed now.

        The run is not completed when the confirmation cannot be made (GitHub
        does not answer: asked again after a pause that grows) or does not hold
        (a commit a task was accepted for is no longer in its branch: that task
        goes back to work).
        """
        if self.store.run_pausing(run_id):
            return False                    # waiting to ask GitHub again
        confirmation: dict[str, Any] | None = None
        confirm = getattr(self.verifier, "confirm", None)
        if confirm is not None:
            tasks = self.store.tasks_internal(run_id)
            try:
                confirmation = confirm(tasks, bases=self.store.run_bases(run_id))
            except Exception as e:  # noqa: BLE001 - never complete on a failed check
                log.exception("final confirmation of run %s failed", run_id)
                confirmation = {"unavailable": f"{type(e).__name__}: {e}", "reopen": [],
                                "repositories": []}
            if confirmation.get("reopen"):
                for item in confirmation["reopen"]:
                    self.store.reopen_task(item["task_id"], reason=item["reason"])
                return False
            if confirmation.get("unavailable"):
                row = self.store.run_row(run_id) or {}
                wait = self.cycle_wait(int(row.get("confirm_attempts") or 0) + 1,
                                       seed=f"confirm:{run_id}", base=30)
                self.store.defer_run(
                    run_id, seconds=wait, confirming=True,
                    reason=("итоговая сверка с GitHub не выполнена: "
                            f"{confirmation['unavailable']}; повтор через {_span(wait)}"))
                return False
        also = self.reports.on_completed(confirmation) if self.reports is not None else None
        applied = self.store.set_run_state_internal(run_id, "completed", also=also)
        if applied is None or applied["state"] != "completed":
            return False                    # paused or cancelled a moment ago
        # ACCEPTANCE: attested over the tasks' own verdicts, like every other
        # terminal decision of a run.
        self._record_run_acceptance(run_id, "completed", None)
        if self.reports is not None:
            # Straight away, for the person who is waiting; a failure here is
            # made good by the next pass of the loop (``deliver_due``).
            try:
                self.reports.deliver_due(run_id=run_id)
            except Exception:  # noqa: BLE001 - the report is stored; delivery is retried
                log.exception("the report of run %s was not delivered at once", run_id)
        return True

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

    # ----------------------------------------------------------- stop signals
    def deliver_stops(self, *, limit: int = 10) -> dict[str, int]:
        """Send the stop signals that are owed and see whether they took.

        A stop is **confirmed** when the executor reports the conversation in a
        state in which it does nothing more by itself (paused, finished, error,
        stuck, deleting) or reports that it is gone. Until then the signal is
        sent again, with a pause that grows; after ``MAX_STOP_ATTEMPTS`` tries
        it is given up as ``abandoned`` and says why. A stop that is neither is
        still owed after a restart: it is a row, not a thread.
        """
        summary = {"confirmed": 0, "pending": 0, "abandoned": 0}
        stop = getattr(self.executor, "stop", None)
        observe = getattr(self.executor, "conversation_state", None)
        owed = (self.store.claim_stops(self.worker_id, limit=limit)
                if stop is not None and observe is not None else [])
        for item in owed:
            attempts = int(item.get("attempts") or 0)
            signalled = bool(item.get("signalled"))
            try:
                # Looked at first: a conversation that has already ended is not
                # told to stop, and one that was told is not told twice while
                # it is doing as it was told.
                state = observe(item)
                if state not in _STOPPED and state != "missing":
                    if not stop(item.get("openhands_account_id"),
                                str(item["conversation_id"]), item.get("project_id")):
                        raise CloudError("the executor did not take the stop signal")
                    signalled = True
                    state = observe(item)
            except CloudNotFound:
                self.store.stop_result(item["id"], signalled=True, confirmed=True,
                                       observed="missing")
                summary["confirmed"] += 1
                continue
            except Exception as e:  # noqa: BLE001 - recorded; the stop is still owed
                error = f"{type(e).__name__}: {e}"
                if attempts >= MAX_STOP_ATTEMPTS:
                    self.store.stop_result(item["id"], error=error, abandon=True)
                    summary["abandoned"] += 1
                    log.warning("stop of conversation %s abandoned after %s tries: %s",
                                item["conversation_id"], attempts, error)
                else:
                    self.store.stop_result(
                        item["id"], error=error, signalled=signalled,
                        retry_seconds=min(600.0, STOP_RETRY_SECONDS * 2 ** min(attempts - 1, 8)))
                    summary["pending"] += 1
                continue
            if state in _STOPPED or state == "missing":
                self.store.stop_result(item["id"], signalled=signalled, confirmed=True,
                                       observed=state)
                summary["confirmed"] += 1
            elif attempts >= MAX_STOP_ATTEMPTS:
                self.store.stop_result(
                    item["id"], signalled=signalled, abandon=True, observed=state,
                    error=f"the conversation still reports {state or 'no state'}")
                summary["abandoned"] += 1
            else:
                # The signal is in; the conversation has not let go yet. It is
                # looked at again shortly, and told again if it still goes on.
                self.store.stop_result(
                    item["id"], signalled=signalled, observed=state,
                    retry_seconds=STOP_OBSERVE_SECONDS,
                    error=f"signal sent; the conversation still reports {state or 'no state'}")
                summary["pending"] += 1
        # What a switch or a failover left behind in a run that has since ended
        # is stopped here: such a run has no pass of its own any more, and its
        # branch would stay held against every later run.
        ended = getattr(self.store, "ended_runs_with_abandoned", None)
        for run_id in (ended() if ended is not None else []):
            try:
                self._stop_abandoned(run_id)
            except Exception:  # noqa: BLE001 - one run must not stop the others
                log.exception("could not stop what run %s left behind", run_id)
        return summary

    def run_until_done(self, run_id: str, *, max_ticks: int = 500,
                       sleep: float = 0.0) -> TickResult:
        """Loop ticks until the run finishes, needs a person, or has nothing
        to do but wait (a durable run between two tries) - or the tick budget
        is spent."""
        last = TickResult()
        for _ in range(max_ticks):
            last = self.tick(run_id)
            if last.finished or last.idle or last.waiting:
                return last
            if sleep:
                time.sleep(sleep)
        last.note = last.note or "tick budget exhausted"
        return last
