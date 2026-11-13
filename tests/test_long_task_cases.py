"""What can go wrong with a long task text, and what the service does about it.

``tests/test_long_task.py`` shows that every size gets through. These tests are
about everything else the fix has to hold under: characters that are easy to
lose, the same text sent twice, a provider that fails or cannot read a part, a
model that leaves obligations out, a sandbox that will not take the file.

Everything runs over HTTP on the real server and real PostgreSQL, with the
direct OpenHands transport against the socket fakes and a planning model that
answers from the prompt it is actually sent (``tests/long_task_kit.py``).
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.long_task_kit import (  # noqa: E402
    HEAD,
    Lab,
    agent_bytes,
    completion,
    get_bytes,
    sha256,
    specification,
    until,
)
from tests.test_studio_api import DSN, REPO, Api, studio_stack  # noqa: E402

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run long-task tests")

#: With stages of 3 000 tokens the 30 000-character text below is planned in
#: these five parts (first line, last line) - long enough to be planned in the
#: background and delivered as a file, short enough to be quick.
STAGED_CHARS = 30_000
PARTS = [(1, 149), (150, 293), (294, 441), (442, 590), (591, 627)]


@pytest.fixture
def lab(monkeypatch):
    monkeypatch.setenv("NEXUS_SPEC_STAGE_TOKENS", "3000")
    with studio_stack(monkeypatch, transport="direct", lab_inline=True) as studio:
        # A failed stage is tried again at once instead of after 15 s and more.
        studio.app.spec_planner.retry_delay = lambda attempts: 0
        yield Lab(studio, repo=REPO)


def spec_of(reply: dict) -> str:
    """The id of the specification a chat turn's text was stored as."""
    return reply["messages"][0]["meta"]["spec"]["id"]


def kinds(messages: list[dict]) -> list[str]:
    """What each message of a transcript is, in a word."""
    out = []
    for message in messages:
        meta = message.get("meta") or {}
        if message["role"] == "assistant":
            out.append("planning" if "planning" in meta else
                       "existing" if meta.get("existing") else
                       "started" if meta.get("run_id") else "assistant")
        elif message["role"] == "system":
            out.append("error" if meta.get("error") else "system")
        else:
            out.append(message["role"])
    return out


# ------------------------------------------------------- nothing is altered
#: Texts that are easy to damage. Each is sent as it is and must come back,
#: and reach the agent, as the same bytes.
EXACT = {
    # Blank lines and indentation in front, spaces and a tab behind.
    "whitespace-around": (
        "\n\n \t  1. Система должна сохранить пробелы в начале текста.\n"
        "\t2. Проверь, что табуляция в конце строки осталась.\t \n\n   \n"),
    # Windows line ends, with a bare CR and a bare LF among them.
    "crlf": (
        "# ТЗ\r\n\r\n1. Сервис обязан принять CRLF как есть.\r\n"
        "2. Проверь, что \\r\\n не стал \\n.\r\n\r\n"
        "Одинокий \r возврат каретки и обычный \n перевод строки.\r\n"),
    # Astral characters, a ZWJ emoji sequence, the same letter composed and
    # decomposed, right-to-left text, invisible characters, line separators
    # that are not "\n", and the ends of the code space.
    "unicode": (
        "\ufeff1. Система должна сохранить 𝔘𝔫𝔦𝔠𝔬𝔡𝔢 🚀 и 👩🏽\u200d💻 целиком.\n"
        "2. Проверь: é (U+00E9) и e\u0301 (e + U+0301) - разные байты, не нормализовать.\n"
        "3. Добавь שלום עולם и مرحبا بالعالم \u202eсправа налево\u202c без перестановки.\n"
        "4. Невидимые: [\u200b] [\u200c] [\u200d] [\u00a0] [\u2060] [\u00ad].\n"
        "5. Разделители строк, которые не \\n: [\u2028] [\u2029] [\x0b] [\x0c] [\x85] [\x1c].\n"
        "6. Края: [\x01] [\x7f] [\ud7ff] [\ue000] [\ufffd] [\uffff] [\U00010000] [\U0010ffff].\n"
        "7. 中文、日本語、한국어、ไทย、हिन्दी - тоже должны дойти.\n"),
    # The same two things at a size that travels as a file.
    "crlf-file": specification(45_000, newline="\r\n", seed=3),
    "whitespace-file": "\r\n \t\n" + specification(21_000, seed=4) + "\n \t \r\n\n",
}


@pytest.mark.parametrize("escaped", [True, False], ids=["escaped-json", "raw-utf8-json"])
@pytest.mark.parametrize("name", sorted(EXACT))
def test_the_text_is_stored_and_delivered_as_the_bytes_that_were_sent(lab, name, escaped):
    text = EXACT[name]
    data, digest = text.encode("utf-8"), sha256(text)

    status, reply = lab.send(text, ensure_ascii=escaped)
    assert status == 200, reply
    told = reply["messages"][0]["meta"]["spec"]
    assert (told["chars"], told["bytes"], told["sha256"]) == (len(text), len(data), digest)

    # Stored: the route a person downloads the text by returns the same bytes.
    assert lab.stored(told["id"]) == data
    # Line numbers are the ones `sed -n` shows: a line ends at "\n" only.
    assert told["lines"] == text.count("\n") + (0 if text.endswith("\n") else 1)

    # Delivered: the first agent has the same bytes, in its message or as a file.
    run_id = lab.wait_run()
    first = lab.run(run_id)["tasks"][0]
    assert first["spec_ref"]["sha256"] == digest
    mode, got = agent_bytes(lab.agent(first["task_key"], run_id), digest)
    assert mode == ("inline" if len(text) <= 20_000 else "file")
    assert got == data


@pytest.mark.parametrize("mode", ["autonomous", "ask", "plan", "code"])
def test_a_text_that_cannot_be_kept_exactly_is_refused_and_changes_nothing(lab, mode):
    """Nothing is repaired silently. A text that would have to be altered to be
    stored is refused, with the position of the problem, and the refusal leaves
    no trace: no message, no title, no goal, no specification."""
    conversation = lab.studio.conversation(lab.project, mode="ask")
    path = f"/api/v1/projects/{lab.project}/conversations/{conversation}/messages"

    def send_raw(text_json: bytes):
        import json
        import urllib.error
        import urllib.request
        request = urllib.request.Request(
            lab.studio.api.base + path, method="POST",
            data=b'{"mode": "%s", "text": %s}' % (mode.encode(), text_json))
        request.add_header("Authorization", f"Bearer {lab.studio.api.token}")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    refused = {
        b'"a\\u0000b: do the work"': "NUL character (U+0000) at position 1",
        b'"' + b"x" * 300 + b'\\u0000 tail"': "NUL character (U+0000) at position 300",
        b'"a\\ud83d b"': "unpaired surrogate (U+D83D) at position 1",
        b'"' + b"x" * 300 + b'\\udc00 tail"': "unpaired surrogate (U+DC00) at position 300",
        b'"  \\n\\t "': "text is required",
        b'""': "text is required",
        b"5": "text must be a string",
        b"null": "text is required",
    }
    for raw, reason in refused.items():
        status, body = send_raw(raw)
        assert status == 400, (raw[:30], status, body)
        assert reason in body["error"], (raw[:30], body)

    assert lab.messages(conversation) == []
    after = lab.get(f"/conversations/{conversation}")["conversation"]
    assert (after["title"], after["mode"], after["state"]) == ("", "ask", "idle")
    assert lab.goals() == [] and lab.runs() == []
    assert lab.model.prompts == []


# ------------------------------------------------------- the same text twice
def test_the_same_text_sent_again_starts_nothing_new(lab):
    text = "  Цель: покрыть модуль тестами.\n"
    status, first = lab.send(text)
    assert status == 200, first
    assert kinds(first["messages"]) == ["user", "tool", "started"]
    run_id = first["messages"][2]["meta"]["run_id"]

    status, second = lab.send(text)
    assert status == 200, second
    assert kinds(second["messages"]) == ["user", "existing"]
    assert second["messages"][1]["meta"]["run_id"] == run_id
    assert spec_of(second) == spec_of(first)

    assert [g["spec_id"] for g in lab.goals()] == [spec_of(first)]
    assert [r["id"] for r in lab.runs()] == [run_id]
    assert len(lab.model.prompts) == 1, "the plan was asked for a second time"

    # A different text is a different task.
    status, other = lab.send(text + "И ещё одно требование.")
    assert status == 200, other
    assert kinds(other["messages"]) == ["user", "tool", "started"]
    assert len(lab.goals()) == 2 and len(lab.runs()) == 2


def test_a_double_click_on_a_short_text_is_one_run(lab):
    """Two identical requests at once: one plans, the other is told so."""
    reached, release = threading.Event(), threading.Event()

    def hold(_number, _prompt):
        reached.set()
        release.wait(30)

    lab.model.before = hold
    text = "Цель: покрыть модуль тестами."
    answers: dict[str, tuple] = {}
    first = threading.Thread(target=lambda: answers.update(first=lab.send(text)))
    first.start()
    try:
        assert reached.wait(15), "the first request never asked for a plan"
        # The first request is inside its planning call. The second one must
        # neither wait for it nor plan by itself.
        status, second = lab.send(text)
        assert status == 200, second
        assert kinds(second["messages"]) == ["user", "planning"]
        assert first.is_alive(), "the second request waited for the first"
    finally:
        release.set()
        first.join(30)
    status, reply = answers["first"]
    assert status == 200, reply
    assert kinds(reply["messages"]) == ["user", "tool", "started"]

    assert len(lab.goals()) == 1
    assert [r["id"] for r in lab.runs()] == [reply["messages"][2]["meta"]["run_id"]]
    assert len(lab.model.prompts) == 1
    time.sleep(0.6)                                     # a few passes of the planning loop
    assert len(lab.model.prompts) == 1 and len(lab.runs()) == 1
    assert len(lab.announcements()) == 1


def test_a_resend_while_the_plan_is_being_built_does_not_plan_twice(lab):
    reached, release = threading.Event(), threading.Event()

    def hold(number, _prompt):
        if number == 2:                                # the second part's request
            reached.set()
            release.wait(30)

    lab.model.before = hold
    text = specification(STAGED_CHARS)
    try:
        status, first = lab.send(text)
        assert status == 200, first
        assert kinds(first["messages"]) == ["user", "planning"]
        assert first["messages"][1]["meta"]["planning"]["total"] == len(PARTS)
        spec_id = spec_of(first)
        assert reached.wait(15), "planning never reached the second part"

        status, again = lab.send(text)
        assert status == 200, again
        assert kinds(again["messages"]) == ["user", "planning"]
        assert spec_of(again) == spec_id
        progress = again["messages"][1]["meta"]["planning"]
        assert (progress["total"], progress["planned"]) == (len(PARTS), 1)
        assert lab.runs() == [] and len(lab.goals()) == 1
    finally:
        release.set()

    run_id = lab.wait_run()
    assert lab.model.planned == PARTS, "a part was planned twice, or out of order"
    assert [g["spec_id"] for g in lab.goals()] == [spec_id]
    assert [r["id"] for r in lab.runs()] == [run_id]
    assert len(lab.announcements()) == 1

    # And once the run exists the same text is simply "already running".
    status, third = lab.send(text)
    assert status == 200, third
    assert kinds(third["messages"]) == ["user", "existing"]
    assert third["messages"][1]["meta"]["run_id"] == run_id
    assert lab.model.planned == PARTS and len(lab.runs()) == 1
    assert kinds(lab.messages()) == ["user", "planning", "user", "planning", "tool", "started",
                                     "user", "existing"]


def _gone():
    from orchestration.cloud_store import StoreUnavailable

    raise StoreUnavailable("this process has been stopped")


def test_a_restart_while_the_plan_is_being_built_goes_on_from_the_part_it_was_on(lab):
    """The service stops with the plan half built - a deploy, a crash, a free
    instance put to sleep - while the model is thinking about the third part.
    What was planned is in PostgreSQL and nowhere else. The next process plans
    the third part again (the answer to the first request never arrived), then
    the rest: no part that had a plan is planned twice, and one run comes out."""
    studio = lab.studio
    reached, release = threading.Event(), threading.Event()

    def hold(number, _prompt):
        if number == 3:                                # the third part, asked by the first process
            reached.set()
            release.wait(60)

    lab.model.before = hold
    lab.model.pieces_per_task = 400                    # one task per part
    # The claim of a planner that is gone is free again after seconds, not 90.
    studio.app.spec_planner.lease_seconds = 3
    text = specification(STAGED_CHARS)
    try:
        status, first = lab.send(text)
        assert status == 200, first
        assert kinds(first["messages"]) == ["user", "planning"]
        spec_id = spec_of(first)
        assert reached.wait(15), "planning never reached the third part"
        assert [stage["state"] for stage in lab.stages(spec_id)] == [
            "planned", "planned", "planning", "pending", "pending"]

        # A real stop ends the process and its planning thread with it. Here
        # the "process" is threads of this one and that thread is still inside
        # its request, so its database is taken away: once the server has
        # stopped, nothing more is written on its behalf.
        studio.app.store._reconnect = _gone
        studio.server.stop()                           # with the third request still open
        app = studio.boot()
        app.spec_planner.retry_delay = lambda attempts: 0
        assert lab.runs() == [] and len(lab.goals()) == 1
        run_id = lab.wait_run()
    finally:
        release.set()

    # Asked once per part, and once more for the part whose answer was lost.
    assert lab.model.asked == [PARTS[0], PARTS[1], PARTS[2], PARTS[2], PARTS[3], PARTS[4]]
    stages = lab.stages(spec_id)
    assert [stage["state"] for stage in stages] == ["planned"] * len(PARTS)
    assert [stage["attempts"] for stage in stages] == [1, 1, 2, 1, 1]
    assert lab.spec(spec_id)["state"] == "planned"

    # The plan is the two parts the first process kept and the three the second
    # one made: the third part's tasks are the answer to request 4, not 3.
    tasks = lab.run(run_id)["tasks"]
    assert [task["task_key"] for task in tasks] == [f"p00{n}-t1" for n in range(1, 6)]
    assert [tuple(task["spec_ref"]["lines"]) for task in tasks] == PARTS
    assert [task["title"].rsplit("#", 1)[1] for task in tasks] == ["1", "2", "4", "5", "6"]
    assert [run["id"] for run in lab.runs()] == [run_id] and len(lab.goals()) == 1
    assert len(lab.announcements()) == 1
    assert kinds(lab.messages()) == ["user", "planning", "tool", "started"]

    # And the new process hands the text over as the first would have.
    data, digest = text.encode("utf-8"), sha256(text)
    assert agent_bytes(lab.agent("p001-t1", run_id), digest) == ("file", data)


def test_after_the_run_has_ended_the_same_text_runs_again_from_the_same_plan(lab):
    text = specification(STAGED_CHARS)
    status, first = lab.send(text)
    assert status == 200, first
    run_id = lab.wait_run()
    tasks = [(t["task_key"], t["title"]) for t in lab.run(run_id)["tasks"]]
    asked = len(lab.model.prompts)

    status, body = lab.post(f"/runs/{run_id}/cancel")
    assert status == 200 and body["run"]["state"] == "cancelled", body

    status, again = lab.send(text)
    assert status == 200, again
    assert kinds(again["messages"]) == ["user", "tool", "started"]
    second = again["messages"][2]["meta"]["run_id"]
    assert second != run_id
    assert [(t["task_key"], t["title"]) for t in lab.run(second)["tasks"]] == tasks
    assert len(lab.model.prompts) == asked, "a finished plan was asked for again"
    assert len(lab.goals()) == 1
    # The cancelled run has left the list (the interface shows what is at work)
    # and is still there to be read by its id, with everything it recorded.
    assert [r["state"] for r in lab.runs()] == ["running"]
    assert lab.run(run_id)["run"]["state"] == "cancelled"
    assert lab.spec(spec_of(first))["run_id"] == second


def test_the_same_text_in_another_conversation_is_another_task(lab):
    """What makes two sends "the same" is the conversation and the text. The
    same words in a new conversation are a new request, on purpose."""
    text = "Цель: покрыть модуль тестами."
    assert lab.send(text)[0] == 200
    elsewhere = lab.studio.conversation(lab.project, mode="autonomous")
    status, reply = lab.send(text, conversation=elsewhere)
    assert status == 200, reply
    assert kinds(reply["messages"]) == ["user", "tool", "started"]
    assert len(lab.goals()) == 2 and len(lab.runs()) == 2


# ------------------------------------------------------------------ over REST
def test_a_long_goal_over_rest_is_planned_in_the_background(lab):
    text = "\n" + specification(STAGED_CHARS) + "  \n"
    status, body = lab.post("/goals", {"text": text})
    assert status == 201, body
    goal = body["goal"]
    # The goal row keeps a label - the beginning of the text - and points at
    # the specification, which is the text.
    assert goal["text"] == text.strip()[:2000].rstrip() + "…"
    spec_id = goal["spec_id"]
    assert lab.stored(spec_id) == text.encode("utf-8")
    assert lab.spec(spec_id)["state"] == "indexed"

    status, body = lab.post(f"/goals/{goal['id']}/run")
    assert status == 202, body
    assert body["status"] == "planning" and body["spec"]["id"] == spec_id
    assert body["planning"]["total"] == len(PARTS)

    spec = lab.wait_spec(spec_id, "planned")
    run_id = spec["run_id"]
    assert [r["id"] for r in lab.runs()] == [run_id]
    assert lab.run(run_id)["run"]["goal_id"] == goal["id"]

    # Asking again answers with the run, not with a second plan.
    status, body = lab.post(f"/goals/{goal['id']}/run")
    assert (status, body["run_id"]) == (409, run_id), body
    assert len(lab.model.planned) == len(PARTS)

    first = lab.run(run_id)["tasks"][0]
    mode, got = agent_bytes(lab.agent(first["task_key"], run_id), sha256(text))
    assert (mode, got) == ("file", text.encode("utf-8"))


def test_a_short_goal_over_rest_starts_at_once_and_is_not_trimmed(lab):
    text = "  \tЦель: покрыть модуль тестами.\r\n"
    status, body = lab.post("/goals", {"text": text})
    assert status == 201, body
    goal = body["goal"]
    assert goal["text"] == text.strip()
    assert lab.stored(goal["spec_id"]) == text.encode("utf-8")

    status, body = lab.post(f"/goals/{goal['id']}/run")
    assert status == 201, body
    assert body["spec"]["sha256"] == sha256(text)
    run_id = body["run"]["id"]
    assert [t["task_key"] for t in body["tasks"]] == [
        t["task_key"] for t in lab.run(run_id)["tasks"]]
    mode, got = agent_bytes(lab.agent(body["tasks"][0]["task_key"], run_id), sha256(text))
    assert (mode, got) == ("inline", text.encode("utf-8"))

    status, body = lab.post(f"/goals/{goal['id']}/run")
    assert (status, body.get("run_id")) == (409, run_id), body


def test_what_is_too_much_is_refused_with_413_and_both_numbers(lab, monkeypatch):
    monkeypatch.setenv("NEXUS_SPEC_MAX_CHARS", "5000")
    exactly, over = specification(5_000), specification(5_001)

    status, body = lab.send(over)
    assert status == 413, body
    assert "5001" in body["error"] and "5000" in body["error"]
    assert "NEXUS_SPEC_MAX_CHARS" in body["error"]
    status, body = lab.post("/goals", {"text": over})
    assert status == 413 and "5001" in body["error"], body
    # Refused means nothing was kept and nobody was asked for a plan.
    assert lab.messages() == [] and lab.goals() == [] and lab.model.prompts == []

    # The limit itself is accepted - by both doors.
    status, body = lab.send(exactly)
    assert status == 200, body
    assert lab.stored(spec_of(body)) == exactly.encode("utf-8")
    status, body = lab.post("/goals", {"text": exactly + ""})
    assert status == 201, body

    # A body larger than any accepted text can be is refused before it is read.
    monkeypatch.delenv("NEXUS_SPEC_MAX_CHARS")
    status, body = lab.send("x" * 1_000_001)
    assert status == 413 and "1000001" in body["error"] and "1000000" in body["error"], body
    huge = "x" * (12 * 1_000_000 + 64 * 1024 + 1)
    status, body = lab.send(huge)
    assert status == 413 and "body" in body["error"], body


def test_the_specification_routes_show_the_text_and_what_was_made_of_it(lab):
    text = specification(STAGED_CHARS)
    status, reply = lab.send(text)
    assert status == 200, reply
    spec_id = spec_of(reply)
    run_id = lab.wait_run()

    spec = lab.spec(spec_id)
    assert (spec["char_count"], spec["sha256"], spec["state"], spec["run_id"]) == (
        len(text), sha256(text), "planned", run_id)
    assert spec["stages"] == {"total": 5, "planned": 5, "planning": 0, "pending": 0, "failed": 0}
    covered = spec["coverage"]
    assert covered["pieces"] == spec["requirement_count"]
    assert covered["in_tasks"] + covered["declared_context"] + covered[
        "implicit_context"] + covered["in_verification_tasks"] == covered["pieces"]

    base = f"/api/v1/projects/{lab.project}/specs/{spec_id}"
    status, data = get_bytes(lab.studio, f"{base}/content")
    assert status == 200 and data == text.encode("utf-8")

    # The outline and the pieces address the text by line; between them the
    # pieces hold every line that is not blank, each exactly once.
    sections = lab.get(f"/specs/{spec_id}/sections")["sections"]
    assert len(sections) == spec["section_count"]
    assert sections[0]["start_line"] == 1 and sections[-1]["end_line"] == spec["line_count"]
    pieces: list[dict] = []
    while True:
        page = lab.get(f"/specs/{spec_id}/requirements?offset={len(pieces)}&limit=200")
        pieces += page["requirements"]
        if len(page["requirements"]) < 200:
            break
    assert len(pieces) == spec["requirement_count"]
    lines = text.split("\n")
    held = [n for piece in pieces for n in range(piece["start_line"], piece["end_line"] + 1)]
    assert len(held) == len(set(held))
    assert {n for n, line in enumerate(lines, 1) if line.strip()} <= set(held)

    stages = lab.stages(spec_id)
    assert [(s["start_line"], s["end_line"]) for s in stages] == PARTS
    assert all(s["state"] == "planned" and s["plan"]["tasks"] for s in stages)

    # Somebody who is not a member of the project is told none of it exists.
    stranger = Api(lab.studio.api.base, token=None)
    for part in ("", "/content", "/sections", "/requirements", "/stages"):
        assert stranger.get(base + part)[0] == 401
    other = lab.studio.project("Другой проект")
    for part in ("", "/content", "/sections", "/requirements", "/stages"):
        status, body = lab.studio.api.get(f"/api/v1/projects/{other}/specs/{spec_id}{part}")
        assert status == 404, (part, status, body)
    for bad in ("not-a-uuid", "00000000-0000-4000-8000-000000000000"):
        assert lab.studio.api.get(f"/api/v1/projects/{lab.project}/specs/{bad}")[0] == 404
    status, body = lab.studio.api.get(f"{base}/requirements?offset=x")
    assert status == 400, body


def test_every_task_of_every_part_is_given_the_whole_text_and_the_run_completes(lab):
    """Not only the first agent: each task, whichever part it was planned from,
    starts with the same file, and is told which lines of it are its own."""
    lab.model.pieces_per_task = 400                     # one task per part
    text = specification(STAGED_CHARS)
    data, digest = text.encode("utf-8"), sha256(text)
    status, reply = lab.send(text)
    assert status == 200, reply
    run_id = lab.wait_run()
    tasks = lab.run(run_id)["tasks"]
    assert [t["task_key"] for t in tasks] == [f"p00{n}-t1" for n in range(1, 6)]
    assert [tuple(t["spec_ref"]["lines"]) for t in tasks] == PARTS

    for position, task in enumerate(tasks, 1):
        conversation = lab.agent(task["task_key"], run_id)
        mode, got = agent_bytes(conversation, digest)
        assert (mode, got) == ("file", data), task["task_key"]
        message = conversation.initial_message
        first, last = PARTS[position - 1]
        assert f"This step is part {position} of 5." in message
        assert f"sed -n '{first},{last}p' /workspace/.nexus/spec-{digest[:12]}.md" in message
        assert f"SHA-256 {digest}" in message
        # The byte range the message gives is those lines of the text, exactly.
        start, end = task["spec_ref"]["bytes"]
        assert f"bytes {start}-{end}:" in message
        lines = text.split("\n")
        assert data[start:end] == ("\n".join(lines[first - 1:last])
                                   + ("\n" if last < len(lines) else "")).encode("utf-8")
        # Later parts wait for earlier ones: nothing else has been started.
        assert len(lab.studio.sandbox.conversations) == position
        lab.studio.sandbox.finish(conversation.id, f"Готово. Коммит {HEAD}.")

    done = until(lambda: (lambda d: d if d["run"]["state"] != "running" else None)(
        lab.run(run_id)), timeout=30, what="the run to finish")
    assert done["run"]["state"] == "completed", done["run"]
    assert {t["state"] for t in done["tasks"]} == {"completed_verified"}
    # What was delivered is on record with each task.
    for task in done["tasks"]:
        proof = task["spec_ref"]["delivery"]
        assert (proof["mode"], proof["sha256"], proof["bytes"]) == ("file", digest, len(data))
        assert proof["files"][0] == {"path": f"/workspace/.nexus/spec-{digest[:12]}.md",
                                     "bytes": len(data), "sha256": digest}


# ------------------------------------------------------- the provider fails
def conversation_state(lab) -> str:
    return lab.get(f"/conversations/{lab.conversation}")["conversation"]["state"]


def test_planning_gets_through_a_provider_that_fails_and_answers_nonsense(lab):
    """Outages, rate limits and answers that are not a plan cost attempts, not
    the plan: the part is asked for again and nothing already planned is lost."""
    lab.model.faults = [
        (500, {}), (500, {}), (500, {}),        # one planning attempt: the provider's own retries
        (429, {}), (429, {}), (429, {}),        # a second one, rate limited
        (200, completion("this is not JSON")),  # a third: an answer that is not JSON
        (200, completion({"plan": "none"})),    # a fourth: JSON that is not a plan
    ]
    text = specification(STAGED_CHARS)
    status, reply = lab.send(text)
    assert status == 200, reply
    spec_id = spec_of(reply)

    run_id = lab.wait_run()
    assert lab.model.planned == PARTS, "a part was planned twice"
    assert lab.model.asked == [PARTS[0]] * 8 + PARTS
    assert [s["attempts"] for s in lab.stages(spec_id)] == [5, 1, 1, 1, 1]
    spec = lab.spec(spec_id)
    assert (spec["state"], spec["run_id"], spec["last_error"]) == ("planned", run_id, None)
    assert kinds(lab.messages()) == ["user", "planning", "tool", "started"]
    assert conversation_state(lab) == "idle"
    assert len(lab.goals()) == 1 and len(lab.runs()) == 1


def test_a_part_that_keeps_failing_stops_the_plan_and_a_resend_finishes_it(lab):
    """Six attempts at one part, then planning stops and says so in the chat.
    The text is kept; sending it again picks the plan up where it stopped."""
    lab.model.faults = [(500, {})] * 18                 # six attempts of three requests
    text = specification(STAGED_CHARS)
    status, reply = lab.send(text)
    assert status == 200, reply
    spec_id = spec_of(reply)

    spec = lab.wait_spec(spec_id, "failed")
    assert "строки 1-149 (после 6 попыток)" in spec["last_error"]
    assert "groq -> 500" in spec["last_error"]
    assert spec["stages"] == {"total": 5, "planned": 0, "planning": 0, "pending": 4, "failed": 1}
    failure = lab.messages()[-1]
    assert kinds(lab.messages()) == ["user", "planning", "error"]
    assert failure["meta"] == {"error": True, "spec_id": spec_id}
    assert "Не удалось построить план по ТЗ" in failure["content"]
    assert "Текст ТЗ сохранён" in failure["content"]
    assert conversation_state(lab) == "error"
    assert lab.runs() == [] and len(lab.goals()) == 1
    assert lab.studio.sandbox.conversations == {}, "an agent was started without a plan"
    assert lab.stored(spec_id) == text.encode("utf-8")

    status, again = lab.send(text)
    assert status == 200, again
    assert kinds(again["messages"]) == ["user", "planning"]
    assert spec_of(again) == spec_id
    run_id = lab.wait_run()
    assert lab.model.planned == PARTS
    assert [s["attempts"] for s in lab.stages(spec_id)] == [1, 1, 1, 1, 1]
    assert conversation_state(lab) == "idle"
    assert [r["id"] for r in lab.runs()] == [run_id] and len(lab.goals()) == 1


def test_a_failure_in_the_middle_keeps_the_parts_already_planned(lab):
    """A refused key ends planning at once - nothing is gained by asking again.
    The two parts planned before it are kept and are not asked for again."""
    def at(number, _prompt):
        if number == 2:                                # the next request is part three's
            lab.model.faults.append((401, {"error": "invalid api key"}))

    lab.model.before = at
    text = specification(STAGED_CHARS)
    status, reply = lab.send(text)
    assert status == 200, reply
    spec_id = spec_of(reply)

    spec = lab.wait_spec(spec_id, "failed")
    assert spec["last_error"].startswith("строки 294-441: ")
    assert "groq -> 401" in spec["last_error"]
    assert [(s["state"], s["attempts"]) for s in lab.stages(spec_id)] == [
        ("planned", 1), ("planned", 1), ("failed", 1), ("pending", 0), ("pending", 0)]
    assert kinds(lab.messages()) == ["user", "planning", "error"]
    assert lab.runs() == []

    status, again = lab.send(text)
    assert status == 200, again
    progress = again["messages"][1]["meta"]["planning"]
    assert (progress["total"], progress["planned"]) == (5, 2)
    run_id = lab.wait_run()

    # Parts one and two were asked for once, by the first send.
    assert lab.model.asked == [PARTS[0], PARTS[1], PARTS[2], PARTS[2], PARTS[3], PARTS[4]]
    assert lab.model.planned == PARTS
    # And the run is built from those first answers: each title carries the
    # number of the request that produced it.
    answered_by = {t["task_key"][:4]: t["title"].rsplit("#", 1)[1]
                   for t in lab.run(run_id)["tasks"]}
    assert answered_by == {"p001": "1", "p002": "2", "p003": "4", "p004": "5", "p005": "6"}
    assert len(lab.goals()) == 1 and len(lab.runs()) == 1


def test_a_short_text_whose_plan_fails_says_so_and_can_be_sent_again(lab):
    """A text of one part is planned inside the request, so its failure is the
    request's answer - and it still leaves one goal and no half-made run."""
    lab.model.faults = [(500, {})] * 3
    text = "Цель: покрыть модуль тестами."
    status, reply = lab.send(text)
    assert status == 502, reply
    assert "could not start the autonomous run" in reply["error"]
    assert kinds(reply["messages"]) == ["user", "error"]
    spec_id = spec_of(reply)
    assert lab.spec(spec_id)["state"] == "failed"
    assert lab.runs() == [] and conversation_state(lab) == "error"

    status, again = lab.send(text)
    assert status == 200, again
    assert kinds(again["messages"]) == ["user", "tool", "started"]
    assert spec_of(again) == spec_id
    assert len(lab.goals()) == 1 and len(lab.runs()) == 1
    assert conversation_state(lab) == "idle"


# ----------------------------------------- a part the model cannot work with
def contiguous(stages: list[dict], spec: dict) -> bool:
    """Whether *stages* cover the text from its first character to its last,
    in order, with no gap and no overlap."""
    position = 0
    for stage in stages:
        if stage["start_char"] != position:
            return False
        position = stage["end_char"]
    return position == spec["char_count"]


def test_a_part_the_model_cannot_read_is_cut_smaller_not_cut_short(lab):
    """The provider refuses a request for its size. The same text is planned
    in smaller parts; none of it is dropped to make it fit."""
    lab.model.too_large_over = 5_000                   # characters of text per request
    text = specification(STAGED_CHARS)
    status, reply = lab.send(text)
    assert status == 200, reply
    spec_id = spec_of(reply)
    run_id = lab.wait_run()

    assert lab.model.refused == 4                      # the four full-size parts
    spec, stages = lab.spec(spec_id), lab.stages(spec_id)
    assert len(stages) == 13 and all(s["state"] == "planned" for s in stages)
    assert contiguous(stages, spec)
    assert [(s["start_line"], s["end_line"]) for s in stages] == lab.model.planned
    assert stages[0]["start_line"] == 1 and stages[-1]["end_line"] == spec["line_count"]
    covered = spec["coverage"]
    assert covered["in_tasks"] == covered["pieces"] == spec["requirement_count"]

    # The run's tasks are numbered by the parts as they ended up, and the first
    # agent is still given the whole original.
    tasks = lab.run(run_id)["tasks"]
    assert sorted({t["task_key"][:4] for t in tasks}) == [f"p{n:03d}" for n in range(1, 14)]
    assert {t["spec_ref"]["stages"] for t in tasks} == {13}
    mode, got = agent_bytes(lab.agent(tasks[0]["task_key"], run_id), sha256(text))
    assert (mode, got) == ("file", text.encode("utf-8"))


def test_a_short_text_too_large_for_the_model_is_planned_in_parts_too(lab):
    lab.model.too_large_over = 1_500
    text = specification(4_000)                        # one part, planned in the request
    status, reply = lab.send(text)
    assert status == 200, reply
    assert kinds(reply["messages"]) == ["user", "planning"]
    spec_id = spec_of(reply)
    run_id = lab.wait_run()
    stages = lab.stages(spec_id)
    assert len(stages) > 1 and contiguous(stages, lab.spec(spec_id))
    assert lab.model.asked[0] == (0, 0) and lab.model.refused >= 1
    tasks = lab.run(run_id)["tasks"]
    mode, got = agent_bytes(lab.agent(tasks[0]["task_key"], run_id), sha256(text))
    assert (mode, got) == ("inline", text.encode("utf-8"))


def test_text_that_no_model_can_read_at_any_size_fails_with_the_reason(lab):
    lab.model.too_large_over = 0                       # refuses everything
    text = specification(8_000)
    status, reply = lab.send(text)
    assert status == 200, reply
    spec_id = spec_of(reply)
    spec = lab.wait_spec(spec_id, "failed")
    assert "the request is too large for the model" in spec["last_error"]
    assert "cannot be cut into smaller stages" in spec["last_error"]
    assert kinds(lab.messages()) == ["user", "planning", "error"]
    assert lab.runs() == [] and lab.studio.sandbox.conversations == {}
    assert lab.model.planned == []


def test_a_model_that_writes_too_many_tasks_gets_smaller_parts(lab):
    """More tasks than one part may contribute is cured the same way: the part
    is cut in two and each half is planned on its own."""
    lab.model.obey_cap, lab.model.pieces_per_task = False, 2
    text = specification(STAGED_CHARS)
    status, reply = lab.send(text)
    assert status == 200, reply
    spec_id = spec_of(reply)
    run_id = lab.wait_run()
    spec, stages = lab.spec(spec_id), lab.stages(spec_id)
    assert len(stages) == 13 and contiguous(stages, spec)
    tasks = lab.run(run_id)["tasks"]
    assert len(tasks) == 229 <= 400
    assert spec["coverage"]["in_tasks"] == spec["requirement_count"]
    # No part contributed more than a part may.
    per_part: dict[str, int] = {}
    for task in tasks:
        per_part[task["task_key"][:4]] = per_part.get(task["task_key"][:4], 0) + 1
    assert max(per_part.values()) <= 50


def test_a_plan_that_would_not_fit_one_run_fails_instead_of_being_trimmed(lab):
    """One task per line of the text is more than a run holds. The plan is not
    quietly cut down to what fits: planning fails and nothing is started."""
    lab.model.obey_cap, lab.model.pieces_per_task = False, 1
    text = specification(STAGED_CHARS)
    status, reply = lab.send(text)
    assert status == 200, reply
    spec_id = spec_of(reply)
    spec = lab.wait_spec(spec_id, "failed")
    assert "a part may have at most" in spec["last_error"]
    assert kinds(lab.messages()) == ["user", "planning", "error"]
    assert lab.runs() == [] and lab.studio.sandbox.conversations == {}


# --------------------------------------------- what the model leaves out
def test_obligations_the_model_leaves_out_are_given_to_a_checking_task(lab):
    """The model's silence about a requirement is not "nothing to do". What it
    skips and reads as an obligation goes to an added task; what it declares
    to be background is recorded as that; an example needs nobody."""
    text = specification(STAGED_CHARS)
    lab.model.forget = {"R-0004", "R-0005", "R-0019"}
    lab.model.context = {"R-0016"}

    status, reply = lab.send(text)
    assert status == 200, reply
    spec_id = spec_of(reply)
    run_id = lab.wait_run()

    pieces = {p["key"]: p for p in lab.get(
        f"/specs/{spec_id}/requirements?limit=30")["requirements"]}
    assert "обязан" in pieces["R-0004"]["excerpt"] and pieces["R-0004"]["normative"]
    assert "Добавь" in pieces["R-0005"]["excerpt"] and pieces["R-0005"]["normative"]
    assert pieces["R-0019"]["kind"] == "code" and not pieces["R-0019"]["normative"]

    covered = lab.spec(spec_id)["coverage"]
    assert covered["pieces"] == 456
    assert (covered["in_tasks"], covered["in_verification_tasks"], covered["declared_context"],
            covered["implicit_context"]) == (452, 2, 1, 1)

    tasks = lab.run(run_id)["tasks"]
    checking = [t for t in tasks if t["spec_ref"].get("verification")]
    assert [t["task_key"] for t in checking] == ["p001-verify"]
    check = checking[0]
    assert check["spec_ref"]["requirements"] == ["R-0004..R-0005"]
    assert "R-0004..R-0005" in check["description"]
    # It runs after the other tasks of its part, and the next part waits for it.
    assert check["dependencies"] == ["p001-t4"]
    after = next(t for t in tasks if t["task_key"] == "p002-t1")
    assert after["dependencies"] == ["p001-verify"]
    # No ordinary task claims what the model left out.
    for task in tasks:
        if task is not check:
            assert "R-0004" not in " ".join(task["spec_ref"]["requirements"])


def test_a_model_that_plans_nothing_still_leaves_no_requirement_unowned(lab):
    """An empty answer is a valid answer for a part that asks for nothing. For
    a part that does ask, every obligation in it lands in a checking task."""
    lab.studio.llm.respond = lambda body: (200, completion({"tasks": []}))
    text = specification(STAGED_CHARS)
    status, reply = lab.send(text)
    assert status == 200, reply
    spec_id = spec_of(reply)
    run_id = lab.wait_run()
    tasks = lab.run(run_id)["tasks"]
    assert [t["task_key"] for t in tasks] == [f"p00{n}-verify" for n in range(1, 6)]
    covered = lab.spec(spec_id)["coverage"]
    assert covered["in_tasks"] == 0 and covered["declared_context"] == 0
    assert covered["in_verification_tasks"] + covered["implicit_context"] == covered["pieces"]
    normative = sum(1 for offset in range(0, 456, 200) for p in lab.get(
        f"/specs/{spec_id}/requirements?offset={offset}&limit=200")["requirements"]
        if p["normative"])
    assert covered["in_verification_tasks"] == normative > 300


# ------------------------------------------------ the sandbox and the file
def first_task(lab, run_id: str, *, timeout: float = 40.0) -> dict:
    """The run's first task once it has been started, has been put to wait
    for its next cycle, or the run has stopped."""
    def settled():
        detail = lab.run(run_id)
        first = detail["tasks"][0]
        if detail["run"]["state"] != "running" or first["state"] in ("running", "waiting"):
            return {**first, "run": detail["run"]}
        return None
    return until(settled, timeout=timeout, what="the first task to be started or given up")


def test_a_directory_that_cannot_be_written_to_is_not_the_end(lab):
    lab.studio.sandbox.read_only_dirs = {"/workspace/.nexus"}
    text = specification(STAGED_CHARS)
    assert lab.send(text)[0] == 200
    run_id = lab.wait_run()
    task = first_task(lab, run_id)
    assert task["state"] == "running"
    digest = sha256(text)
    placed = task["spec_ref"]["delivery"]["files"]
    assert placed[0] == {"path": f"/tmp/.nexus/spec-{digest[:12]}.md",
                         "bytes": len(text.encode("utf-8")), "sha256": digest}
    conversation = lab.agent(task["task_key"], run_id)
    assert f"/tmp/.nexus/spec-{digest[:12]}.md" in conversation.initial_message
    assert "/workspace/.nexus" not in conversation.initial_message
    assert agent_bytes(conversation, digest) == ("file", text.encode("utf-8"))


@pytest.mark.parametrize("fault", ["nowhere-to-write", "cut-short", "one-byte-changed",
                                   "one-byte-added"])
def test_no_agent_is_started_on_a_text_that_did_not_arrive_whole(lab, fault):
    """If the file in the sandbox is not the text, byte for byte, there is no
    conversation: not one with a partial file, not one without it."""
    box = lab.studio.sandbox
    if fault == "nowhere-to-write":
        box.read_only_dirs = {"/workspace/.nexus", "/tmp/.nexus"}
    elif fault == "cut-short":
        box.store_as = lambda path, data: data[:-1] if path.endswith(".md") else data
    elif fault == "one-byte-changed":
        box.store_as = lambda path, data: (
            data[:1000] + bytes([data[1000] ^ 1]) + data[1001:] if path.endswith(".md") else data)
    else:
        box.store_as = lambda path, data: data + b"\n" if path.endswith(".md") else data
    text = specification(STAGED_CHARS)
    assert lab.send(text)[0] == 200
    run_id = lab.wait_run()

    task = first_task(lab, run_id)
    assert box.conversations == {}, "an agent was started without the whole text"
    # Three starts were refused by the check: the attempts of this cycle are
    # used, and the task waits for the next one. It is not given up - another
    # sandbox may take the file - and the reason it waits is on its line.
    assert (task["state"], task["cycle"], task["attempt"], task["conversation_id"]) == (
        "waiting", 1, 0, None)
    reason = task["wait_reason"]
    assert reason.startswith("DeliveryError: ") and "Следующая попытка через" in reason
    if fault == "nowhere-to-write":
        assert "Permission denied" in reason
    else:
        size = len(text.encode("utf-8"))
        held = {"cut-short": f"holds {size - 1} bytes", "one-byte-changed": f"holds {size} bytes",
                "one-byte-added": f"holds more than {size + 1} bytes"}[fault]
        assert held in reason and f"{size} bytes were sent" in reason
    assert task["run"]["state"] == "running" and task["retry_at"] is not None
    time.sleep(0.6)                                    # more passes of the lab loop
    assert box.conversations == {}


def test_an_upload_that_fails_once_is_tried_again(lab):
    box = lab.studio.sandbox
    box.upload_status, box.upload_status_times = 500, 2   # both directories, first attempt
    text = specification(STAGED_CHARS)
    assert lab.send(text)[0] == 200
    run_id = lab.wait_run()
    task = first_task(lab, run_id)
    assert (task["state"], task["attempt"]) == ("running", 2)
    assert len(box.conversations) == 1
    conversation = lab.agent(task["task_key"], run_id)
    assert agent_bytes(conversation, sha256(text)) == ("file", text.encode("utf-8"))


# ------------------------------------- a transport that cannot place a file
def _through_the_run(studio, lab, run_id, *, timeout=120.0):
    """Let every task of a run on the app transport finish, and return the first
    message each one was started with, by task key."""
    cloud = studio.cloud.cloud
    messages: dict[str, str] = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        for cid, conversation in list(cloud.conversations.items()):
            tags = conversation.get("tags") or {}
            if tags.get("nexusrun") != run_id[:40] or tags["nexustask"] in messages:
                continue
            messages[tags["nexustask"]] = (
                cloud.start_bodies[cid]["initial_message"]["content"][0]["text"])
            cloud.add_assistant_message(cid, f"Готово. Коммит {HEAD}.")
            conversation["execution_status"] = "finished"
        detail = lab.run(run_id)
        if detail["run"]["state"] != "running":
            return detail, messages
        time.sleep(0.05)
    raise AssertionError(f"the run did not end: {lab.run(run_id)['run']}")


def _between(message: str, kind: str, digest: str) -> str:
    """What stands between the two marker lines, cut out by the documented rule."""
    opening, closing = f"<<<NEXUS-TASK-{kind} {digest[:12]}\n", f"\nNEXUS-TASK-{kind} {digest[:12]}>>>"
    assert message.count(opening) == 1 and message.count(closing) == 1, (kind, digest[:12])
    return message[message.index(opening) + len(opening):message.index(closing)]


def test_where_no_file_can_be_placed_a_text_that_fits_a_message_travels_whole(monkeypatch):
    """The app-conversation transport starts a conversation and nothing else:
    there is no way to put a file in its sandbox. A text too long for the usual
    inline limit is then still given whole, in the message, to every task."""
    monkeypatch.setenv("NEXUS_SPEC_STAGE_TOKENS", "3000")
    with studio_stack(monkeypatch, transport="app", lab_inline=True) as studio:
        lab = Lab(studio, repo=REPO)
        text = specification(STAGED_CHARS)                 # above the 20 000 inline limit
        assert lab.send(text)[0] == 200
        run_id = lab.wait_run()
        detail, messages = _through_the_run(studio, lab, run_id)
        assert detail["run"]["state"] == "completed", detail["run"]
        assert len(messages) == len(detail["tasks"]) == 17
        digest = sha256(text)
        for task in detail["tasks"]:
            message = messages[task["task_key"][:40]]
            assert _between(message, "TEXT", digest) == text, task["task_key"]
            assert (task["attempt"], task["spec_ref"]["delivery"]["mode"]) == (1, "inline")
            # Its own part is named, so it knows where its work is.
            first, last = task["spec_ref"]["lines"]
            assert f"its part of the text is lines {first}-{last}" in message


def test_where_no_file_can_be_placed_a_longer_text_travels_part_by_part(monkeypatch):
    """Beyond what one message should carry, each task is given the part it
    was planned from, byte for byte, and is told that the rest is elsewhere.
    Between them the tasks are given every byte of the text."""
    monkeypatch.setenv("NEXUS_SPEC_STAGE_TOKENS", "3000")
    monkeypatch.setenv("NEXUS_SPEC_MESSAGE_CHARS", "20000")    # 30 000 no longer fits
    with studio_stack(monkeypatch, transport="app", lab_inline=True) as studio:
        lab = Lab(studio, repo=REPO)
        text = specification(STAGED_CHARS)
        data = text.encode("utf-8")
        assert lab.send(text)[0] == 200
        run_id = lab.wait_run()
        detail, messages = _through_the_run(studio, lab, run_id)
        assert detail["run"]["state"] == "completed", detail["run"]
        assert len(messages) == len(detail["tasks"]) == 17

        given: dict[tuple[int, int], bytes] = {}
        for task in detail["tasks"]:
            ref, message = task["spec_ref"], messages[task["task_key"][:40]]
            proof = ref["delivery"]
            assert (proof["mode"], proof["sha256"], proof["bytes"]) == ("part", sha256(text), len(data))
            first, end = ref["bytes"]
            own = data[first:end]
            part = proof["part"]
            assert part == {"stage": ref["stage"], "stages": 5, "lines": ref["lines"],
                            "bytes": [first, end], "chars": len(own.decode("utf-8")),
                            "sha256": sha256(own.decode("utf-8"))}
            assert _between(message, "PART", part["sha256"]).encode("utf-8") == own
            assert "NEXUS-TASK-TEXT" not in message
            assert "The rest of the specification is NOT in this conversation" in message
            assert f"part {ref['stage']} of 5: lines {ref['lines'][0]}-{ref['lines'][1]}" in message
            assert "Outline of the whole specification" in message
            given[(first, end)] = own
        # Five parts, in order, with no gap and no overlap: the whole text.
        assert [ref for ref in sorted(given)][0][0] == 0
        assert b"".join(given[key] for key in sorted(given)) == data
        assert len(given) == 5


# ------------------------------------------------ the modes that do not plan
def test_ask_and_plan_say_where_a_text_too_long_for_them_goes(lab):
    """`ask` and `plan` send the message to a model in one request. When it
    does not fit they say so, and name the mode that takes a text of any size."""
    for provider in lab.studio.app.providers.usable():
        provider.context_tokens = 12_000
    text = specification(STAGED_CHARS)                  # about 12 500 tokens
    for mode in ("ask", "plan"):
        conversation = lab.studio.conversation(lab.project, mode=mode)
        status, reply = lab.send(text, mode=mode, conversation=conversation)
        assert status == 502, (mode, reply)
        assert "не помещается в окно модели" in reply["error"]
        assert "«Автономно»" in reply["error"]
        assert "GROQ_CONTEXT_TOKENS=12000" in reply["error"]
        assert kinds(reply["messages"]) == ["user", "error"]
        # The message itself is kept, whole.
        assert reply["messages"][0]["content"] == text.strip()
    assert lab.model.prompts == [], "a request that could not fit was sent anyway"

    # And that mode does take it, with the same small model.
    status, reply = lab.send(text)
    assert status == 200, reply
    run_id = lab.wait_run()
    assert lab.spec(spec_of(reply))["state"] == "planned"
    first = lab.run(run_id)["tasks"][0]
    mode, got = agent_bytes(lab.agent(first["task_key"], run_id), sha256(text))
    assert (mode, got) == ("file", text.encode("utf-8"))


def test_a_message_over_the_transcript_limit_is_refused_with_the_way_out(lab):
    over = "я" * 200_001
    for mode in ("ask", "plan", "code"):
        conversation = lab.studio.conversation(lab.project, mode=mode)
        status, body = lab.send(over, mode=mode, conversation=conversation)
        assert status == 413, (mode, body)
        assert "200001" in body["error"] and "200000" in body["error"]
        assert "autonomous mode" in body["error"] and "1000000" in body["error"]
        assert lab.messages(conversation) == []
    # Exactly the limit is a message like any other.
    lab.studio.llm.respond = lambda body: (200, completion("Понял."))
    conversation = lab.studio.conversation(lab.project, mode="ask")
    status, body = lab.send("я" * 200_000, mode="ask", conversation=conversation)
    assert status == 200, body
    assert len(body["messages"][0]["content"]) == 200_000


def test_one_long_message_does_not_make_later_questions_too_large(lab):
    """History is background and is shortened to fit; the request never is."""
    asked: list[str] = []

    def answer(body):
        asked.append(body["messages"][-1]["content"])
        return 200, completion("Ответ.")

    lab.studio.llm.respond = answer
    conversation = lab.studio.conversation(lab.project, mode="ask")
    long_text = specification(150_000, seed=9)
    status, reply = lab.send(long_text, mode="ask", conversation=conversation)
    assert status == 200, reply
    # The request is in its prompt once - not a second time as "history".
    assert asked[0].count(long_text[:200]) == 1
    assert asked[0].endswith("Request:\n" + long_text.strip())
    assert "Recent conversation" not in asked[0]

    status, reply = lab.send("А что с пунктом 7?", mode="ask", conversation=conversation)
    assert status == 200, reply
    assert asked[1].endswith("Request:\nА что с пунктом 7?")
    assert "assistant: Ответ." in asked[1]
    assert "more characters not shown]" in asked[1]
    assert len(asked[1]) < 26_000, "the whole earlier message was sent again"


# --------------------------------------------- planner fallback (empty plan)
def test_an_empty_plan_is_recovered_by_decomposing_the_specification(lab):
    """The model answered "no tasks" for a brief that plainly has work. The
    planner must not hand that back as the end: it decomposes by the
    specification's own index (one task per section with obligations) and starts
    a real run, with every normative piece traced to a task."""
    import re as _re

    def declare_all_background(body):
        # Return no tasks and claim every piece of the part needs nothing done -
        # the answer that leaves the merged plan empty ("the plan has no tasks").
        prompt = body["messages"][-1]["content"]
        keys = sorted(set(_re.findall(r"\[(R-\d+)\]", prompt)))
        return 200, completion({"tasks": [], "context": keys})

    lab.studio.llm.respond = declare_all_background
    text = specification(STAGED_CHARS)
    status, reply = lab.send(text)
    assert status == 200, reply
    spec_of(reply)                                     # the text was stored
    run_id = lab.wait_run()
    tasks = lab.run(run_id)["tasks"]
    # A real DAG, not an empty plan and not a single giant task.
    assert len(tasks) >= 3, [t["task_key"] for t in tasks]
    # It is the application's fallback, recorded as such on the run's plan.
    announced = lab.announcements()[0]["meta"]
    assert announced["run_id"] == run_id
    assert announced["coverage"].get("fallback") is True
    # Every task names the original requirement(s) it covers.
    for task in tasks:
        assert task["spec_ref"]["requirements"], task["task_key"]
        assert task["spec_ref"]["fallback"] is True
    # The chain is ordered: each task waits for the one before it.
    assert tasks[0]["dependencies"] == []
    for earlier, later in zip(tasks, tasks[1:]):
        assert later["dependencies"] == [earlier["task_key"]]


def test_the_fallback_refuses_a_specification_with_nothing_to_do(lab):
    """A text with no obligation at all is not turned into fabricated work: the
    honest failure is kept, and nothing is started. Sent over REST (the chat
    router would answer such a message as chat, which is the point of the router;
    this test is about the planner)."""
    lab.studio.llm.respond = lambda body: (200, completion({"tasks": []}))
    # One paragraph, no imperative, no obligation, no list, no heading.
    text = "Это просто заметка о проекте и ничего больше. Спасибо за внимание."
    status, body = lab.post("/goals", {"text": text})
    assert status == 201, body
    spec_id = body["spec"]["id"]
    status, body = lab.post(f"/goals/{body['goal']['id']}/run")
    assert status == 422, body
    assert "the plan has no tasks" in body["error"]
    failed = lab.wait_spec(spec_id, "failed")
    assert "the plan has no tasks" in failed["last_error"]
    assert lab.runs() == []
    assert lab.studio.sandbox.conversations == {}


def test_the_fallback_plan_is_executable_and_delivers_the_original(lab):
    """The fallback plan runs like any other: its first task is dispatched and
    its agent is given the original text byte for byte.

    The model declares *every* piece to be background ("context") and returns no
    tasks - the answer that makes the merged plan empty and produces "the plan
    has no tasks". The fallback recovers it from the specification's index.
    """
    import re as _re

    def declare_all_background(body):
        # The prompt carries the part with each piece marked "[R-0001]"; the
        # model claims all of them need nothing done. Read them from the prompt.
        prompt = body["messages"][-1]["content"]
        keys = sorted(set(_re.findall(r"\[(R-\d+)\]", prompt)))
        return 200, completion({"tasks": [], "context": keys})

    lab.studio.llm.respond = declare_all_background
    # Short enough to be one planning stage, so the whole text is one part and
    # the fallback plan is the plan for it.
    text = specification(2_000, seed=3)
    digest = sha256(text)
    status, body = lab.post("/goals", {"text": text})
    assert status == 201, body
    status, body = lab.post(f"/goals/{body['goal']['id']}/run")
    assert status == 201, body
    assert body["plan"]["coverage"].get("fallback") is True
    run_id = body["run"]["id"]
    first = lab.run(run_id)["tasks"][0]
    assert first["spec_ref"]["sha256"] == digest
    mode, got = agent_bytes(lab.agent(first["task_key"], run_id), digest)
    assert mode in ("file", "inline")
    assert got == text.encode("utf-8")
