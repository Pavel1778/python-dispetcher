"""What the long-task tests share: texts, a planning model, and what the agent got.

Nothing here is a mock of the code under test. ``PlannerModel`` answers over the
same HTTP endpoint as every other fake provider, and it answers from the prompt
it was actually sent - it can only plan the pieces it was shown - so a prompt
that lost part of the text produces a plan that visibly lacks it.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request

HEAD = "9a8b7c6d5e4f30211203f4e5d6c7b8a901234567"

_PART = re.compile(r"<<<PART\n(.*)\nPART>>>", re.S)
_PIECE = re.compile(r"\[(R-\d+)\] ")
_EARLIER = re.compile(r"^  (p\d{3}-\S+)  ", re.M)
_AT_MOST = re.compile(r"Return at most (\d+) tasks\.")
_WHICH = re.compile(r"This is part (\d+) of (\d+): lines (\d+)-(\d+)\.")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------- texts
_RULES = (
    "Система должна сохранять исходный текст задания без изменений.",
    "Сервис обязан вернуть 413, если текст больше допустимого.",
    "Добавь индекс по полю {n} и миграцию к нему.",
    "The service must reject a request without a session (case {n}).",
    "Проверь, что повторная отправка не создаёт второй прогон.",
    "Each task shall name the lines of the specification it covers.",
    "Нельзя запускать агента на части текста: либо всё, либо отказ с причиной.",
    "Fix the handler so that it never truncates the input silently.",
)
_NOTES = (
    "Справка: в прошлой версии текст обрезался на 20 000 символов.",
    "For background, see how the planner is called today.",
    "Пример ответа: {{\"status\": \"planning\", \"stages\": {n}}}.",
    "Это пояснение ничего не требует - оно только описывает контекст 🙂.",
)


def specification(chars: int, *, newline: str = "\n", seed: int = 0) -> str:
    """A specification-like text of exactly *chars* characters.

    Headings, numbered sections, lists, obligations, plain remarks, references
    to other sections, a code block, Cyrillic next to Latin and an emoji now
    and then. Every line carries its own number, so no two stretches of the
    text are alike and a part that went missing or moved cannot hide.
    """
    lines: list[str] = [f"# Техническое задание №{seed}: перенос и проверка данных", ""]
    size = sum(len(line) + len(newline) for line in lines)
    section = n = 0
    while size < chars:
        section += 1
        block = [f"## {section}. Раздел {section}: требования к блоку {section}", ""]
        for sub in range(1, 4):
            block += [f"### {section}.{sub} Подраздел {section}.{sub}", ""]
            for _ in range(6):
                n += 1
                rule = _RULES[(n + seed) % len(_RULES)].format(n=n)
                block.append(f"{n}. {rule} [п. {n}]")
                if n % 5 == 0:
                    block.append(f"   - {_NOTES[(n // 5) % len(_NOTES)].format(n=n)}")
                if n % 7 == 0 and section > 1:
                    block.append(f"   - Как сказано в разделе {section - 1}.{sub}, "
                                 f"порядок сохраняется (см. раздел {section - 1}).")
            block.append("")
            if sub == 2:
                block += ["```python", f"def check_{section}(value):  # блок {section}",
                          f"    return value == {section * 1000 + n}", "```", ""]
        lines += block
        size += sum(len(line) + len(newline) for line in block)
    text = newline.join(lines)
    assert len(text) >= chars, (len(text), chars)
    return text[:chars]


# ------------------------------------------------------------- planning model
def completion(payload: object) -> dict:
    """An OpenAI-style chat completion whose content is *payload* as JSON."""
    content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return {"choices": [{"message": {"content": content}}]}


class PlannerModel:
    """A stand-in for the planning model that reads what it is sent.

    For each request it finds the part of the specification in the prompt and
    returns tasks that between them name every piece marked in that part,
    *pieces_per_task* at a time - more at a time when that is what it takes to
    stay within the number of tasks the prompt allows (``obey_cap``). When the
    prompt lists tasks planned from earlier parts, the first task depends on
    the last of them, the way a model that understood the prompt would.

    ``faults`` are answers to give instead, one per request, first in first
    out: ``(status, payload)``. ``forget`` are piece ids the model leaves out
    of every answer; ``context`` are ids it declares to ask for nothing.
    ``too_large_over`` makes it a model with a small window: a request whose
    part of the text is more characters than that is refused with 413, the way
    a provider refuses a request it cannot read.

    Every task title carries the number of the request that produced it
    (``#7``), so a test can tell which answer a stored plan came from.
    """

    def __init__(self, *, pieces_per_task: int = 30):
        self.pieces_per_task = pieces_per_task
        #: False for a model that writes as many tasks as it likes.
        self.obey_cap = True
        self.faults: list[tuple[int, object]] = []
        self.forget: set[str] = set()
        self.context: set[str] = set()
        self.too_large_over: int | None = None
        #: Called with ``(request number, prompt)`` before answering; a test
        #: blocks here to hold a request open.
        self.before = None
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.answers: list[dict] = []
        #: ``(first line, last line)`` of the part each request was about, and
        #: the same for the requests that were answered with a plan. ``(0, 0)``
        #: stands for "the whole text".
        self.asked: list[tuple[int, int]] = []
        self.planned: list[tuple[int, int]] = []
        self.refused = 0
        self._lock = threading.Lock()

    def __call__(self, body: dict) -> tuple[int, object]:
        messages = body.get("messages") or []
        prompt = messages[-1]["content"] if messages else ""
        which = self.lines_of(prompt)
        with self._lock:
            self.prompts.append(prompt)
            number = len(self.prompts)
            self.systems.append(messages[0]["content"] if len(messages) > 1 else "")
            self.asked.append(which)
            fault = self.faults.pop(0) if self.faults else None
        if self.before is not None:
            self.before(number, prompt)
        if fault is not None:
            return fault
        if (self.too_large_over is not None
                and len(self.part_of(prompt)) > self.too_large_over):
            with self._lock:
                self.refused += 1
            return 413, {"error": {"message": "Request too large for model",
                                   "code": "request_too_large"}}
        answer = self.plan(prompt, number)
        with self._lock:
            self.answers.append(answer)
            self.planned.append(which)
        return 200, completion(answer)

    @staticmethod
    def lines_of(prompt: str) -> tuple[int, int]:
        """Which lines of the specification a planning prompt is about."""
        match = _WHICH.search(prompt.split("<<<PART")[0])
        return (int(match.group(3)), int(match.group(4))) if match else (0, 0)

    @staticmethod
    def part_of(prompt: str) -> str:
        """The annotated part of the specification a planning prompt carries."""
        match = _PART.search(prompt)
        return match.group(1) if match else ""

    def plan(self, prompt: str, number: int = 0) -> dict:
        ids = [key for key in _PIECE.findall(self.part_of(prompt))]
        wanted = [key for key in ids if key not in self.forget and key not in self.context]
        earlier = _EARLIER.findall(prompt.split("<<<PART")[0])
        per_task = self.pieces_per_task
        cap = _AT_MOST.search(prompt)
        if self.obey_cap and cap:
            per_task = max(per_task, -(-len(wanted) // int(cap.group(1))))
        tasks = []
        for i in range(0, len(wanted), per_task):
            group = wanted[i:i + per_task]
            name = f"t{len(tasks) + 1}"
            tasks.append({
                "id": name, "title": f"Выполнить пункты {group[0]}–{group[-1]} #{number}",
                "description": f"Сделать то, что требуют пункты с {group[0]} по {group[-1]}.",
                "acceptance": ["пункты выполнены", "тесты проходят"],
                "dependencies": ([f"t{len(tasks)}"] if tasks else earlier[-1:]),
                "requirements": group,
            })
        return {"tasks": tasks, "context": [key for key in ids if key in self.context]}


# ------------------------------------------------------------------ over HTTP
def post_json(studio, path: str, body: dict, *, ensure_ascii: bool = True,
              headers: dict | None = None, timeout: float = 120.0):
    """POST *body* as JSON, written with or without ``\\uXXXX`` escapes."""
    data = json.dumps(body, ensure_ascii=ensure_ascii).encode("utf-8")
    request = urllib.request.Request(studio.api.base + path, data=data, method="POST")
    request.add_header("Authorization", f"Bearer {studio.api.token}")
    request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        raw = error.read().decode()
        return error.code, (json.loads(raw) if raw else None)


def get_bytes(studio, path: str) -> tuple[int, bytes]:
    request = urllib.request.Request(studio.api.base + path)
    request.add_header("Authorization", f"Bearer {studio.api.token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def until(read, *, timeout: float = 30.0, what: str = "the condition"):
    """Poll *read* until it returns something truthy; fail with *what* if not."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


# ------------------------------------------------------------------- a project
class Lab:
    """One project wired for autonomous runs, and the questions tests ask of it.

    Everything goes over HTTP, through whichever server the stack is running
    at the moment (a test may restart it).
    """

    def __init__(self, studio, model: PlannerModel | None = None, *, repo: str,
                 mode: str = "autonomous"):
        self.studio, self.model = studio, model or PlannerModel()
        studio.llm.respond = self.model
        self.project = studio.project()
        status, body = studio.api.post(f"/api/v1/projects/{self.project}/repository",
                                       {"full_name": repo})
        assert status == 200, body
        studio.github.commits = [{"sha": HEAD, "commit": {"message": "work"}}]
        self.conversation = studio.conversation(self.project, mode=mode)

    # ------------------------------------------------------------- sending
    def send(self, text: str, *, mode: str = "autonomous", conversation: str | None = None,
             **options):
        """One chat turn. Returns ``(status, body)``."""
        return post_json(
            self.studio,
            f"/api/v1/projects/{self.project}/conversations/"
            f"{conversation or self.conversation}/messages",
            {"text": text, "mode": mode}, **options)

    def post(self, path: str, body: dict | None = None, **options):
        return post_json(self.studio, f"/api/v1/projects/{self.project}{path}",
                         body or {}, **options)

    # ------------------------------------------------------------- reading
    def get(self, path: str):
        status, body = self.studio.api.get(f"/api/v1/projects/{self.project}{path}")
        assert status == 200, (path, status, body)
        return body

    def messages(self, conversation: str | None = None) -> list[dict]:
        return self.get(f"/conversations/{conversation or self.conversation}/messages")[
            "messages"]

    def goals(self) -> list[dict]:
        return self.get("/goals")["goals"]

    def runs(self) -> list[dict]:
        return self.get("/runs")["runs"]

    def run(self, run_id: str) -> dict:
        return self.get(f"/runs/{run_id}")

    def spec(self, spec_id: str) -> dict:
        return self.get(f"/specs/{spec_id}")["spec"]

    def stages(self, spec_id: str) -> list[dict]:
        return self.get(f"/specs/{spec_id}/stages")["stages"]

    def stored(self, spec_id: str) -> bytes:
        """The text as the server kept it, over the route a person downloads it by."""
        status, data = get_bytes(
            self.studio, f"/api/v1/projects/{self.project}/specs/{spec_id}/content")
        assert status == 200, status
        return data

    def announcements(self) -> list[dict]:
        """The ``start_run`` tool messages of the conversation: one per run started."""
        return [m for m in self.messages()
                if m["role"] == "tool" and (m.get("meta") or {}).get("tool") == "start_run"]

    def announced_run(self) -> str | None:
        """The run the conversation was told about, once it has been."""
        told = self.announcements()
        return told[0]["meta"]["run_id"] if told else None

    def wait_run(self, timeout: float = 90.0) -> str:
        return until(self.announced_run, timeout=timeout, what="the run to be announced")

    def wait_spec(self, spec_id: str, state: str, timeout: float = 60.0) -> dict:
        return until(lambda: (lambda s: s if s["state"] == state else None)(self.spec(spec_id)),
                     timeout=timeout, what=f"the specification to be {state}")

    def agent(self, task_key: str, run_id: str, timeout: float = 60.0):
        """The conversation the lab created for a task, once it exists."""
        return until(lambda: conversation_of(self.studio, task_key, run_id),
                     timeout=timeout, what=f"task {task_key} to be started")


# -------------------------------------------------------- what the agent got
def conversation_of(studio, task_key: str, run_id: str | None = None):
    """The agent-server conversation the lab created for one task, if any."""
    for conversation in list(studio.sandbox.conversations.values()):
        tags = conversation.tags or {}
        if tags.get("nexustask") == task_key[:40] and (
                run_id is None or tags.get("nexusrun") == run_id[:40]):
            return conversation
    return None


def cut_inline(message: str, digest: str) -> str | None:
    """The text between the two marker lines of a first message.

    Written here from the documented format, not borrowed from the code that
    writes the message: everything after the line ``<<<NEXUS-TASK-TEXT <tag>``
    and before the line ``NEXUS-TASK-TEXT <tag>>>>``, where the tag is the
    first twelve characters of the text's SHA-256.
    """
    opening = f"<<<NEXUS-TASK-TEXT {digest[:12]}\n"
    closing = f"\nNEXUS-TASK-TEXT {digest[:12]}>>>"
    if message.count(opening) != 1 or message.count(closing) != 1:
        return None
    return message[message.index(opening) + len(opening):message.index(closing)]


def file_path_in(message: str, digest: str) -> str | None:
    """The absolute path of the specification file a first message names."""
    match = re.search(rf"(/\S*/spec-{digest[:12]}\.md)\b", message)
    return match.group(1) if match else None


def agent_bytes(conversation, digest: str) -> tuple[str, bytes | None]:
    """``(mode, bytes)``: the original text as the agent of *conversation* has it.

    ``inline`` - cut out of its first message; ``file`` - the file its first
    message names, as it was in the sandbox when the conversation was created.
    ``None`` when the agent has neither.
    """
    message = conversation.initial_message or ""
    inline = cut_inline(message, digest)
    if inline is not None:
        return "inline", inline.encode("utf-8")
    path = file_path_in(message, digest)
    if path is None:
        return "none", None
    return "file", conversation.files_at_start.get(path)
