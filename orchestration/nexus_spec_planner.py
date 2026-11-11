"""Planning a specification part by part, durably, and accounting for all of it.

A task text of any size is stored as a specification (``nexus_specs``). This
module turns one into a run:

* **By parts that fit the model.** The planning model reads a bounded number of
  tokens. The text is cut into contiguous stages sized from the *smallest*
  context window among the configured providers, so every stage can be planned
  by whichever provider is up. A short text is one stage and is planned inside
  the request, as it always was. A long one is planned in the background and
  the run starts when the last stage has a plan.
* **In document order, each part knowing the ones before it.** A stage's prompt
  carries the outline of the whole text, the tasks already planned from earlier
  parts and the gist of the sections it refers to. Its tasks may depend on
  earlier tasks by id; besides that, the first tasks of a part wait for the
  last tasks of the part before it, so a specification is carried out in the
  order it was written.
* **Durably.** A stage is a row claimed with a short lease that is renewed
  while the model is thinking. A process that dies loses nothing but that
  lease: the next pass, in this process or another, takes the stage again.
  Plans of finished stages are kept, so nothing is planned twice.
* **A stage that is too large is cut, not truncated.** When a provider says the
  request does not fit, the stage is replaced by smaller stages that cover the
  same text exactly.
* **Every piece of the text is accounted for by the application, not the
  model.** The model is asked which pieces each task covers. Whatever it leaves
  out is not dropped: an obligation nobody claimed goes into a verification
  task that this module adds to the stage, and the rest is recorded as context.
  See :func:`stage_plan`.

The model only proposes. What is stored is validated here: ids, dependencies,
no cycles, and the stages together covering the text with no gap.
"""
from __future__ import annotations

import logging
import threading
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .cloud_store import NotFound
from .nexus_conversations import ConversationService
from .nexus_lab_store import (
    DEFAULT_DEADLINE_SECONDS,
    MAX_DEADLINE_SECONDS,
    MAX_RUN_ITERATIONS,
    LabStore,
)
from .nexus_planner import validate_tasks
from .nexus_specs import (
    SpecService,
    _positive_env,
    annotate,
    build_index,
    build_stages,
    compact_keys,
    expand_keys,
    stage_rows,
)
from .providers import (
    RESPONSE_RESERVE_TOKENS,
    AllProvidersFailed,
    Provider,
    ProviderCascade,
    ProviderError,
    estimate_tokens,
    failure_kind,
)

log = logging.getLogger(__name__)

#: Tokens of specification text in one stage, unless ``NEXUS_SPEC_STAGE_TOKENS``
#: says otherwise or the model's window allows less. Small enough that a plan
#: stays about its part and its answer fits any model's output limit.
DEFAULT_STAGE_TOKENS = 24_000
#: Below this a stage is not worth a request, and is not cut further.
MIN_STAGE_TOKENS = 300
#: Room left in the model's window for the plan it writes.
PLAN_ANSWER_TOKENS = 8_000
#: The fixed words of a stage prompt, generously.
FRAME_TOKENS = 500
#: More stages than this is refused when the text arrives.
MAX_STAGES = 150
#: Tasks in one run. A run starts each task at least once and has at most
#: ``MAX_RUN_ITERATIONS`` task starts.
MAX_PLAN_TASKS = 400
#: Times one stage is tried before planning stops.
MAX_STAGE_ATTEMPTS = 6

PLANNER_SYSTEM = (
    "You are a planning component. You are given ONE PART of a specification and turn it "
    "into tasks for a coding agent. Return ONLY a JSON object of the form "
    '{"tasks": [{"id": "t1", "title": "...", "description": "...", "acceptance": ["..."], '
    '"dependencies": ["t1"], "requirements": ["R-0001", "R-0004..R-0009"]}], '
    '"context": ["R-0002"]}.\n'
    "Rules:\n"
    "1. Every piece of the part is marked with an id in square brackets, like [R-0012]. In "
    '"requirements" each task lists the ids of the pieces it carries out. Write consecutive '
    'ids as a range: "R-0004..R-0009".\n'
    '2. Every id of the part must appear in some task\'s "requirements" or in "context". '
    '"context" is only for pieces that ask for nothing to be done: an introduction, an '
    "explanation, an example. A piece you leave out is not skipped: it is handed to an extra "
    "verification task.\n"
    "3. Plan this part only. The other parts are planned separately.\n"
    '4. "dependencies" may name tasks of this plan and tasks from the list of tasks already '
    "planned for earlier parts.\n"
    "5. Use short stable ids. Write titles, descriptions and acceptance criteria in the "
    "language of the specification.\n"
    "6. The agent that performs a task can read the whole specification, so a description "
    "may be brief; it must say what to do.\n"
    '7. If the part asks for nothing to be done, return an empty "tasks" list and put every '
    'id in "context".\n'
    "Do not include any prose outside the JSON."
)


class PlanningFailed(RuntimeError):
    """A plan cannot be made from this specification as things are. The message
    says why, in words meant for the person who sent the text."""


class _Lost(Exception):
    """Someone else finished this in the meantime. Nothing to report."""


# ---------------------------------------------------------------------- sizes
@dataclass(frozen=True)
class Limits:
    """Token bounds for a stage prompt, derived from the providers' windows."""

    window: int          # the smallest context window among the providers
    context: int         # project facts
    memory: int          # verified project memory
    outline: int         # the outline of the whole specification
    earlier: int         # tasks already planned from earlier parts
    see: int             # the gist of sections this part refers to
    text: int            # the specification text of one stage


def limits_for(providers: list[Provider]) -> Limits:
    """How a stage prompt is divided, for the smallest window in *providers*.

    How much text the HTTP API accepts and how much a model reads at once are
    separate limits. This is the second one. It is never applied by cutting the
    text short: it decides into how many stages the text is divided.
    """
    if not providers:
        raise AllProvidersFailed([])
    window = min(provider.context_tokens for provider in providers)
    answer = min(PLAN_ANSWER_TOKENS, max(RESPONSE_RESERVE_TOKENS, window // 4))
    available = window - answer - estimate_tokens(PLANNER_SYSTEM) - FRAME_TOKENS

    def part(cap: int, share: float) -> int:
        return max(0, min(cap, int(available * share)))

    context, memory = part(1_200, 0.08), part(800, 0.06)
    outline, earlier, see = part(2_000, 0.12), part(1_500, 0.08), part(1_200, 0.08)
    # A fifth of what is left goes to the ids written into the text.
    text = int((available - context - memory - outline - earlier - see) * 0.8)
    text = min(text, _positive_env("NEXUS_SPEC_STAGE_TOKENS", DEFAULT_STAGE_TOKENS))
    if text < MIN_STAGE_TOKENS:
        smallest = min(providers, key=lambda provider: provider.context_tokens)
        raise PlanningFailed(
            f"the planning model reads {window} tokens at a time "
            f"({smallest.name.upper()}_CONTEXT_TOKENS), which leaves no room for the "
            "specification next to the planning instructions. Configure a provider with a "
            "larger window")
    return Limits(window=window, context=context, memory=memory, outline=outline,
                  earlier=earlier, see=see, text=text)


def _stage_task_cap(stages: int) -> int:
    """Tasks one stage may contribute, so that all stages fit one run."""
    return max(3, min(50, MAX_PLAN_TASKS // max(1, stages) - 1))


def _retry_delay(attempts: int) -> int:
    """Seconds before a stage that failed *attempts* times is tried again."""
    return min(300, 15 * 2 ** max(0, attempts - 1))


# --------------------------------------------------------------------- prompt
def _fit(lines: list[str], budget: int, *, keep_last: bool = False) -> tuple[list[str], int]:
    """The lines that fit *budget* tokens, and how many were left out."""
    kept: list[str] = []
    used = 0
    for line in (reversed(lines) if keep_last else lines):
        cost = estimate_tokens(line) + 1
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    if keep_last:
        kept.reverse()
    return kept, len(lines) - len(kept)


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def outline_lines(sections: list[dict], first_line: int, last_line: int,
                  budget: int) -> tuple[list[str], int]:
    """The outline of the specification within *budget* tokens.

    The sections of this stage are always listed; the rest are added from the
    top level down for as long as they fit, and stay in document order.
    """
    def mine(section: dict) -> bool:
        return section["start_line"] <= last_line and section["end_line"] >= first_line

    def line(section: dict) -> str:
        name = _clean(f"{section['number']} {section['title']}", 100)
        return (f"  {'>' if mine(section) else ' '} {section['key']}  lines "
                f"{section['start_line']}-{section['end_line']}  "
                f"{name or '(before the first heading)'}")

    cost = {section["ordinal"]: estimate_tokens(line(section)) + 1 for section in sections}
    chosen = {section["ordinal"] for section in sections if mine(section)}
    used = sum(cost[ordinal] for ordinal in chosen)
    for level in sorted({section["level"] for section in sections}):
        more = [s["ordinal"] for s in sections if s["level"] == level and s["ordinal"] not in chosen]
        extra = sum(cost[ordinal] for ordinal in more)
        if used + extra > budget:
            break
        chosen.update(more)
        used += extra
    listed = [section for section in sections if section["ordinal"] in chosen]
    lines, dropped = _fit([line(section) for section in listed], budget)
    return lines, len(sections) - len(listed) + dropped


def cross_references(text: str, stage: dict, pieces: list[dict], sections: list[dict],
                     links: list[dict], budget: int) -> tuple[list[dict], list[str]]:
    """Sections outside *stage* that its text refers to.

    Returns what is stored with the stage's tasks (key, title, lines) and the
    lines shown to the planner, which also carry the beginning of each section.
    """
    inside = {piece["ordinal"] for piece in pieces}
    by_ordinal = {section["ordinal"]: section for section in sections}
    seen: list[int] = []
    for link in links:
        target = by_ordinal.get(link["target_section"])
        if link["requirement"] not in inside or target is None or target["ordinal"] in seen:
            continue
        if target["start_char"] >= stage["start_char"] and target["end_char"] <= stage["end_char"]:
            continue                                   # the planner has it in front of it
        seen.append(target["ordinal"])
        if len(seen) >= 12:
            break
    refs, lines = [], []
    for ordinal in seen:
        section = by_ordinal[ordinal]
        title = _clean(f"{section['number']} {section['title']}", 100)
        refs.append({"section": section["key"], "title": title,
                     "lines": [section["start_line"], section["end_line"]]})
        gist = _clean(text[section["start_char"]:min(section["end_char"],
                                                     section["start_char"] + 1_500)], 320)
        lines.append(f"  {section['key']} \"{title}\" (lines {section['start_line']}-"
                     f"{section['end_line']}): {gist}")
    lines, _dropped = _fit(lines, budget)
    return refs, lines


def project_lines(project: dict, limits: Limits) -> list[str]:
    """What is known about the project, bounded."""
    lines: list[str] = []
    context = project.get("context") or {}
    if context:
        facts: list[str] = []
        languages = ", ".join(f"{k} ({v})" for k, v in (context.get("languages") or {}).items())
        facts.append(f"Detected languages: {languages or 'unknown'}")
        if context.get("build_commands"):
            facts.append("Build/test commands: " + "; ".join(map(str, context["build_commands"])))
        if context.get("test_file_count"):
            facts.append(f"Existing test files: {context['test_file_count']}")
        if context.get("readme_excerpt"):
            facts.append("README excerpt: " + _clean(context["readme_excerpt"], 1_500))
        lines += _fit(facts, limits.context)[0]
    verified = [m for m in (project.get("memory") or [])
                if m.get("provenance") in ("test_verified", "independently_verified")]
    facts = [f"- {_clean(m.get('content'), 400)}" for m in verified[:20] if m.get("content")]
    facts = _fit(facts, limits.memory)[0]
    if facts:
        lines += ["Verified project memory:", *facts]
    return lines


def stage_prompt(*, project: dict, spec: dict, position: int, total: int, stage: dict,
                 annotated: str, outline: tuple[list[str], int],
                 earlier: tuple[list[str], int], see: list[str], max_tasks: int,
                 limits: Limits) -> str:
    """The request for one stage's plan."""
    lines = [f"Project: {project.get('name') or ''}", *project_lines(project, limits), ""]
    lines.append(
        f"The specification is {spec['char_count']} characters in {spec['line_count']} lines: "
        f"{spec['section_count']} sections, {spec['requirement_count']} pieces.")
    if total == 1:
        lines.append("You are given all of it.")
    else:
        lines.append(f"It is planned in {total} parts, in order. This is part {position} of "
                     f"{total}: lines {stage['start_line']}-{stage['end_line']}.")
    outline_shown, outline_left = outline
    if outline_shown and (total > 1 or len(outline_shown) > 1):
        lines += ["", 'Outline of the whole specification (">" marks what is in this part):',
                  *outline_shown]
        if outline_left:
            lines.append(f"  ({outline_left} more sections are not listed)")
    earlier_shown, earlier_left = earlier
    if earlier_shown:
        lines += ["", "Tasks already planned for earlier parts. A task of this part may depend "
                      "on them by id:"]
        if earlier_left:
            lines.append(f"  ({earlier_left} older tasks are not listed)")
        lines += earlier_shown
    if see:
        lines += ["", "This part refers to sections outside it:", *see]
    lines += ["", f"Return at most {max_tasks} tasks.", "",
              "The part follows. The ids in square brackets were added for you; they are not "
              "in the original.", "<<<PART", annotated, "PART>>>"]
    return "\n".join(lines)


# ----------------------------------------------------------------- stage plan
def stage_plan(data: Any, *, pieces: list[dict], count: int, earlier_keys: set[str],
               taken: set[str], max_tasks: int, position: int, total: int) -> dict:
    """Validate a model's answer for one stage and account for every piece of it.

    *pieces* are the rows of the stage's pieces; *count* is how many pieces the
    whole specification has; *earlier_keys* are the final ids of tasks planned
    from earlier stages (*taken* is the same set as ids that cannot be reused).

    The answer's tasks are validated the way every plan is. Then each piece of
    the stage ends up in exactly one of four places:

    * ``in_tasks``               - a task of the model names it;
    * ``declared_context``       - the model says it asks for nothing;
    * ``in_verification_tasks``  - the model did not mention it and it reads
      as an obligation: this function adds a task, after all others of the
      stage, to carry out or confirm exactly those pieces;
    * ``implicit_context``       - the model did not mention it and it is plain
      prose.

    The model's silence is never taken as "nothing to do" for an obligation.
    The one exception is not an exception: a text that consists of a single
    piece is what every task of its plan is about, whether the model says so
    or not.
    Raises ``ValueError`` when the answer is not a usable plan.
    """
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise ValueError("the answer has no 'tasks' list")
    raw = data["tasks"]
    local = {task.get("id") or f"task-{i + 1}" for i, task in enumerate(raw)
             if isinstance(task, dict) and isinstance(task.get("id") or "", str)}
    prepared: list[Any] = []
    for task in raw:
        if not isinstance(task, dict):
            prepared.append(task)                      # validate_tasks names the problem
            continue
        deps = task.get("dependencies", [])
        if isinstance(deps, list) and all(isinstance(d, str) for d in deps):
            earlier = [d for d in deps if d not in local and d in earlier_keys]
            task = {**task, "dependencies": [d for d in deps if d not in earlier],
                    "earlier": list(dict.fromkeys(earlier))}
        prepared.append(task)
    tasks = (validate_tasks({"tasks": prepared}, max_tasks=max_tasks,
                            keep=("requirements", "earlier")) if prepared else [])

    in_stage = {piece["ordinal"] for piece in pieces}
    obligations = {piece["ordinal"] for piece in pieces if piece["normative"]}
    in_tasks: set[int] = set()
    ignored = 0
    for task in tasks:
        named, unknown = expand_keys(task.get("requirements"), count)
        if count == 1:
            # A text that is a single piece - a one-paragraph goal - has nothing
            # else a task could be about: every task of its plan carries it out.
            named, unknown = set(in_stage), 0
        ignored += unknown + len(named - in_stage)
        in_tasks |= named & in_stage
        task["requirements"] = compact_keys(named & in_stage, count)
        task["earlier"] = [key for key in task.get("earlier") or [] if isinstance(key, str)]
    declared, unknown = expand_keys(data.get("context"), count)
    ignored += unknown + len(declared - in_stage)
    declared = (declared & in_stage) - in_tasks
    silent = in_stage - in_tasks - declared
    unclaimed = silent & obligations

    if unclaimed:
        ids = {task["id"] for task in tasks}
        depended = {dep for task in tasks for dep in task["dependencies"]}
        key, n = "verify", 2
        while key in ids:
            key, n = f"verify-{n}", n + 1
        where = "" if total == 1 else f" части {position}"
        tasks.append({
            "id": key,
            "title": f"Сверка с ТЗ: пункты{where}, не вошедшие в задачи плана",
            "description": (
                "План не отнёс эти пункты ТЗ ни к одной задаче: "
                + ", ".join(compact_keys(unclaimed, count)) + ". "
                "Прочитай каждый из них в оригинале ТЗ. Если пункт требует работы, которая "
                "ещё не сделана, - сделай её. Если делать нечего (справка, пример, уже "
                "сделано) - так и напиши. В отчёте перечисли пункты по идентификаторам и "
                "скажи про каждый, что сделано."),
            "acceptance": [
                "В отчёте про каждый перечисленный пункт сказано, что сделано или почему "
                "делать нечего",
                "Изменения закоммичены, коммиты названы в отчёте"],
            # After everything else in the stage: its last tasks are enough.
            "dependencies": sorted(ids - depended),
            "requirements": compact_keys(unclaimed, count),
            "earlier": [],
            "verification": True,
        })

    prefix = "" if total == 1 else f"p{position:03d}-"
    for task in tasks:
        key, n = (prefix + task["id"])[:64], 2
        while key in taken:
            key, n = f"{(prefix + task['id'])[:60]}-{n}", n + 1
        taken.add(key)
        task["key"] = key
    return {
        "tasks": tasks,
        "coverage": {"pieces": len(in_stage), "in_tasks": len(in_tasks),
                     "declared_context": len(declared),
                     "in_verification_tasks": len(unclaimed),
                     "implicit_context": len(silent - obligations),
                     "ignored_ids": ignored},
    }


# ---------------------------------------------------------------------- merge
def merge(spec: dict, stages: list[dict]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One run plan out of the stages' plans, in document order.

    Checks first that the stages are all planned and cover the text exactly.
    Each task gets ``spec_ref``: which specification, which part of it (lines
    and bytes of the file the agent is given), which pieces. The first tasks of
    a stage wait for the last tasks of the stage before it.
    """
    covered = 0
    for stage in stages:
        if stage["start_char"] != covered:
            raise PlanningFailed("the stages do not cover the specification without a gap; "
                                 "the plan is discarded")
        covered = stage["end_char"]
        if stage["state"] != "planned" or not isinstance(stage.get("plan"), dict):
            raise PlanningFailed(f"lines {stage['start_line']}-{stage['end_line']} of the "
                                 "specification have no plan")
    if not stages or covered != spec["char_count"]:
        raise PlanningFailed("the stages do not reach the end of the specification; "
                             "the plan is discarded")
    total = len(stages)
    tasks: list[dict[str, Any]] = []
    known: set[str] = set()
    waits_for: list[str] = []
    summary = {"stages": total, "pieces": 0, "in_tasks": 0, "declared_context": 0,
               "in_verification_tasks": 0, "implicit_context": 0}
    for position, stage in enumerate(stages, 1):
        plan = stage["plan"]
        for name in ("pieces", "in_tasks", "declared_context", "in_verification_tasks",
                     "implicit_context"):
            summary[name] += int((plan.get("coverage") or {}).get(name, 0))
        stage_tasks = plan.get("tasks") or []
        if not stage_tasks:
            continue
        final = {task["id"]: task["key"] for task in stage_tasks}
        depended: set[str] = set()
        for task in stage_tasks:
            deps = [final[dep] for dep in task["dependencies"]]
            depended.update(deps)
            deps += [key for key in task.get("earlier") or [] if key in known and key not in deps]
            if not task["dependencies"]:
                deps += [key for key in waits_for if key not in deps]
            ref: dict[str, Any] = {
                "spec_id": spec["id"], "sha256": spec["sha256"],
                "stage": position, "stages": total,
                "lines": [stage["start_line"], stage["end_line"]],
                "bytes": [stage["start_byte"], stage["end_byte"]],
                "sections": plan.get("sections") or [],
                "requirements": task.get("requirements") or [],
            }
            if plan.get("see"):
                ref["see"] = plan["see"]
            if task.get("verification"):
                ref["verification"] = True
            tasks.append({"id": task["key"], "title": task["title"],
                          "description": task.get("description") or "",
                          "acceptance": task.get("acceptance") or [],
                          "dependencies": deps, "spec_ref": ref})
        known.update(final.values())
        waits_for = [task["key"] for task in stage_tasks if task["key"] not in depended]
    if not tasks:
        raise PlanningFailed("the planner found nothing to do in the specification: "
                             "the plan has no tasks")
    if len(tasks) > MAX_RUN_ITERATIONS:
        raise PlanningFailed(f"the plan has {len(tasks)} tasks; one run holds at most "
                             f"{MAX_RUN_ITERATIONS}")
    _check_graph(tasks)
    summary["tasks"] = len(tasks)
    return tasks, summary


def _check_graph(tasks: list[dict[str, Any]]) -> None:
    """Unique ids, known dependencies, no cycle - for the merged plan."""
    waiting = {task["id"]: len(task["dependencies"]) for task in tasks}
    if len(waiting) != len(tasks):
        raise PlanningFailed("two tasks of the plan have the same id")
    followers: dict[str, list[str]] = {key: [] for key in waiting}
    for task in tasks:
        for dep in task["dependencies"]:
            if dep not in waiting:
                raise PlanningFailed(f"task {task['id']} depends on unknown task {dep}")
            followers[dep].append(task["id"])
    ready = deque(key for key, n in waiting.items() if n == 0)
    seen = 0
    while ready:
        key = ready.popleft()
        seen += 1
        for nxt in followers[key]:
            waiting[nxt] -= 1
            if waiting[nxt] == 0:
                ready.append(nxt)
    if seen != len(tasks):
        raise PlanningFailed("the plan contains a dependency cycle")


# ------------------------------------------------------------------ heartbeat
class _Heartbeat:
    """Keep a lease alive from a side thread while the model is thinking."""

    def __init__(self, beat: Callable[[], bool], interval: float):
        self._beat, self._interval = beat, interval
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._done.wait(self._interval):
            try:
                if not self._beat():
                    return                             # the lease is someone else's now
            except Exception:  # noqa: BLE001 - a failed beat must not stop the plan
                log.exception("stage lease could not be renewed")

    def __enter__(self) -> "_Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._done.set()
        self._thread.join(timeout=5)


# -------------------------------------------------------------------- planner
class SpecPlanner:
    """Start runs from specifications; plan the long ones in the background.

    *cascade* returns the provider cascade to plan with and *describe* returns
    ``{"name", "context", "memory"}`` for ``(principal, project_id)``; both are
    read at the moment of use.

    :meth:`start` is what a request calls. :meth:`tick` is one pass of the
    background loop; any number of processes may run it.
    """

    def __init__(self, specs: SpecService, lab: LabStore, *,
                 cascade: Callable[[], ProviderCascade],
                 describe: Callable[[str, str], dict],
                 conversations: ConversationService | None = None,
                 owner: str = "planner", lease_seconds: int = 90,
                 max_stage_attempts: int = MAX_STAGE_ATTEMPTS,
                 retry_delay: Callable[[int], int] = _retry_delay):
        self.specs = specs
        self.lab = lab
        self.cascade = cascade
        self.describe = describe
        self.conversations = conversations
        self.owner = owner
        self.lease_seconds = lease_seconds
        self.max_stage_attempts = max_stage_attempts
        self.retry_delay = retry_delay
        self._wake = threading.Event()

    # ------------------------------------------------------------- requests
    def start(self, *, principal: str, project_id: str, goal_id: str, spec_id: str,
              repository: str = "", branch: str = "",
              options: dict[str, Any] | None = None) -> dict[str, Any]:
        """Start a run of *goal_id* from its specification, or say where that stands.

        Returns ``{"status": ...}``:

        * ``started``  - planned now; ``run`` is the new run and ``plan`` says
          who planned it and how the text is accounted for;
        * ``existing`` - the goal already has a live run (``run_id``). Nothing
          new was started: sending the same text twice gives one run;
        * ``planning`` - the plan is being built in stages (``stages`` is the
          progress). The run starts by itself when the last stage is planned.

        Raises :class:`~orchestration.providers.AllProvidersFailed` when no
        provider could plan, and :class:`PlanningFailed` when no plan can be
        made as things are. Either way nothing is left half-started.
        """
        providers = self.cascade().usable()
        if not providers:
            raise AllProvidersFailed([])
        limits = limits_for(providers)
        spec, text = self.specs.content_internal(spec_id)
        index = build_index(text)
        stages = build_stages(text, index, limits.text)
        if len(stages) > MAX_STAGES:
            raise PlanningFailed(
                f"at {limits.text} tokens a stage (the planning model reads {limits.window} at "
                f"a time) this specification needs {len(stages)} planning stages; the limit "
                f"is {MAX_STAGES}. Configure a provider with a larger window, or split the text")
        owner = f"{self.owner}:request:{uuid.uuid4().hex[:8]}"
        decision = self.specs.decide_start(
            spec_id, goal_id, stages=stage_rows(stages, index.sections), budget=limits.text,
            options={"principal": principal, "repository": repository, "branch": branch,
                     **(options or {})},
            owner=owner, lease_seconds=self.lease_seconds)
        kind = decision["decision"]
        if kind == "existing":
            return {"status": "existing", "run_id": decision["run_id"], "spec": spec}
        if kind == "ready":
            done = self._finish(spec_id, announce=False, reuse=True)
            if done["status"] != "lost":
                return {**done, "spec": spec}
        elif kind == "begin" and decision.get("claimed") is not None:
            done = self._plan_stage(decision["claimed"], owner=owner, interactive=True,
                                    text=text, index=index)
            if isinstance(done, dict) and done["status"] != "lost":
                return {**done, "spec": spec}
        self.wake()
        return {"status": "planning", "spec": spec,
                "stages": self.specs.progress_internal(spec_id)}

    # ----------------------------------------------------------------- loop
    def wake(self) -> None:
        """Ask the background loop to make a pass now."""
        self._wake.set()

    def wait(self, seconds: float) -> None:
        """Sleep until *seconds* pass or :meth:`wake` is called."""
        self._wake.wait(seconds)
        self._wake.clear()

    def tick(self) -> dict[str, Any]:
        """One pass: start the runs whose plans are complete, plan one stage."""
        summary: dict[str, Any] = {"started": 0, "planned": 0, "split": 0, "retry": 0,
                                   "failed": 0, "errors": []}
        for spec_id in self.specs.ready_to_finish():
            self._finish_quietly(spec_id, summary)
        stage = self.specs.claim_stage(self.owner, lease_seconds=self.lease_seconds)
        if stage is not None:
            outcome = self._plan_stage(stage, owner=self.owner, interactive=False)
            if outcome in summary:
                summary[outcome] += 1
            if outcome == "planned" and str(stage["spec_id"]) in self.specs.ready_to_finish():
                self._finish_quietly(str(stage["spec_id"]), summary)
            if outcome in ("planned", "split"):
                self.wake()                            # the next stage need not wait
        return summary

    def _finish_quietly(self, spec_id: str, summary: dict[str, Any]) -> None:
        try:
            if self._finish(spec_id, announce=True)["status"] == "started":
                summary["started"] += 1
        except PlanningFailed:
            summary["failed"] += 1                     # recorded and announced by _finish
        except Exception as e:  # noqa: BLE001 - one specification must not stop the loop
            log.exception("could not start the run of specification %s", spec_id)
            summary["errors"].append(f"{spec_id}: {type(e).__name__}: {e}")
            # Left in "planning" it would be tried again on every pass for ever.
            self._failed(spec_id, f"internal error: {type(e).__name__}: {e}", announce=True)

    # ----------------------------------------------------------- one stage
    def _plan_stage(self, stage: dict, *, owner: str, interactive: bool,
                    text: str | None = None, index: Any = None) -> Any:
        """Plan one claimed stage.

        In the background returns ``planned``, ``split``, ``retry``, ``failed``
        or ``lost``. For a request (*interactive*; the stage is the whole text)
        returns what :meth:`_finish` returns, or ``split``, and raises when the
        plan cannot be made - a request reports its failure itself.
        """
        spec_id, ordinal = str(stage["spec_id"]), stage["ordinal"]
        beat = _Heartbeat(
            lambda: self.specs.renew_stage(spec_id, ordinal, owner,
                                           lease_seconds=self.lease_seconds),
            self.lease_seconds / 3)
        try:
            with beat:
                return self._plan_claimed(stage, owner, interactive, text, index)
        except (AllProvidersFailed, PlanningFailed) as e:
            if interactive:
                raise
            return self._retry_or_fail(stage, owner, str(e), permanent=True)
        except Exception as e:  # noqa: BLE001 - turned into a recorded failure
            log.exception("planning stage %s of specification %s failed", ordinal, spec_id)
            reason = f"{type(e).__name__}: {e}"
            if interactive:
                self.specs.stage_failed(spec_id, ordinal, owner, error=reason)
                self._failed(spec_id, reason, announce=False)
                raise PlanningFailed(reason) from e
            return self._retry_or_fail(stage, owner, reason, permanent=False)

    def _plan_claimed(self, stage: dict, owner: str, interactive: bool,
                      text: str | None, index: Any) -> Any:
        spec_id, ordinal = str(stage["spec_id"]), stage["ordinal"]
        spec = self.specs.meta_internal(spec_id)
        stages = self.specs.stages_internal(spec_id)
        position = next((i for i, row in enumerate(stages, 1) if row["ordinal"] == ordinal), 0)
        if spec is None or not position:
            return "lost"
        if text is None:
            _spec, text = self.specs.content_internal(spec_id)
        total = len(stages)
        earlier = [(task["key"], task["title"]) for row in stages[:position - 1]
                   for task in ((row.get("plan") or {}).get("tasks") or [])]
        allowance = MAX_PLAN_TASKS - len(earlier) - 2 * (total - position)
        if allowance < 2:
            return self._stop(stage, owner, interactive, PlanningFailed(
                f"the plan would have more than {MAX_PLAN_TASKS} tasks; the specification is "
                "too large for one run"))
        max_tasks = max(1, min(_stage_task_cap(total), allowance - 1))

        providers = self.cascade().usable()
        try:
            limits = limits_for(providers)
        except (AllProvidersFailed, PlanningFailed) as e:
            return self._stop(stage, owner, interactive, e)
        options = spec.get("plan_options") or {}
        project = self.describe(options.get("principal") or spec["created_by"],
                                spec["project_id"])
        pieces = self.specs.requirements_internal(spec_id, stage["start_char"],
                                                  stage["end_char"])
        sections = self.specs.sections_internal(spec_id)
        refs, see = cross_references(text, stage, pieces, sections,
                                     self.specs.links_internal(spec_id), limits.see)
        prompt = stage_prompt(
            project=project, spec=spec, position=position, total=total, stage=stage,
            annotated=annotate(text, stage["start_char"], stage["end_char"], pieces),
            outline=outline_lines(sections, stage["start_line"], stage["end_line"],
                                  limits.outline),
            earlier=_fit([f"  {key}  {_clean(title, 120)}" for key, title in earlier],
                         limits.earlier, keep_last=True),
            see=see, max_tasks=max_tasks, limits=limits)
        earlier_keys = {key for key, _title in earlier}

        def validate(data: Any) -> dict:
            return stage_plan(data, pieces=pieces, count=spec["requirement_count"],
                              earlier_keys=earlier_keys, taken=set(earlier_keys),
                              max_tasks=max_tasks, position=position, total=total)

        timeout = min(180.0, 30.0 + stage["token_estimate"] / 200.0)
        try:
            plan, provider, failovers = self._ask(providers, prompt, validate, timeout)
        except AllProvidersFailed as e:
            if e.too_large:
                smaller = self._smaller(text, index, stage)
                if smaller is not None:
                    rows, budget = smaller
                    split = self.specs.split_stage(spec_id, ordinal, owner, pieces=rows,
                                                   budget=budget)
                    return "split" if split else "lost"
                if all(kind == "too_large" for kind in e.kinds):
                    # Nobody can read it and it cannot be made smaller.
                    return self._stop(stage, owner, interactive, AllProvidersFailed(
                        [*e.failures,
                         ("planner", f"lines {stage['start_line']}-{stage['end_line']} cannot "
                                     "be cut into smaller stages")], [*e.kinds, "too_large"]))
            if interactive:
                return self._stop(stage, owner, interactive, e)
            return self._retry_or_fail(stage, owner, str(e), permanent=e.permanent)

        inside = [s["key"] for s in sections
                  if s["start_char"] < stage["end_char"] and s["end_char"] > stage["start_char"]]
        plan["sections"] = inside if len(inside) <= 2 else [inside[0], inside[-1]]
        if refs:
            plan["see"] = refs
        if interactive and total == 1:
            return self._finish(spec_id, announce=False,
                                inline=(ordinal, owner, plan, provider),
                                meta={"provider": provider, "failovers": failovers})
        planned = self.specs.stage_planned(spec_id, ordinal, owner, plan=plan, provider=provider)
        return "planned" if planned else "lost"

    @staticmethod
    def _ask(providers: list[Provider], prompt: str, validate: Callable[[Any], dict],
             timeout: float) -> tuple[dict, str, list[tuple[str, str]]]:
        """The first provider whose answer is a usable plan, in cascade order.

        A provider that fails, or answers with something that is not a plan, is
        passed over; the reasons come back with the plan, or are raised
        together when nobody could.
        """
        failures: list[tuple[str, str]] = []
        kinds: list[str] = []
        for provider in providers:
            try:
                response = provider.complete(prompt, PLANNER_SYSTEM, timeout=timeout)
                return validate(response.data), provider.name, failures
            except ProviderError as e:
                failures.append((provider.name, str(e)))
                kinds.append(failure_kind(e))
            except ValueError as e:
                failures.append((provider.name, f"invalid plan: {e}"))
                kinds.append("rejected")
        raise AllProvidersFailed(failures, kinds)

    @staticmethod
    def _smaller(text: str, index: Any, stage: dict) -> tuple[list[dict], int] | None:
        """*stage* cut into at least two stages of about half its size, if it can be."""
        if stage["token_estimate"] <= MIN_STAGE_TOKENS:
            return None
        budget = max(MIN_STAGE_TOKENS, stage["token_estimate"] // 2)
        index = index or build_index(text)
        parts = build_stages(text, index, budget, start=stage["start_char"],
                             end=stage["end_char"])
        if len(parts) < 2:
            return None
        return stage_rows(parts, index.sections), budget

    def _stop(self, stage: dict, owner: str, interactive: bool, error: Exception) -> Any:
        """Planning of this specification ends here, with *error* as the reason."""
        spec_id, ordinal = str(stage["spec_id"]), stage["ordinal"]
        if interactive:
            self.specs.stage_failed(spec_id, ordinal, owner, error=str(error))
            self._failed(spec_id, str(error), announce=False)
            raise error
        return self._retry_or_fail(stage, owner, str(error), permanent=True)

    def _retry_or_fail(self, stage: dict, owner: str, reason: str, *, permanent: bool) -> str:
        spec_id, ordinal = str(stage["spec_id"]), stage["ordinal"]
        if not permanent and stage["attempts"] < self.max_stage_attempts:
            self.specs.stage_retry(spec_id, ordinal, owner, error=reason,
                                   delay_seconds=self.retry_delay(stage["attempts"]))
            return "retry"
        if not self.specs.stage_failed(spec_id, ordinal, owner, error=reason):
            return "lost"
        tried = "" if permanent else f" (после {stage['attempts']} попыток)"
        self._failed(spec_id, f"строки {stage['start_line']}-{stage['end_line']}{tried}: "
                              f"{reason}", announce=True)
        return "failed"

    # ------------------------------------------------------------- the run
    def _finish(self, spec_id: str, *, announce: bool, reuse: bool = False,
                inline: tuple[int, str, dict, str] | None = None,
                meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """Merge the stage plans and create the run, as one fact.

        The run, its tasks, "this specification has its run" and - for a plan
        that was built in the background - the chat message that says so are
        written in one transaction. *inline* is a stage planned by the request
        itself: its plan is recorded in that same transaction, so a plan that is
        complete and a run that does not exist yet are never visible together.

        Returns ``{"status": "started", ...}``, ``{"status": "existing", ...}``
        when the goal already has a live run, or ``{"status": "lost"}`` when
        another process finished first. Raises :class:`PlanningFailed`.
        """
        spec = self.specs.meta_internal(spec_id)
        if spec is None or not spec.get("goal_id"):
            raise PlanningFailed("the specification or its goal no longer exists")
        stages = self.specs.stages_internal(spec_id)
        if inline is not None:
            ordinal, owner, plan, provider = inline
            stages = [{**row, "state": "planned", "plan": plan, "provider": provider}
                      if row["ordinal"] == ordinal else row for row in stages]
        states = ("planned", "failed") if reuse else ("planning",)
        try:
            tasks, coverage = merge(spec, stages)
        except PlanningFailed as e:
            if inline is not None:
                self.specs.stage_failed(spec_id, inline[0], inline[1], error=str(e))
            self._failed(spec_id, str(e), announce=announce, discard=True, while_in=states)
            raise
        options = spec.get("plan_options") or {}
        principal = options.get("principal") or spec["created_by"]
        repository, branch = options.get("repository") or "", options.get("branch") or ""
        providers = sorted({row["provider"] for row in stages if row.get("provider")})
        plan_meta = {"provider": (meta or {}).get("provider") or ", ".join(providers),
                     "failovers": (meta or {}).get("failovers") or [],
                     "task_count": len(tasks), "stages": len(stages), "coverage": coverage}

        def also(cur: Any, run: dict) -> None:
            if inline is not None and not SpecService.stage_planned_in(
                    cur, spec_id, inline[0], inline[1], plan=inline[2], provider=inline[3]):
                raise _Lost()
            done = (SpecService.reuse_in(cur, spec_id, run["id"]) if reuse
                    else SpecService.finish_in(cur, spec_id, run["id"]))
            if not done:
                raise _Lost()
            if announce:
                self._announce_run(cur, spec, run, plan_meta, repository, branch)

        try:
            run = self.lab.start_run(
                principal, spec["project_id"], spec["goal_id"],
                max_iterations=options.get("max_iterations") or min(
                    MAX_RUN_ITERATIONS, max(50, 2 * len(tasks))),
                max_attempts_per_task=options.get("max_attempts_per_task") or 3,
                deadline_seconds=options.get("deadline_seconds") or min(
                    MAX_DEADLINE_SECONDS, max(DEFAULT_DEADLINE_SECONDS, 3600 * len(tasks))),
                plan=tasks, repository=repository, branch=branch, also=also)
        except _Lost:
            return {"status": "lost"}
        except NotFound:
            reason = (f"the project or the goal no longer exists, or {principal} is no longer "
                      "a member of the project")
            self._failed(spec_id, reason, announce=announce, while_in=states)
            raise PlanningFailed(reason) from None
        except ValueError as e:
            live = self.lab.active_run_internal(spec["goal_id"])
            if live is not None:
                # The goal was started some other way while this plan was being
                # made. That run is the goal's run; a second one is not started.
                self.specs.finish(spec_id, str(live["id"]))
                return {"status": "existing", "run_id": str(live["id"])}
            self._failed(spec_id, str(e), announce=announce, while_in=states)
            raise PlanningFailed(str(e)) from None
        return {"status": "started", "run": run, "plan": plan_meta}

    # --------------------------------------------------------- telling the chat
    def _announce_run(self, cur: Any, spec: dict, run: dict, plan: dict,
                      repository: str, branch: str) -> None:
        """Tell the chat the run has started, in the transaction that starts it."""
        conversation_id = spec.get("conversation_id")
        if not conversation_id:
            return
        cur.execute("SELECT 1 FROM nexus_conversations WHERE id=%s FOR KEY SHARE",
                    (conversation_id,))
        if cur.fetchone() is None:
            return
        run_id = str(run["id"])
        ConversationService.append_internal(
            cur, conversation_id, role="tool", content=f"start_run → {run_id}",
            meta={"tool": "start_run", "run_id": run_id, "tasks": plan["task_count"],
                  "repository": repository, "branch": branch, "spec_id": spec["id"],
                  "stages": plan["stages"], "coverage": plan["coverage"]})
        ConversationService.append_internal(
            cur, conversation_id, role="assistant", run_id=run_id, state="idle",
            content=(f"План построен по всему ТЗ: частей {plan['stages']}, задач в плане: "
                     f"{plan['task_count']}. Автономный прогон запущен: репозиторий "
                     f"`{repository}`, ветка `{branch}`.\n\n"
                     "Ход работы и проверки видны на вкладке «Агент»."),
            meta={"mode": "autonomous", "run_id": run_id, "spec_id": spec["id"]})

    def _failed(self, spec_id: str, reason: str, *, announce: bool, discard: bool = False,
                while_in: tuple[str, ...] = ("planning",)) -> None:
        """Record that planning stopped and, for background work, tell the chat."""
        changed = self.specs.fail(spec_id, reason, discard_plans=discard, while_in=while_in)
        if not (changed and announce and self.conversations is not None):
            return
        spec = self.specs.meta_internal(spec_id)
        if spec is None or not spec.get("conversation_id"):
            return
        again = ("План придётся строить заново." if discard
                 else "Готовые части плана сохранены и заново не строятся.")
        try:
            self.conversations.post_internal(spec["conversation_id"], [{
                "role": "system",
                "content": (f"Не удалось построить план по ТЗ: {reason}\n\n"
                            f"Текст ТЗ сохранён. Отправьте его ещё раз, чтобы повторить. {again}"),
                "meta": {"error": True, "spec_id": spec_id}}], state="error")
        except Exception:  # noqa: BLE001 - the failure itself is already recorded
            log.exception("could not tell the conversation about specification %s", spec_id)
