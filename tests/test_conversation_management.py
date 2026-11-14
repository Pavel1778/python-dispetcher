"""Conversation management: pins, soft delete/restore, search, export.

Over real HTTP and PostgreSQL. A pin is durable and orders the pinned section;
a delete is a stamp (reversible) and leaves the project and its history alone;
search finds a conversation by its title *and* by the text of its messages;
export is a self-contained document with no secret in it.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_studio_api import DSN, studio_stack  # noqa: E402

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run these tests")


@pytest.fixture
def studio(monkeypatch):
    with studio_stack(monkeypatch) as stack:
        yield stack


def _conversation(stack, project, title="", mode="ask"):
    status, body = stack.api.post(f"/api/v1/projects/{project}/conversations",
                                   {"title": title, "mode": mode})
    assert status == 201, body
    return body["conversation"]["id"]


def _titles(stack, project, query=""):
    status, body = stack.api.get(f"/api/v1/projects/{project}/conversations{query}")
    assert status == 200, body
    return [c["title"] for c in body["conversations"]]


def _search(value):
    from urllib.parse import quote
    return "?q=" + quote(value)


# -------------------------------------------------------------------- pins
def test_a_pinned_conversation_is_durable_and_comes_first(studio):
    project = studio.project()
    first = _conversation(studio, project, "Первая")
    _conversation(studio, project, "Вторая")
    _conversation(studio, project, "Третья")
    # Pin the oldest; it must move to the top and stay there.
    status, body = studio.api.patch(f"/api/v1/projects/{project}/conversations/{first}",
                                    {"pinned": True})
    assert status == 200 and body["conversation"]["pinned"] is True
    assert _titles(studio, project)[0] == "Первая"
    # A restart on the same database keeps the pin (it is a column).
    studio.boot()
    assert _titles(studio, project)[0] == "Первая"
    # Unpinning restores the activity order.
    studio.api.patch(f"/api/v1/projects/{project}/conversations/{first}",
                     {"pinned": False})
    assert _titles(studio, project)[0] == "Третья"
    assert set(_titles(studio, project)) == {"Первая", "Вторая", "Третья"}


def test_pin_must_be_a_boolean(studio):
    project = studio.project()
    conversation = _conversation(studio, project, "Чат")
    status, body = studio.api.patch(
        f"/api/v1/projects/{project}/conversations/{conversation}", {"pinned": "yes"})
    assert status == 400 and "boolean" in body["error"]


# ------------------------------------------------------------- soft delete
def test_a_delete_is_real_and_reversible_and_leaves_the_project(studio):
    project = studio.project()
    kept = _conversation(studio, project, "Остаётся")
    gone = _conversation(studio, project, "Удаляемый")
    status, body = studio.api.patch(
        f"/api/v1/projects/{project}/conversations/{gone}", {"deleted": True})
    assert status == 200 and body["conversation"]["deleted_at"]
    # It is gone from the normal list...
    assert _titles(studio, project) == ["Остаётся"]
    # ...and visible only through the trash view.
    assert _titles(studio, project, "?deleted=1") == ["Удаляемый"]
    # The project and its other conversations are untouched.
    assert studio.api.get(f"/api/v1/projects/{project}")[0] == 200
    assert kept in [c["id"] for c in
                    studio.api.get(f"/api/v1/projects/{project}/conversations")[1]["conversations"]]
    # Restore brings it back.
    status, body = studio.api.patch(
        f"/api/v1/projects/{project}/conversations/{gone}", {"deleted": False})
    assert status == 200 and body["conversation"]["deleted_at"] is None
    assert set(_titles(studio, project)) == {"Остаётся", "Удаляемый"}


def test_a_deleted_conversation_keeps_its_messages(studio):
    project = studio.project()
    conversation = _conversation(studio, project, "С историей")
    studio.answers("Ответ сохранён.")
    status, body = studio.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages",
        {"text": "Вопрос"})
    assert status == 200
    studio.api.patch(f"/api/v1/projects/{project}/conversations/{conversation}",
                     {"deleted": True})
    # The messages are still readable by id (nothing was removed).
    messages = studio.api.get(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages")[1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]


def test_the_delete_route_archives_and_does_not_delete(studio):
    """The legacy DELETE route still archives (not the new soft delete)."""
    project = studio.project()
    conversation = _conversation(studio, project, "Архив")
    status, _ = studio.api("DELETE",
                           f"/api/v1/projects/{project}/conversations/{conversation}")
    assert status == 200
    assert _titles(studio, project) == []
    listed = studio.api.get(
        f"/api/v1/projects/{project}/conversations?archived=1")[1]["conversations"]
    assert [c["title"] for c in listed] == ["Архив"]


# ------------------------------------------------------------------ search
def test_search_finds_a_conversation_by_the_text_of_its_messages(studio):
    project = studio.project()
    wanted = _conversation(studio, project, "Без ключевого слова в названии")
    _conversation(studio, project, "Другая тема")
    studio.answers("Готово.")
    studio.api.post(f"/api/v1/projects/{project}/conversations/{wanted}/messages",
                    {"text": "Расскажи про unicorn-pipeline"})
    assert _titles(studio, project, "?q=unicorn-pipeline") == [
        "Без ключевого слова в названии"]


def test_search_by_title(studio):
    project = studio.project()
    _conversation(studio, project, "Настройка оплаты")
    _conversation(studio, project, "Настройка входа")
    _conversation(studio, project, "Документация")
    assert sorted(_titles(studio, project, _search("Настройка"))) == ["Настройка входа",
                                                                 "Настройка оплаты"]


# ------------------------------------------------------------------ export
def test_export_is_a_self_contained_document_with_no_secret(studio):
    project = studio.project()
    conversation = _conversation(studio, project, "Экспорт")
    studio.answers("Вот ответ.")
    studio.api.post(f"/api/v1/projects/{project}/conversations/{conversation}/messages",
                    {"text": "Вот вопрос"})
    status, body = studio.api.get(
        f"/api/v1/projects/{project}/conversations/{conversation}/export")
    assert status == 200
    assert body["conversation"]["title"] == "Экспорт"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][0]["content"] == "Вот вопрос"
    # Nothing that looks like a credential is in the document.
    blob = json.dumps(body, ensure_ascii=False)
    assert "cloud-key-value" not in blob
    assert "github-token" not in blob


def test_export_of_a_foreign_conversation_is_not_found(studio):
    project = studio.project()
    conversation = _conversation(studio, project, "Чужой")
    status, _ = studio.api.get(
        f"/api/v1/projects/{project}/conversations/"
        "00000000-0000-4000-8000-000000000000/export")
    assert status == 404
    # The real one is reachable.
    assert studio.api.get(
        f"/api/v1/projects/{project}/conversations/{conversation}/export")[0] == 200
