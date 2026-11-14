"""The chat command system: slash commands, hashtags and typed parameters.

The first half is the pure parser (no database, no provider): the vocabulary
from the requirement, the tag groups, the parameter types, the priority of a
restriction, and the shape rules that keep a path from looking like a command.
The second half drives the real server over real HTTP and PostgreSQL and
asserts the observable behaviour: ``/plan`` plans without dispatching,
``/autorun`` starts a run, ``#read-only`` forbids a change, an unknown command
is refused, ``/help`` answers, and an interface button (``action``) goes
through exactly the same parser.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.nexus_commands import (  # noqa: E402
    ALIASES,
    COMMANDS,
    TAG_GROUPS,
    metadata,
    parse,
    registry,
    suggest,
    task_text,
)
from tests.test_studio_api import DSN, REPO, studio_stack  # noqa: E402

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run command tests")


@pytest.fixture
def studio(monkeypatch):
    with studio_stack(monkeypatch) as stack:
        yield stack


PLAN = {"tasks": [
    {"id": "audit", "title": "Изучить код", "description": "Прочитать модуль.",
     "acceptance": ["сводка"], "dependencies": []},
]}


def _start_conversation(stack, project, mode="auto"):
    status, body = stack.api.post(f"/api/v1/projects/{project}/conversations",
                                   {"title": "", "mode": mode})
    assert status == 201, body
    return body["conversation"]["id"]


def _send(stack, project, conversation, text, **extra):
    body = {"text": text, **extra}
    return stack.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages", body)


def _count_runs(stack, project) -> int:
    return len(stack.api.get(f"/api/v1/projects/{project}/runs")[1]["runs"])


# ------------------------------------------------------------------ vocabulary
def test_every_command_from_the_requirement_exists():
    for name in ("chat", "plan", "autorun", "audit", "fix", "test", "review",
                 "deploy", "research", "status", "help"):
        assert name in COMMANDS, name


def test_the_four_tag_groups_and_their_tags():
    assert set(TAG_GROUPS) == {"technology", "quality", "execution", "restriction"}
    expected = {
        "technology": {"frontend", "backend", "database", "api", "infrastructure",
                       "devops", "mobile", "desktop"},
        "quality": {"testing", "security", "performance", "refactoring",
                    "documentation", "accessibility"},
        "execution": {"parallel", "sequential", "deep", "fast", "critical"},
        "restriction": {"no-merge", "no-deploy", "no-delete", "read-only"},
    }
    assert {g: set(t) for g, t in TAG_GROUPS.items()} == expected


# ------------------------------------------------------------------ the parser
def test_a_mode_command_picks_the_mode_and_strips_itself():
    parsed = parse("/plan #backend #security починить вход")
    assert parsed.ok
    assert parsed.mode == "plan"
    assert parsed.text == "починить вход"
    assert parsed.tags["technology"] == ["backend"]
    assert parsed.tags["quality"] == ["security"]


def test_autorun_is_an_explicit_execute_command():
    parsed = parse("/autorun #frontend #mobile #testing починить форму")
    assert parsed.mode == "autonomous"
    assert parsed.text == "починить форму"
    assert parsed.tags["technology"] == ["frontend", "mobile"]


def test_a_task_command_defaults_its_own_mode():
    assert parse("/audit #deep #security").mode == "analysis"
    assert parse("/audit #deep #security").task_type == "audit"
    assert parse("/review #security").mode == "analysis"
    assert parse("/fix #critical").mode == "autonomous"


def test_a_mode_and_a_task_command_combine():
    parsed = parse("/plan /audit #database")
    assert parsed.mode == "plan"
    assert parsed.task_type == "audit"
    assert [c.name for c in parsed.commands] == ["plan", "audit"]


def test_hashtags_are_grouped_and_kept_in_order():
    parsed = parse("/plan #frontend #backend #security #deep #critical")
    assert parsed.tag_list() == ["frontend", "backend", "security", "deep", "critical"]
    assert parsed.restrictions == set()


# --------------------------------------------------------------- restrictions
def test_read_only_wins_over_an_execute_command():
    parsed = parse("/fix #read-only перепиши модуль")
    assert parsed.ok, "a downgrade is not an error"
    assert parsed.mode == "analysis", "#read-only must not change code"
    assert parsed.task_type == "audit"
    assert parsed.warnings, "the downgrade is reported"


def test_read_only_over_autorun_downgrades_to_analysis():
    parsed = parse("/autorun #read-only проверь логику")
    assert parsed.mode == "analysis"
    assert parsed.ok


def test_an_impossible_restriction_is_an_error():
    parsed = parse("/deploy #no-deploy")
    assert not parsed.ok
    assert parsed.errors and "no-deploy" in parsed.errors[0]


def test_no_merge_keeps_deploy_but_warns():
    parsed = parse("/deploy #no-merge")
    assert parsed.ok
    assert parsed.warnings


# ----------------------------------------------------------------- parameters
def test_typed_parameters_are_parsed_and_removed_from_the_request():
    parsed = parse("/fix --repo owner/name --branch dev исправить вход")
    assert parsed.params == {"repo": "owner/name", "branch": "dev"}
    assert parsed.text == "исправить вход"


def test_a_wrong_parameter_type_is_refused():
    parsed = parse("/fix --repo")           # a value is required
    assert not parsed.ok
    assert any("repo" in e for e in parsed.errors)


def test_an_unknown_parameter_is_refused():
    parsed = parse("/fix --nope=1 исправить")
    assert not parsed.ok
    assert any("nope" in e for e in parsed.errors)


# ------------------------------------------------------------- shape rules
def test_a_path_is_not_a_command():
    parsed = parse("Проверь /tmp/file.txt и C:/x #backend")
    assert parsed.unknown_commands == []
    assert parsed.text == "Проверь /tmp/file.txt и C:/x"


def test_a_real_unknown_command_is_reported():
    parsed = parse("/nosuchcommand сделай что-то")
    assert parsed.unknown_commands == ["/nosuchcommand"]


def test_an_issue_number_is_reported_but_still_read_as_unknown():
    parsed = parse("Починить #1234 и #backend")
    assert parsed.tags["technology"] == ["backend"]
    assert parsed.unknown_tags == ["#1234"]


def test_a_multiline_specification_keeps_its_lines():
    parsed = parse("/plan #backend #security\nпервая строка\nвторая строка")
    assert parsed.text == "первая строка\nвторая строка"


def test_a_plain_message_is_untouched():
    parsed = parse("Обычное сообщение без команд")
    assert parsed.text == "Обычное сообщение без команд"
    assert parsed.commands == [] and parsed.tags == {}


def test_two_commands_of_one_kind_are_refused():
    assert not parse("/plan /autorun что-то").ok


# --------------------------------------------------------------- aliases/suggest
def test_aliases_resolve_to_the_canonical_command():
    assert parse("/вопрос привет").mode == "ask"
    assert ALIASES["auto"] == "autorun"


def test_suggestions_complete_commands_and_tags():
    assert "/autorun" in [s["value"] for s in suggest("/au")]
    assert "#security" in [s["value"] for s in suggest("#sec")]
    assert all(s["kind"] == "tag" for s in suggest("#", limit=100))


def test_registry_exposes_the_whole_vocabulary():
    payload = registry()
    assert {c["name"] for c in payload["commands"]} == (
        set(COMMANDS) - {"help"})  # help is hidden from the menu
    assert len(payload["tags"]) == sum(len(t) for t in TAG_GROUPS.values())
    assert "Команды NEXUS" in payload["help"]


def test_task_text_carries_the_metadata_but_not_for_a_plain_message():
    assert task_text(parse("просто текст"), "просто текст") == "просто текст"
    with_meta = task_text(parse("/fix #critical #backend исправить"), "исправить")
    assert "исправить" in with_meta and "#backend" in with_meta and "#critical" in with_meta


def test_metadata_is_json_serialisable():
    meta = metadata(parse("/plan #backend #security --repo o/n починить"))
    assert json.loads(json.dumps(meta))["tags"] == ["backend", "security"]


# ----------------------------------------------------------------- HTTP surface
def test_the_commands_endpoint_serves_the_vocabulary(studio):
    status, body = studio.api.get("/api/commands")
    assert status == 200, body
    assert {c["name"] for c in body["commands"]} >= {"plan", "autorun", "audit"}
    assert any(t["name"] == "read-only" for t in body["tags"])
    status, body = studio.api.get("/api/commands?q=/au")
    assert status == 200
    assert "/autorun" in [s["value"] for s in body["suggestions"]]


def test_help_answers_without_asking_a_model_or_starting_anything(studio):
    project = studio.project()
    conversation = _start_conversation(studio, project)
    before = len(studio.llm.calls)
    status, body = _send(studio, project, conversation, "/help")
    assert status == 200, body
    assert body["messages"][-1]["meta"].get("help")
    assert "Команды NEXUS" in body["messages"][-1]["content"]
    assert len(studio.llm.calls) == before, "/help must not call a model"
    assert _count_runs(studio, project) == 0


def test_an_unknown_command_is_refused_before_anything_is_written(studio):
    project = studio.project()
    conversation = _start_conversation(studio, project)
    status, body = _send(studio, project, conversation, "/nosuchcommand сделай")
    assert status == 400
    assert "unknown command" in body["error"]
    messages = studio.api.get(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages")[1]["messages"]
    assert messages == [], "a refused command leaves no trace"


def test_plan_command_plans_and_starts_no_run(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _start_conversation(studio, project)
    studio.answers(json.dumps(PLAN))
    status, body = _send(studio, project, conversation, "/plan #backend #security добавить проверку")
    assert status == 200, body
    assert body["messages"][-1]["meta"]["mode"] == "plan"
    assert _count_runs(studio, project) == 0, "/plan must not start a run"
    assert studio.cloud.cloud.conversations == {}
    # The command decision is recorded on the stored user turn.
    user = body["messages"][0]
    assert user["meta"]["command"]["commands"] == ["plan"]
    assert user["meta"]["command"]["tags"] == ["backend", "security"]


def test_autorun_command_starts_a_run_and_carries_the_tags_into_the_task(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _start_conversation(studio, project)
    studio.answers(json.dumps(PLAN))
    status, body = _send(studio, project, conversation, "/autorun #backend #testing починить тесты")
    assert status == 200, body
    assert _count_runs(studio, project) == 1
    # The planner saw the tags: the stored specification carries them.
    prompt = studio.llm.calls[-1]["body"]["messages"][-1]["content"]
    assert "#backend" in prompt and "#testing" in prompt
    assert "починить тесты" in prompt


def test_read_only_command_never_starts_a_run(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _start_conversation(studio, project)
    studio.answers("Разбор: изменений нет.")
    status, body = _send(studio, project, conversation, "/autorun #read-only проанализируй логику")
    assert status == 200, body
    assert body["messages"][-1]["meta"]["mode"] == "analysis"
    assert _count_runs(studio, project) == 0
    assert studio.cloud.cloud.conversations == {}


def test_a_restriction_that_cannot_be_honoured_is_refused(studio):
    project = studio.project()
    conversation = _start_conversation(studio, project)
    status, body = _send(studio, project, conversation, "/deploy #no-deploy")
    assert status == 400
    assert "no-deploy" in body["error"]
    assert _count_runs(studio, project) == 0


def test_an_interface_button_uses_the_same_parser_as_a_slash_command(studio):
    """``{"action": "/plan"}`` and typing ``/plan`` must behave identically."""
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _start_conversation(studio, project)
    studio.answers(json.dumps(PLAN))
    status, body = _send(studio, project, conversation, "добавить проверку", action="/plan")
    assert status == 200, body
    assert body["messages"][-1]["meta"]["mode"] == "plan"
    assert _count_runs(studio, project) == 0
    user = body["messages"][0]
    assert user["meta"]["command"]["commands"] == ["plan"]
    assert user["content"] == "добавить проверку"


def test_an_action_may_be_a_bare_hashtag(studio):
    project = studio.project()
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = _start_conversation(studio, project)
    studio.answers("Разбор.")
    status, body = _send(studio, project, conversation, "проверь модуль", action="#read-only")
    assert status == 200, body
    assert body["messages"][-1]["meta"]["mode"] == "analysis"
