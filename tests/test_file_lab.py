"""File lab, workspaces, voice and guarded GitHub actions over real HTTP.

Uses the same harness as :mod:`tests.test_nexus_projects`: a real
:class:`orchestration.web_server.WebServer`, a real PostgreSQL and the real fake
GitHub server. Nothing is mocked at the client boundary, so multipart parsing,
archive extraction, confirmation tokens and the GitHub write path are all
exercised end to end.
"""

from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.request
import uuid
import zipfile

import pytest

from orchestration.web_auth import AuthService, hash_password
from orchestration.web_server import App, WebServer
from tests.fake_github import FakeGitHubServer
from tests.test_nexus_projects import request

DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")

TOKEN = "ghp_test_token_value_never_logged"


def make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def multipart(fields: list[tuple[str, str, bytes, str | None]]) -> tuple[bytes, str]:
    """Build a multipart body. ``fields`` is (name, value, data, content_type)."""
    boundary = "----nexus" + uuid.uuid4().hex
    out = b""
    for name, value, data, ctype in fields:
        out += f"--{boundary}\r\n".encode()
        disp = f'Content-Disposition: form-data; name="{name}"'
        if data is not None:
            disp += f'; filename="{value}"'
        out += disp.encode() + b"\r\n"
        if ctype:
            out += f"Content-Type: {ctype}\r\n".encode()
        out += b"\r\n"
        out += data if data is not None else value.encode()
        out += b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


def upload(base, path, body, content_type, *, session=None, csrf=True):
    req = urllib.request.Request(base + path, data=body, method="POST")
    req.add_header("Content-Type", content_type)
    if session:
        req.add_header("Cookie", "dispatcher_session=" + session.token)
        if csrf:
            req.add_header("X-CSRF-Token", session.csrf_token)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture
def store():
    from orchestration.cloud_store import PostgresStore

    instance = PostgresStore(DSN)
    with instance._tx() as cur:
        cur.execute(
            "TRUNCATE nexus_agent_sessions, nexus_conversation_attachments, "
            "nexus_conversation_messages, nexus_conversations, "
            "nexus_openhands_accounts, nexus_voice_notes, nexus_pending_actions, "
            "nexus_workspace_files, nexus_workspaces, nexus_files, "
            "nexus_plan_dependencies, nexus_plan_tasks, nexus_plans, "
            "nexus_project_events, nexus_goals, nexus_project_members, "
            "nexus_projects RESTART IDENTITY CASCADE"
        )
    yield instance
    instance.close()


@pytest.fixture
def gh_server():
    with FakeGitHubServer() as server:
        yield server


@pytest.fixture
def http_server(store, monkeypatch, gh_server):
    monkeypatch.setenv("OPENHANDS_API_KEY", "fake")
    app = App(DSN, token="machine-token")
    app.auth = AuthService(app.store, username="alice", password_hash=hash_password("test"),
                           session_secret="test-session-secret-which-is-long-enough")
    # Point the GitHub client (and its guarded writer) at the fake server.
    from orchestration.github_client import GitHubClient, GitHubWriter

    app.github = GitHubClient(gh_server.base_url, token=TOKEN, timeout=5.0)
    app.github_writer = GitHubWriter(app.github)
    alice = app.auth._open_session("alice")
    bob = app.auth._open_session("bob")
    server = WebServer(app, "127.0.0.1", 0)
    server.start()
    yield f"http://127.0.0.1:{server.port}", alice, bob, app, gh_server
    server.stop()


def new_project(base, session, name="Lab"):
    status, payload = request(base, "/api/v1/projects", "POST", {"name": name}, session=session)
    assert status == 201
    return payload["project"]["id"]


# ------------------------------------------------------------------- files
def test_upload_list_preview_and_download(http_server):
    base, alice, _, _, _ = http_server
    pid = new_project(base, alice)
    body, ctype = multipart([("file", "notes.txt", b"hello nexus", "text/plain")])
    status, payload = upload(base, f"/api/v1/projects/{pid}/files", body, ctype, session=alice)
    assert status == 201
    fid = payload["file"]["id"]
    assert payload["file"]["filename"] == "notes.txt"
    assert payload["file"]["size"] == 11
    assert payload["file"]["is_archive"] is False

    status, listing = request(base, f"/api/v1/projects/{pid}/files", session=alice)
    assert status == 200 and [f["id"] for f in listing["files"]] == [fid]

    status, preview = request(base, f"/api/v1/projects/{pid}/files/{fid}", session=alice)
    assert status == 200
    assert preview["file"]["kind"] == "text" and preview["file"]["text"] == "hello nexus"

    # A binary download is not JSON, so a raw fetch proves the bytes and headers.
    req = urllib.request.Request(base + f"/api/v1/projects/{pid}/files/{fid}/download")
    req.add_header("Cookie", "dispatcher_session=" + alice.token)
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.status == 200
        assert response.read() == b"hello nexus"
        assert "attachment" in response.headers.get("Content-Disposition", "")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"


def test_upload_requires_a_session(http_server):
    base, alice, _, _, _ = http_server
    pid = new_project(base, alice)
    body, ctype = multipart([("file", "a.txt", b"x", "text/plain")])
    assert upload(base, f"/api/v1/projects/{pid}/files", body, ctype)[0] == 401


def test_upload_without_csrf_is_refused(http_server):
    base, alice, _, _, _ = http_server
    pid = new_project(base, alice)
    body, ctype = multipart([("file", "a.txt", b"x", "text/plain")])
    assert upload(base, f"/api/v1/projects/{pid}/files", body, ctype, session=alice, csrf=False)[0] == 403


def test_upload_to_another_users_project_is_404(http_server):
    base, alice, bob, _, _ = http_server
    pid = new_project(base, alice)
    body, ctype = multipart([("file", "a.txt", b"x", "text/plain")])
    assert upload(base, f"/api/v1/projects/{pid}/files", body, ctype, session=bob)[0] == 404


def test_upload_without_a_file_part_is_400(http_server):
    base, alice, _, _, _ = http_server
    pid = new_project(base, alice)
    body, ctype = multipart([("notafile", "value", None, None)])
    assert upload(base, f"/api/v1/projects/{pid}/files", body, ctype, session=alice)[0] == 400


def test_a_hostile_filename_is_stored_as_a_bare_name(http_server):
    base, alice, _, _, _ = http_server
    pid = new_project(base, alice)
    body, ctype = multipart([("file", "../../etc/passwd", b"x", "text/plain")])
    status, payload = upload(base, f"/api/v1/projects/{pid}/files", body, ctype, session=alice)
    assert status == 201
    assert payload["file"]["filename"] == "passwd"


def test_delete_file(http_server):
    base, alice, _, _, _ = http_server
    pid = new_project(base, alice)
    body, ctype = multipart([("file", "a.txt", b"x", "text/plain")])
    fid = upload(base, f"/api/v1/projects/{pid}/files", body, ctype, session=alice)[1]["file"]["id"]
    assert request(base, f"/api/v1/projects/{pid}/files/{fid}", "DELETE", session=alice)[0] == 200
    assert request(base, f"/api/v1/projects/{pid}/files", session=alice)[1]["files"] == []


# -------------------------------------------------------------- workspaces
def test_extract_archive_into_workspace(http_server):
    base, alice, _, _, _ = http_server
    pid = new_project(base, alice)
    archive = make_zip({"src/a.txt": b"hello", "src/sub/b.txt": b"world"})
    body, ctype = multipart([("file", "project.zip", archive, "application/zip")])
    fid = upload(base, f"/api/v1/projects/{pid}/files", body, ctype, session=alice)[1]["file"]["id"]

    status, payload = request(base, f"/api/v1/projects/{pid}/workspaces/from-file", "POST",
                              {"file_id": fid, "name": "extracted"}, session=alice)
    assert status == 201
    wsid = payload["workspace"]["id"]
    assert payload["workspace"]["file_count"] == 2

    status, listing = request(base, f"/api/v1/projects/{pid}/workspaces/{wsid}", session=alice)
    paths = {f["path"] for f in listing["files"]}
    assert paths == {"src/a.txt", "src/sub/b.txt"}

    status, one = request(base, f"/api/v1/projects/{pid}/workspaces/{wsid}/files/src/a.txt", session=alice)
    assert status == 200 and one["file"]["text"] == "hello"


def test_extract_rejects_a_traversal_archive(http_server):
    base, alice, _, _, _ = http_server
    pid = new_project(base, alice)
    archive = make_zip({"../../escape.txt": b"x"})
    body, ctype = multipart([("file", "evil.zip", archive, "application/zip")])
    fid = upload(base, f"/api/v1/projects/{pid}/files", body, ctype, session=alice)[1]["file"]["id"]
    status, payload = request(base, f"/api/v1/projects/{pid}/workspaces/from-file", "POST",
                              {"file_id": fid, "name": "evil"}, session=alice)
    assert status == 400 and "rejected" in payload["error"]
    # No workspace is left behind by a rejected archive.
    assert request(base, f"/api/v1/projects/{pid}/workspaces", session=alice)[1]["workspaces"] == []


def test_edit_workspace_file_and_see_the_diff(http_server):
    base, alice, _, _, _ = http_server
    pid = new_project(base, alice)
    archive = make_zip({"a.txt": b"original\n"})
    body, ctype = multipart([("file", "p.zip", archive, "application/zip")])
    fid = upload(base, f"/api/v1/projects/{pid}/files", body, ctype, session=alice)[1]["file"]["id"]
    wsid = request(base, f"/api/v1/projects/{pid}/workspaces/from-file", "POST",
                   {"file_id": fid, "name": "w"}, session=alice)[1]["workspace"]["id"]

    status, _ = request(base, f"/api/v1/projects/{pid}/workspaces/{wsid}/files", "POST",
                        {"path": "a.txt", "text": "changed\n"}, session=alice)
    assert status == 200
    diff = request(base, f"/api/v1/projects/{pid}/workspaces/{wsid}/diff", session=alice)[1]["diff"]
    assert len(diff) == 1 and diff[0]["path"] == "a.txt"
    assert "-original" in diff[0]["diff"] and "+changed" in diff[0]["diff"]


def test_workspace_file_path_traversal_is_refused(http_server):
    base, alice, _, _, _ = http_server
    pid = new_project(base, alice)
    wsid = request(base, f"/api/v1/projects/{pid}/workspaces", "POST",
                   {"name": "w"}, session=alice)[1]["workspace"]["id"]
    status, payload = request(base, f"/api/v1/projects/{pid}/workspaces/{wsid}/files", "POST",
                              {"path": "../../etc/passwd", "text": "x"}, session=alice)
    assert status == 400


def test_create_and_delete_workspace(http_server):
    base, alice, _, _, _ = http_server
    pid = new_project(base, alice)
    wsid = request(base, f"/api/v1/projects/{pid}/workspaces", "POST",
                   {"name": "w"}, session=alice)[1]["workspace"]["id"]
    assert request(base, f"/api/v1/projects/{pid}/workspaces", session=alice)[1]["workspaces"][0]["id"] == wsid
    assert request(base, f"/api/v1/projects/{pid}/workspaces/{wsid}", "DELETE", session=alice)[0] == 200
    assert request(base, f"/api/v1/projects/{pid}/workspaces", session=alice)[1]["workspaces"] == []


# ------------------------------------------------------------------ voice
def test_voice_note_without_transcript_is_honest(http_server):
    base, alice, _, _, _ = http_server
    pid = new_project(base, alice)
    body, ctype = multipart([("audio", "note.webm", b"\x00\x01\x02", "audio/webm"),
                             ("duration_ms", "1500", None, None)])
    status, payload = upload(base, f"/api/v1/projects/{pid}/voice", body, ctype, session=alice)
    assert status == 201
    note = payload["voice_note"]
    assert note["transcript"] is None and note["transcript_engine"] is None
    assert note["duration_ms"] == 1500


def test_voice_note_with_browser_transcript_is_labelled(http_server):
    base, alice, _, _, _ = http_server
    pid = new_project(base, alice)
    body, ctype = multipart([("audio", "n.webm", b"x", "audio/webm"),
                             ("transcript", "привет", None, None)])
    status, payload = upload(base, f"/api/v1/projects/{pid}/voice", body, ctype, session=alice)
    assert status == 201
    assert payload["voice_note"]["transcript"] == "привет"
    assert payload["voice_note"]["transcript_engine"] == "browser"


def test_voice_rejects_a_non_audio_content_type(http_server):
    base, alice, _, _, _ = http_server
    pid = new_project(base, alice)
    body, ctype = multipart([("audio", "n.txt", b"x", "text/plain")])
    assert upload(base, f"/api/v1/projects/{pid}/voice", body, ctype, session=alice)[0] == 400


# ---------------------------------------------------- guarded GitHub actions
def test_github_repos_read_is_read_only(http_server):
    base, alice, _, _, gh = http_server
    pid = new_project(base, alice)
    status, payload = request(base, f"/api/v1/projects/{pid}/github/repositories", session=alice)
    assert status == 200
    assert payload["count"] >= 2
    assert gh.gh.writes == []  # a read never writes


def test_github_repos_for_a_foreign_project_is_404(http_server):
    base, alice, bob, _, _ = http_server
    pid = new_project(base, alice)
    assert request(base, f"/api/v1/projects/{pid}/github/repositories", session=bob)[0] == 404


def test_repo_detail_sections(http_server):
    base, alice, _, _, _ = http_server
    pid = new_project(base, alice)
    base_path = f"/api/v1/projects/{pid}/github/repositories/Pavel1778/python-dispetcher"
    assert request(base, base_path + "/branches", session=alice)[1]["branches"][0]["name"] == "main"
    assert request(base, base_path + "/commits", session=alice)[1]["commits"][0]["sha"] == "abc123"
    issues = request(base, base_path + "/issues", session=alice)[1]["issues"]
    # The pull-request entry is filtered out of the issues list.
    assert [i["number"] for i in issues] == [1]
    assert request(base, base_path + "/pulls", session=alice)[1]["pulls"][0]["number"] == 3


def test_create_repository_requires_confirmation(http_server):
    base, alice, _, _, gh = http_server
    pid = new_project(base, alice)
    status, payload = request(base, f"/api/v1/projects/{pid}/github/repositories", "POST",
                              {"name": "nexus-project", "private": True}, session=alice)
    assert status == 201
    action = payload["action"]
    assert action["status"] == "pending"
    assert action["confirmation_token"]
    # Proposing did not touch GitHub.
    assert gh.gh.writes == []

    status, payload = request(base, f"/api/v1/projects/{pid}/actions/{action['id']}/confirm",
                              "POST", {"token": action["confirmation_token"]}, session=alice)
    assert status == 200
    assert payload["result"]["full_name"] == "Pavel1778/nexus-project"
    assert gh.gh.writes and gh.gh.writes[0][0] == "POST" and gh.gh.writes[0][1] == "/user/repos"


def test_confirming_with_a_wrong_token_is_403_and_does_not_write(http_server):
    base, alice, _, _, gh = http_server
    pid = new_project(base, alice)
    action = request(base, f"/api/v1/projects/{pid}/github/repositories", "POST",
                     {"name": "x", "private": True}, session=alice)[1]["action"]
    status, _ = request(base, f"/api/v1/projects/{pid}/actions/{action['id']}/confirm",
                        "POST", {"token": "not-the-token"}, session=alice)
    assert status == 403
    assert gh.gh.writes == []


def test_confirming_twice_does_not_create_two_repositories(http_server):
    base, alice, _, _, gh = http_server
    pid = new_project(base, alice)
    action = request(base, f"/api/v1/projects/{pid}/github/repositories", "POST",
                     {"name": "once", "private": True}, session=alice)[1]["action"]
    token = action["confirmation_token"]
    assert request(base, f"/api/v1/projects/{pid}/actions/{action['id']}/confirm",
                   "POST", {"token": token}, session=alice)[0] == 200
    assert request(base, f"/api/v1/projects/{pid}/actions/{action['id']}/confirm",
                   "POST", {"token": token}, session=alice)[0] == 409
    assert len([w for w in gh.gh.writes if w[1] == "/user/repos"]) == 1


def test_cancelled_action_is_not_confirmed(http_server):
    base, alice, _, _, gh = http_server
    pid = new_project(base, alice)
    action = request(base, f"/api/v1/projects/{pid}/github/repositories", "POST",
                     {"name": "x", "private": True}, session=alice)[1]["action"]
    assert request(base, f"/api/v1/projects/{pid}/actions/{action['id']}/cancel",
                   "POST", {}, session=alice)[0] == 200
    assert request(base, f"/api/v1/projects/{pid}/actions/{action['id']}/confirm",
                   "POST", {"token": action["confirmation_token"]}, session=alice)[0] == 409
    assert gh.gh.writes == []


def test_visibility_change_to_public_goes_through_confirmation(http_server):
    base, alice, _, _, gh = http_server
    pid = new_project(base, alice)
    path = (f"/api/v1/projects/{pid}/github/repositories/"
            "Pavel1778/python-dispetcher/visibility")
    status, payload = request(base, path, "POST", {"private": False}, session=alice)
    assert status == 201
    action = payload["action"]
    assert action["payload"]["private"] is False
    assert gh.gh.writes == []
    status, _ = request(base, f"/api/v1/projects/{pid}/actions/{action['id']}/confirm",
                        "POST", {"token": action["confirmation_token"]}, session=alice)
    assert status == 200
    assert gh.gh.writes[-1] == ("PATCH", "/repos/Pavel1778/python-dispetcher", {"private": False})


def test_a_foreign_user_cannot_confirm_someone_elses_action(http_server):
    base, alice, bob, _, _ = http_server
    pid = new_project(base, alice)
    action = request(base, f"/api/v1/projects/{pid}/github/repositories", "POST",
                     {"name": "x", "private": True}, session=alice)[1]["action"]
    assert request(base, f"/api/v1/projects/{pid}/actions/{action['id']}/confirm",
                   "POST", {"token": action["confirmation_token"]}, session=bob)[0] == 404


def test_pending_actions_list_shows_only_the_actors(http_server):
    base, alice, bob, _, _ = http_server
    pid = new_project(base, alice)
    request(base, f"/api/v1/projects/{pid}/github/repositories", "POST",
            {"name": "x", "private": True}, session=alice)
    assert len(request(base, f"/api/v1/projects/{pid}/actions", session=alice)[1]["actions"]) == 1
    assert request(base, f"/api/v1/projects/{pid}/actions", session=bob)[0] == 404
