"""Full terminal-output retrieval: the file a truncated event points at.

The agent-server cuts a terminal ``ObservationEvent`` at 30 000 characters and
writes the whole output to ``observations/terminal_output_<sha256[:8]>.txt``
beside the conversation; the event names only the directory. The output is read
through the conversation-scoped ``download-trajectory`` archive, the truncated
text is the key it is matched by, and the result is the exact file - never a
path a caller supplied.

No mock of the client: a real local HTTP server (``tests/fake_runtime.py``)
speaks the agent-server surface, and the client under test makes real requests
over a socket. The tests that need PostgreSQL skip cleanly without a DSN.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_direct import CloudDirectClient  # noqa: E402
from orchestration.openhands_tools import ArtifactsUnsupported  # noqa: E402
from tests.fake_runtime import FakeServer  # noqa: E402

CAP = 30_000


def _observation(content: str, *, save_dir="/var/openhands/conversations/x/observations",
                 event_id="ev-1") -> dict:
    return {
        "id": event_id,
        "kind": "ObservationEvent",
        "source": "environment",
        "tool_name": "terminal",
        "observation": {
            "kind": "TerminalObservation",
            "content": [{"type": "text", "text": content}],
            "exit_code": 0,
            "full_output_save_dir": save_dir,
        },
    }


def _seed(sandbox, conversation_id: str, *, shown: str, full: str | None) -> None:
    c = sandbox.runtime.conversations[conversation_id]
    c.observation_events = [_observation(shown)]
    if full is not None:
        name = "terminal_output_" + hashlib.sha256(full.encode()).hexdigest()[:8] + ".txt"
        c.output_files = {name: full}


def _direct(store, sandbox, conversation_id: str) -> CloudDirectClient:
    store.save_conversation_route(
        conversation_id, sandbox_id="sb", agent_server_url=sandbox.base_url,
        session_api_key=sandbox.runtime.expected_key)
    client = CloudDirectClient.__new__(CloudDirectClient)
    from orchestration.openhands_client import OpenHandsClient

    client._http = OpenHandsClient("", api_key="", timeout=10.0)
    client._cache = {}
    client.store = store
    return client


class _FakeStore:
    """Just enough of ``PostgresStore`` for ``_route_for``: a fixed route."""

    def __init__(self):
        self._route = None

    def save_conversation_route(self, conversation_id, *, sandbox_id,
                                agent_server_url, session_api_key):
        self._route = {"sandbox_id": sandbox_id, "agent_server_url": agent_server_url,
                       "session_api_key": session_api_key}

    def conversation_route(self, conversation_id):
        return self._route



# ---------------------------------------------------------------- detection
def test_a_truncated_observation_is_reported_and_a_short_one_is_not():
    with FakeServer() as sandbox:
        cid = sandbox.runtime.make_conversation()
        c = sandbox.runtime.conversations[cid]
        c.observation_events = [
            _observation("short output", event_id="short"),
            _observation("x" * CAP, event_id="cut"),
        ]
        store = _FakeStore()
        client = _direct(store, sandbox, cid)
        out = client.truncated_outputs(cid)
        assert [o["event_id"] for o in out] == ["cut"]
        assert out[0]["shown_chars"] == CAP
        assert out[0]["truncated"] is True


# ---------------------------------------------------------------- retrieval
def test_the_full_output_is_returned_byte_for_byte():
    with FakeServer() as sandbox:
        cid = sandbox.runtime.make_conversation()
        full = "x" * CAP + "\n" + "the part that was cut off\n" + "y" * 5_000
        _seed(sandbox, cid, shown=full[:CAP], full=full)
        client = _direct(_FakeStore(), sandbox, cid)
        result = client.full_terminal_output(cid, full[:CAP])
        assert result["text"] == full
        assert result["bytes"] == len(full.encode())
        assert result["sha256"] == hashlib.sha256(full.encode()).hexdigest()
        assert result["name"].startswith("terminal_output_")


def test_output_below_the_cap_has_no_artifact_and_is_not_flagged():
    with FakeServer() as sandbox:
        cid = sandbox.runtime.make_conversation()
        _seed(sandbox, cid, shown="a" * (CAP - 1), full=None)
        client = _direct(_FakeStore(), sandbox, cid)
        assert client.truncated_outputs(cid) == []


def test_output_exactly_at_the_cap_is_flagged_and_readable():
    with FakeServer() as sandbox:
        cid = sandbox.runtime.make_conversation()
        full = "z" * CAP
        _seed(sandbox, cid, shown=full, full=full)
        client = _direct(_FakeStore(), sandbox, cid)
        assert client.truncated_outputs(cid)[0]["shown_chars"] == CAP
        assert client.full_terminal_output(cid, full)["text"] == full


def test_a_missing_artifact_is_not_found_not_empty():
    """The file was never written, or the retention window passed: a 404."""
    from orchestration.cloud_client import CloudNotFound

    with FakeServer() as sandbox:
        cid = sandbox.runtime.make_conversation()
        _seed(sandbox, cid, shown="q" * CAP, full=None)
        sandbox.runtime.conversations[cid].trajectory_missing = True
        client = _direct(_FakeStore(), sandbox, cid)
        with pytest.raises(CloudNotFound):
            client.full_terminal_output(cid, "q" * CAP)


def test_an_artifact_that_does_not_match_the_preview_is_not_found():
    from orchestration.cloud_client import CloudNotFound

    with FakeServer() as sandbox:
        cid = sandbox.runtime.make_conversation()
        _seed(sandbox, cid, shown="a" * CAP, full="a" * CAP + "different tail")
        client = _direct(_FakeStore(), sandbox, cid)
        # A prefix that is not the start of any stored file: refused, not guessed.
        with pytest.raises(CloudNotFound):
            client.full_terminal_output(cid, "b" * CAP)


def test_a_traversal_shaped_file_name_in_the_archive_is_ignored():
    """A name with ``..`` is never read, even if a hostile archive carried one."""
    from orchestration.cloud_client import CloudNotFound

    with FakeServer() as sandbox:
        cid = sandbox.runtime.make_conversation()
        c = sandbox.runtime.conversations[cid]
        c.observation_events = [_observation("t" * CAP)]
        c.output_files = {"../../etc/passwd": "t" * CAP}
        client = _direct(_FakeStore(), sandbox, cid)
        with pytest.raises(CloudNotFound):
            client.full_terminal_output(cid, "t" * CAP)


def test_a_member_not_named_terminal_output_is_never_read():
    """Only ``terminal_output_*`` files are read; any other member is skipped."""
    from orchestration.cloud_client import CloudNotFound

    with FakeServer() as sandbox:
        cid = sandbox.runtime.make_conversation()
        c = sandbox.runtime.conversations[cid]
        c.observation_events = [_observation("s" * CAP)]
        # A plausible-looking but wrong file, whose content is the preview.
        c.output_files = {"secrets.txt": "s" * CAP}
        client = _direct(_FakeStore(), sandbox, cid)
        with pytest.raises(CloudNotFound):
            client.full_terminal_output(cid, "s" * CAP)


def test_a_corrupted_archive_is_an_error_not_a_crash():
    """A non-ZIP body is reported as an error, never read as if it were one."""
    from orchestration.cloud_client import CloudError

    with FakeServer() as sandbox:
        cid = sandbox.runtime.make_conversation()
        c = sandbox.runtime.conversations[cid]
        c.observation_events = [_observation("c" * CAP)]
        c.trajectory_raw = b"this is not a zip archive at all"
        client = _direct(_FakeStore(), sandbox, cid)
        with pytest.raises(CloudError):
            client.full_terminal_output(cid, "c" * CAP)


def test_an_oversized_compressed_archive_is_refused():
    """A body larger than the compressed limit is refused before it is parsed."""
    from orchestration.cloud_client import CloudUnavailable

    with FakeServer() as sandbox:
        cid = sandbox.runtime.make_conversation()
        c = sandbox.runtime.conversations[cid]
        c.observation_events = [_observation("o" * CAP)]
        # A tiny client limit makes the bound cheap to exercise.
        c.trajectory_raw = b"x" * 5000
        client = _direct(_FakeStore(), sandbox, cid)
        client.TRAJECTORY_MAX_BYTES = 1000
        with pytest.raises(CloudUnavailable):
            client.full_terminal_output(cid, "o" * CAP)


def test_a_zip_bomb_member_is_refused_by_the_decompressed_limit():
    """A small archive whose member expands hugely is refused, not buffered.

    The member's bytes *start with the preview*, so without the decompressed
    limit it would be read whole and returned (proving the bomb was buffered);
    the compressed body is well under ``TRAJECTORY_MAX_BYTES``, so only the
    member limit can stop it. This test fails if ``archive.read`` is used again.
    """
    import io
    import zipfile

    from orchestration.cloud_client import CloudNotFound

    with FakeServer() as sandbox:
        cid = sandbox.runtime.make_conversation()
        c = sandbox.runtime.conversations[cid]
        preview = "b" * CAP
        c.observation_events = [_observation(preview)]
        bomb = (preview + "Z" * (5 * 1024 * 1024)).encode()   # starts with the preview
        name = "terminal_output_" + hashlib.sha256(bomb).hexdigest()[:8] + ".txt"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{cid}/observations/{name}", bomb)
        c.trajectory_raw = buf.getvalue()
        client = _direct(_FakeStore(), sandbox, cid)
        # A limit below the member's decompressed size, well above its
        # compressed size: the compressed body passes, the member does not.
        client.OUTPUT_MAX_BYTES = 1024
        assert len(c.trajectory_raw) < client.TRAJECTORY_MAX_BYTES
        with pytest.raises(CloudNotFound):
            client.full_terminal_output(cid, preview)


def test_the_app_transport_says_unsupported_not_empty():
    from orchestration.cloud_client import CloudClient

    client = CloudClient("http://127.0.0.1:9", api_key="k")
    with pytest.raises(ArtifactsUnsupported):
        client.truncated_outputs("c")
    with pytest.raises(ArtifactsUnsupported):
        client.full_terminal_output("c", "x")


# ------------------------------------------------------------- over HTTP
# The routes are exercised through the real server, the real store and the real
# sandbox client (the direct transport against the fake agent-server). A run is
# started, its task's conversation is seeded with a truncated observation and
# the full-output file, and the routes are read the way the studio reads them.
DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
requires_db = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run the artifact HTTP tests")


@pytest.fixture
def studio(monkeypatch):
    if not DSN:
        pytest.skip("set TEST_SUPABASE_DB_URL")
    from tests.test_studio_api import studio_stack

    with studio_stack(monkeypatch, transport="direct") as stack:
        yield stack


def _one_task_plan():
    return json.dumps({"tasks": [
        {"id": "only", "title": "Одна задача", "description": "сделать",
         "acceptance": ["ок"], "dependencies": []},
    ]})


def _bind_repo(studio, project):
    from tests.test_studio_api import REPO

    status, body = studio.api.post(
        f"/api/v1/projects/{project}/repository", {"full_name": REPO})
    assert status == 200, body


def _start(studio):
    """A run with one task, and the task bound to a conversation on the fake
    agent-server. The loop is off for the session, so the binding is made here
    rather than waited for; the route under test resolves the client and the
    sandbox exactly as it would for a dispatched task."""
    from tests.test_studio_api import REPO

    project = studio.project()
    _bind_repo(studio, project)
    conversation = studio.conversation(project, mode="autonomous")
    studio.answers(_one_task_plan())
    status, reply = studio.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages",
        {"text": "Цель: сделать одну вещь", "mode": "autonomous"})
    assert status == 200, reply
    run_id = reply["messages"][2]["meta"]["run_id"]
    status, detail = studio.api.get(f"/api/v1/projects/{project}/runs/{run_id}")
    assert status == 200, detail
    task_id = detail["tasks"][0]["id"]
    # A conversation on the sandbox the direct transport talks to, bound to the
    # task and to a route, so the route reads it through the real client.
    cid = studio.sandbox.make_conversation()
    studio.app.store.save_conversation_route(
        cid, sandbox_id="sb",
        agent_server_url=studio.cloud.cloud.agent_server_url,
        session_api_key=studio.cloud.cloud.sandbox_session_key)
    with studio.app.store._tx() as cur:
        cur.execute("UPDATE nexus_run_tasks SET conversation_id=%s WHERE id=%s",
                    (cid, task_id))
    assert REPO  # the repository is bound; a task of the run carries it
    return project, run_id, task_id, cid


def test_the_outputs_route_lists_only_truncated_events(studio):
    project, run_id, task_id, cid = _start(studio)
    full = "F" * CAP + " tail\n"
    conv = studio.sandbox.conversations[cid]
    conv.observation_events = [
        {"id": "short", "kind": "ObservationEvent", "tool_name": "terminal",
         "observation": {"kind": "TerminalObservation",
                         "content": [{"type": "text", "text": "small"}],
                         "full_output_save_dir": "/x/observations"}},
        {"id": "cut", "kind": "ObservationEvent", "tool_name": "terminal",
         "observation": {"kind": "TerminalObservation",
                         "content": [{"type": "text", "text": full[:CAP]}],
                         "full_output_save_dir": "/x/observations"}},
    ]
    name = "terminal_output_" + hashlib.sha256(full.encode()).hexdigest()[:8] + ".txt"
    conv.output_files = {name: full}

    status, body = studio.api.get(
        f"/api/v1/projects/{project}/runs/{run_id}/tasks/{task_id}/outputs")
    assert status == 200, body
    assert body["conversation_id"] == cid
    assert [o["event_id"] for o in body["outputs"]] == ["cut"]
    assert body["outputs"][0]["shown_chars"] == CAP

    status, got = studio.api.get(
        f"/api/v1/projects/{project}/runs/{run_id}/tasks/{task_id}/outputs/cut")
    assert status == 200, got
    assert got["text"] == full
    assert got["bytes"] == len(full.encode())
    assert got["event_id"] == "cut"
    # The filesystem path the event named is never returned to the caller.
    assert "/x/observations" not in json.dumps(got)


def test_an_unknown_event_is_404_and_a_foreign_project_is_404(studio):
    project, run_id, task_id, cid = _start(studio)
    studio.sandbox.conversations[cid].observation_events = []

    status, _ = studio.api.get(
        f"/api/v1/projects/{project}/runs/{run_id}/tasks/{task_id}/outputs/nope")
    assert status == 404
    # A project the caller is not a member of is "not found", never "forbidden".
    foreign = "11111111-1111-1111-1111-111111111111"
    status, _ = studio.api.get(
        f"/api/v1/projects/{foreign}/runs/{run_id}/tasks/{task_id}/outputs/nope")
    assert status == 404


def test_a_task_with_no_conversation_reports_no_outputs(studio):
    """A task that has not been dispatched yet has no conversation and no
    artifact; the route says so with an empty list, never an error."""
    from tests.test_studio_api import REPO

    project = studio.project()
    _bind_repo(studio, project)
    conversation = studio.conversation(project, mode="autonomous")
    studio.answers(_one_task_plan())
    status, reply = studio.api.post(
        f"/api/v1/projects/{project}/conversations/{conversation}/messages",
        {"text": "Цель: сделать одну вещь", "mode": "autonomous"})
    assert status == 200, reply
    run_id = reply["messages"][2]["meta"]["run_id"]
    # No loop runs here (NEXUS_LAB_INLINE=0 for the session), so the task keeps
    # no conversation; ask for its outputs at once.
    status, detail = studio.api.get(f"/api/v1/projects/{project}/runs/{run_id}")
    assert status == 200, detail
    task_id = detail["tasks"][0]["id"]
    assert REPO
    status, body = studio.api.get(
        f"/api/v1/projects/{project}/runs/{run_id}/tasks/{task_id}/outputs")
    assert status == 200, body
    assert body["outputs"] == []
    assert body["conversation_id"] is None



