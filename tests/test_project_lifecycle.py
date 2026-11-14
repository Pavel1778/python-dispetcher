"""Project lifecycle: archive, soft delete (trash) and restore.

Over real HTTP and PostgreSQL. An archived project drops out of the default
list but is otherwise untouched; a deleted project is inert (it refuses new
conversations and goals) and cannot be deleted while it has an active run.
Nothing is ever hard-deleted: the data stays and a restore brings it back.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_studio_api import DSN, REPO, studio_stack  # noqa: E402

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to run these tests")


@pytest.fixture
def studio(monkeypatch):
    with studio_stack(monkeypatch) as stack:
        yield stack


def _names(stack):
    return sorted(p["name"] for p in stack.api.get("/api/v1/projects")[1]["projects"])


def _all_names(stack, query=""):
    return sorted(p["name"] for p in
                  stack.api.get(f"/api/v1/projects{query}")[1]["projects"])


def test_archive_hides_a_project_but_keeps_its_data(studio):
    project = studio.project("Архивируемый")
    studio.project("Остаётся")
    assert studio.api.post(f"/api/v1/projects/{project}/conversations",
                           {"title": "Чат", "mode": "ask"})[0] == 201

    status, body = studio.api.patch(f"/api/v1/projects/{project}", {"archived": True})
    assert status == 200 and body["project"]["archived_at"], body
    assert _names(studio) == ["Остаётся"]
    # The archive view brings it back; the conversation is untouched.
    assert _all_names(studio, "?archived=1") == ["Архивируемый", "Остаётся"]
    assert studio.api.get(f"/api/v1/projects/{project}/conversations")[0] == 200
    # Restore.
    assert studio.api.patch(f"/api/v1/projects/{project}", {"archived": False})[0] == 200
    assert _names(studio) == ["Архивируемый", "Остаётся"]


def test_a_deleted_project_refuses_new_work_and_is_reversible(studio):
    project = studio.project("Удаляемый")
    studio.api.patch(f"/api/v1/projects/{project}", {"deleted": True})
    assert _names(studio) == []
    assert _all_names(studio, "?deleted=1") == ["Удаляемый"]

    # New work is refused while it is in the trash.
    status, body = studio.api.post(f"/api/v1/projects/{project}/conversations",
                                   {"title": "", "mode": "ask"})
    assert status == 400 and "deleted" in body["error"], body
    status, body = studio.api.post(f"/api/v1/projects/{project}/goals", {"text": "цель"})
    assert status == 400 and "deleted" in body["error"], body

    # Restore, and work is accepted again.
    status, body = studio.api.patch(f"/api/v1/projects/{project}", {"deleted": False})
    assert status == 200 and body["project"]["deleted_at"] is None
    assert _names(studio) == ["Удаляемый"]
    assert studio.api.post(f"/api/v1/projects/{project}/conversations",
                           {"title": "", "mode": "ask"})[0] == 201


def test_a_project_with_an_active_run_cannot_be_deleted(studio):
    project = studio.project("Занятый")
    assert studio.api.post(f"/api/v1/projects/{project}/repository",
                           {"full_name": REPO})[0] == 200
    conversation = studio.api.post(
        f"/api/v1/projects/{project}/conversations",
        {"title": "", "mode": "autonomous"})[1]["conversation"]["id"]
    studio.answers(json.dumps({"tasks": [
        {"id": "a", "title": "Шаг", "description": "сделать", "acceptance": ["ок"],
         "dependencies": []}]}))
    assert studio.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages",
        {"text": "Исправь найденные ошибки"})[0] == 200

    status, body = studio.api.patch(f"/api/v1/projects/{project}", {"deleted": True})
    assert status == 400 and "active run" in body["error"], body

    # Cancel the run; now it can be deleted.
    run_id = studio.api.get(f"/api/v1/projects/{project}/runs")[1]["runs"][0]["id"]
    assert studio.api.post(f"/api/v1/projects/{project}/runs/{run_id}/cancel", {})[0] == 200
    assert studio.api.patch(f"/api/v1/projects/{project}", {"deleted": True})[0] == 200


def test_a_repeat_delete_is_idempotent(studio):
    project = studio.project("Двойное удаление")
    assert studio.api.patch(f"/api/v1/projects/{project}", {"deleted": True})[0] == 200
    status, body = studio.api.patch(f"/api/v1/projects/{project}", {"deleted": True})
    assert status == 200 and body["project"]["deleted_at"], body


def test_a_rename_still_works_alongside_lifecycle(studio):
    project = studio.project("Имя")
    status, body = studio.api.patch(f"/api/v1/projects/{project}", {"name": "Новое имя"})
    assert status == 200 and body["project"]["name"] == "Новое имя"


def test_deleting_one_project_does_not_touch_another(studio):
    a = studio.project("A")
    b = studio.project("B")
    studio.api.patch(f"/api/v1/projects/{a}", {"deleted": True})
    assert studio.api.post(f"/api/v1/projects/{b}/conversations",
                           {"title": "", "mode": "ask"})[0] == 201
    assert _names(studio) == ["B"]
