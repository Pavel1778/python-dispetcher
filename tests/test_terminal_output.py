"""Terminal output recovery, tested at the boundary and over real HTTP.

Honest scope (verified against the exact agent-server 1.60.1 artifacts on
2026-11-15): the raw untrimmed stdout/stderr of a command is discarded at
the session layer - every ``maybe_truncate`` in ``terminal_session.py``
passes no ``save_dir`` - so the file the SDK saves carries the clipped,
<=30,000-char rendering plus the prefix/suffix/wrapper lines. What this
module recovers is that SDK-saved rendering, byte-exact, or nothing at
all. These tests pin:

* the lexical validation of the untrusted ``full_output_save_dir``;
* the hash-preimage reconstruction, checked against the SDK's own
  construction (``TerminalObservation.to_llm_content``), not against this
  module's own code;
* the API over real HTTP with a real PostgreSQL and the stand-in
  agent-server (``tests/fake_runtime.py``) holding real files - list,
  probe, fetch, download, and every refusal: 404/413/501/502;
* the audit line ``terminal_output.read`` on a successful read.

Also covered: ref extraction (terminal vs non-terminal events), the
preview cleanup, and the "no path ever leaves the process" rule.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from orchestration import terminal_output as to  # noqa: E402
from tests.test_studio_api import Api, studio_stack  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to a disposable PostgreSQL database")

OWNER = "user:owner"


# ------------------------------------------------------------ unit boundary
def test_normalize_save_dir_accepts_a_plain_absolute_posix_path():
    assert to.normalize_save_dir("/workspace/conversations/abc/observations") == \
        "/workspace/conversations/abc/observations"
    assert to.normalize_save_dir("/a.b/c-d/e_f") == "/a.b/c-d/e_f"


@pytest.mark.parametrize("bad", [
    "",                       # empty
    "tmp/x",                  # not absolute
    "/a//b",                  # empty component
    "/a/./b",                 # dot component
    "/a/../b",                # traversal
    "/workspace/../../etc",   # traversal, multi-step
    "/a\\b",                  # backslash
    "/a\x00b",                # NUL
    "/a b/c",                 # space in a component
    "/a/b!",                  # surprise character
    "/-leading-dash",         # component may not start with '-'
    "/.hidden",               # component may not start with '.'
    "/a/b/",                  # trailing slash -> empty component
    "/" + "x" * 600,          # too long
    "/" + "ab/" * 220,          # longer than 512 characters
    123,                      # not a string
    None,
])
def test_normalize_save_dir_rejects_anything_the_sandbox_must_not_name(bad):
    assert to.normalize_save_dir(bad) is None


def test_normalize_save_dir_refuses_a_runtime_emitted_escape_shaped_value():
    # A sandbox that answers with a Windows-shaped path gets refused, not
    # "fixed up" into something else.
    assert to.normalize_save_dir("C:\\Users\\agent\\observations") is None
    assert to.normalize_save_dir("..\\..\\observations") is None


def _observation(*, prefix="PFX:", text="hello output", suffix=":SFX",
                 working_dir="/wd", interpreter="/py", exit_code=3,
                 content=None, save_dir="/workspace/conversations/c/observations"):
    return {
        "kind": "TerminalObservation",
        "command": "make test",
        "exit_code": exit_code,
        "is_error": False,
        "metadata": {
            "exit_code": exit_code,
            "working_dir": working_dir,
            "py_interpreter_path": interpreter,
            "prefix": prefix,
            "suffix": suffix,
        },
        "content": content if content is not None else [
            {"type": "text", "text": text}],
        "full_output_save_dir": save_dir,
    }


def test_expected_file_name_matches_the_sdk_hash_formula():
    # Built by hand from what openhands-tools 1.60.1 does in
    # TerminalObservation.to_llm_content + sdk.utils.truncate.maybe_truncate.
    obs = _observation()
    ret = ("PFX:hello output:SFX"
           "\n[Current working directory: /wd]"
           "\n[Python interpreter: /py]"
           "\n[Command finished with exit code 3]")
    digest = hashlib.sha256(ret.encode("utf-8")).hexdigest()[:8]
    assert to.expected_file_name(obs) == f"terminal_output_{digest}.txt"


def test_expected_file_name_with_exit_code_minus_one_omits_the_exit_line():
    obs = _observation(exit_code=-1)
    ret = ("PFX:hello output:SFX"
           "\n[Current working directory: /wd]"
           "\n[Python interpreter: /py]")
    digest = hashlib.sha256(ret.encode("utf-8")).hexdigest()[:8]
    assert to.expected_file_name(obs) == f"terminal_output_{digest}.txt"


def test_expected_file_name_without_optional_lines():
    obs = _observation(working_dir=None, interpreter=None, exit_code=-1)
    ret = "PFX:hello output:SFX"
    digest = hashlib.sha256(ret.encode("utf-8")).hexdigest()[:8]
    assert to.expected_file_name(obs) == f"terminal_output_{digest}.txt"


def test_expected_file_name_refuses_when_prefix_or_suffix_missing():
    obs = _observation()
    del obs["metadata"]["prefix"]
    assert to.expected_file_name(obs) is None
    obs = _observation()
    obs["metadata"]["suffix"] = 5
    assert to.expected_file_name(obs) is None


def test_expected_file_name_joins_multiple_text_parts_like_the_sdk():
    content = [{"type": "text", "text": "one "},
               {"type": "image", "image": "data"},
               {"type": "text", "text": "two"}]
    obs = _observation(content=content)
    ret = ("PFX:one two:SFX"
           "\n[Current working directory: /wd]"
           "\n[Python interpreter: /py]"
           "\n[Command finished with exit code 3]")
    digest = hashlib.sha256(ret.encode("utf-8")).hexdigest()[:8]
    assert to.expected_file_name(obs) == f"terminal_output_{digest}.txt"


def test_output_path_is_recomputed_never_taken_from_the_event():
    obs = _observation(save_dir="/workspace/conversations/c/observations")
    recomputed = to.output_path(obs)
    assert recomputed.startswith(
        "/workspace/conversations/c/observations/terminal_output_")
    # A directory the event merely claims is refused lexically; there is no
    # way to name an arbitrary file through this path.
    assert to.output_path(_observation(save_dir="/etc/../tmp/observations")) is None
    assert to.output_path(_observation(save_dir="relative/observations")) is None
    assert to.output_path(_observation(save_dir="/a/b/")) is None


def test_clipped_in_stream_detects_the_sdk_notice():
    assert to.clipped_in_stream("abc <response clipped><NOTE> x") is True
    assert to.clipped_in_stream("all there") is False


def _events(*, prefix="PFX:", text="make ran", exit_code=0,
            ts="2026-11-15T10:00:00Z", eid="evt-1",
            save_dir="/workspace/conversations/c/observations"):
    return [{
        "id": eid,
        "kind": "ObservationEvent",
        "source": "environment",
        "tool_name": "terminal",
        "timestamp": ts,
        "action_id": "act-1",
        "observation": _observation(prefix=prefix, text=text, exit_code=exit_code,
                                    save_dir=save_dir),
    }]


def test_extract_refs_keeps_only_terminal_observations():
    events = _events() + [
        {"id": "evt-fin", "kind": "ObservationEvent", "tool_name": "finish",
         "observation": {"kind": "FinishObservation"}},
        {"id": "evt-act", "kind": "ActionEvent", "tool_name": "terminal",
         "action": {"kind": "TerminalAction", "command": "ls"}},
    ]
    refs = to.extract_refs(events, "cid")
    assert [ref.event_id for ref in refs] == ["evt-1"]


def test_extract_refs_reports_the_size_the_clip_and_the_command():
    body = "start <response clipped><NOTE> middle end"
    refs = to.extract_refs(_events(text=body, exit_code=17), "cid")
    ref = refs[0]
    assert ref.command == "make test"
    assert ref.exit_code == 17
    assert ref.output_chars == len(body)
    assert ref.clipped is True
    assert ref.preview.startswith("start")
    assert ref.saved_output == "unknown"      # probed later by the caller


def test_extract_refs_reports_missing_when_no_save_dir_is_possible():
    refs = to.extract_refs(_events(save_dir=None), "cid")
    assert refs[0].saved_output == "missing"


def test_extract_refs_sorts_newest_first_and_caps():
    events = []
    for n in range(3):
        events += _events(eid=f"e{n}", ts=f"2026-11-{10 + n:02d}T10:00:00Z")
    refs = to.extract_refs(events, "cid")
    assert [r.event_id for r in refs] == ["e2", "e1", "e0"]


def test_ref_public_never_carries_the_path_or_the_hash():
    ref = to.extract_refs(_events(), "cid")[0]
    public = to.ref_public(ref)
    text = json.dumps(public)
    assert ref._path and ref._path not in text
    assert "full_output_save_dir" not in text
    assert "_path" not in public


def test_capability_distinguishes_transports():
    from orchestration.openhands_client import OpenHandsClient
    assert to.capability(OpenHandsClient("http://localhost:1")) == "agent-server"

    class Double:  # honours the download_file contract
        def download_file(self, path, *, limit):
            return 200, b""

    assert to.capability(Double()) == "agent-server"
    assert to.capability(object()) == "unsupported"


class _StubClient:
    """Speaks the minted download_file contract; nothing else."""

    def __init__(self, mapping):
        self.mapping = mapping

    def download_file(self, path, *, limit):
        value = self.mapping.get(path)
        if value is None:
            return 404, b""
        return value


def test_fetch_full_output_returns_the_bytes_whole():
    ref = to.extract_refs(_events(), "cid")[0]
    payload = b"PFX:data:SFX\nfull bytes \xf0\x9f\x9a\x80"
    client = _StubClient({ref._path: (200, payload)})
    assert to.fetch_full_output(client, ref) == payload


def test_fetch_full_output_404_is_unavailable_not_empty():
    ref = to.extract_refs(_events(), "cid")[0]
    client = _StubClient({ref._path: (404, b"")})
    with pytest.raises(to.OutputUnavailable):
        to.fetch_full_output(client, ref)


def test_fetch_full_output_refuses_a_body_over_the_cap():
    ref = to.extract_refs(_events(), "cid")[0]
    client = _StubClient({ref._path: (200, b"x" * (to.MAX_OUTPUT_BYTES + 1))})
    with pytest.raises(to.FileTooLarge):
        to.fetch_full_output(client, ref)


def test_fetch_full_output_refuses_a_ref_without_a_path():
    ref = to.extract_refs(_events(save_dir=None), "cid")[0]

    class Exploding:
        def download_file(self, *args, **kwargs):
            raise AssertionError("the transport must not be called")

    with pytest.raises(to.OutputRefused):
        to.fetch_full_output(Exploding(), ref)


# ------------------------------------------------------- over real HTTP
@pytest.fixture
def direct(monkeypatch):
    # One stack per test, as everywhere in this suite: the schema is reset,
    # the server runs on a real port, and the direct transport talks to the
    # stand-in agent-server over a second socket.
    with studio_stack(monkeypatch, transport="direct") as stack:
        yield stack


@pytest.fixture
def app_stack(monkeypatch):
    with studio_stack(monkeypatch, transport="app") as stack:
        yield stack


def _start_sandbox_conversation(stack):
    starter = stack.app.client.start_conversation(
        initial_message="say hi", title="terminal-output test")
    assert starter.ready
    return starter.id


def _seed_run(stack, project_id: str, client_cid: str) -> str:
    goal, _ = stack.app.projects.create_goal(OWNER, project_id, "Ship the thing")
    run = stack.app.lab.start_run(OWNER, project_id, str(goal["id"]),
                                  policy="durable")
    run_id = str(run["id"])
    task = stack.app.lab.add_task(OWNER, project_id, run_id,
                                  task_key="t1", title="Do it")
    with stack.app.store._tx() as cur:
        cur.execute(
            "INSERT INTO nexus_task_executions(run_id, task_id, project_id, task_key, "
            "  kind, conversation_id, executor_label) "
            "VALUES (%s,%s,%s,'t1','start',%s,'')",
            (run_id, str(task["id"]), project_id, client_cid))
    return run_id


def _seed_output(stack, conversation_id: str, *, body_bytes: bytes | None = None,
                 save_dir: str | None = None):
    """Write what the SDK would have written and return ``(ret, path)``."""
    body = (body_bytes if isinstance(body_bytes, bytes)
            else b"command output").decode("utf-8", errors="replace")
    obs = _observation(prefix="PFX:", text=body, exit_code=0,
                       working_dir="/workspace", interpreter="/usr/bin/python3",
                       save_dir=save_dir or f"/workspace/conversations/{conversation_id}/observations")
    rendered = to.render_observation(obs)
    assert rendered is not None
    path = f"{obs['full_output_save_dir']}/terminal_output_" + \
        hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:8] + ".txt"
    event = {
        "id": f"evt-{str(conversation_id)[:12]}".replace("-", ""),
        "kind": "ObservationEvent",
        "source": "environment",
        "tool_name": "terminal",
        "timestamp": "2026-11-15T12:00:00Z",
        "action_id": "act-1",
        "observation": obs,
    }
    stack.sandbox.set_events(conversation_id, [event])
    return rendered, path, event


def test_list_and_fetch_roundtrip_through_the_api(direct):
    studio = direct
    project_id = studio.project()
    cid = _start_sandbox_conversation(studio)
    run_id = _seed_run(studio, project_id, cid)
    rendered, path, event = _seed_output(studio, cid)
    studio.sandbox.files[path] = rendered.encode("utf-8")

    status, body = studio.api.get(
        f"/api/v1/projects/{project_id}/runs/{run_id}/terminal-outputs")
    assert status == 200, body
    assert body["truncated"] is False
    assert body["unavailable"] == []
    assert len(body["outputs"]) == 1
    item = body["outputs"][0]
    assert item["conversation_id"] == cid
    assert item["command"] == "make test"
    assert item["exit_code"] == 0
    assert item["saved_output"] == "available"
    assert item["output_chars"] == len("command output")

    status, payload = studio.api.get(
        f"/api/v1/projects/{project_id}/runs/{run_id}/terminal-outputs/{cid}/{event['id']}")
    assert status == 200, payload
    assert payload["bytes"] == len(rendered.encode("utf-8"))
    assert payload["text"] == rendered

    # The download is the bytes exactly, with an attachment disposition.
    request = urllib.request.Request(
        studio.api.base
        + f"/api/v1/projects/{project_id}/runs/{run_id}/terminal-outputs/{cid}/{event['id']}/download")
    request.add_header("Authorization", f"Bearer {studio.api.token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/plain")
        assert "attachment" in response.headers["Content-Disposition"]
        assert response.read() == rendered.encode("utf-8")


def test_the_read_is_recorded_as_an_audit_line(direct):
    studio = direct
    project_id = studio.project()
    cid = _start_sandbox_conversation(studio)
    run_id = _seed_run(studio, project_id, cid)
    rendered, path, event = _seed_output(studio, cid)
    studio.sandbox.files[path] = rendered.encode("utf-8")
    status, _ = studio.api.get(
        f"/api/v1/projects/{project_id}/runs/{run_id}/terminal-outputs/{cid}/{event['id']}")
    assert status == 200
    with studio.app.store._tx() as cur:
        cur.execute(
            "SELECT kind FROM nexus_project_events WHERE project_id=%s AND kind='terminal_output.read'",
            (project_id,))
        assert cur.fetchall(), "the read must leave an audit line"


def test_a_missing_file_reports_missing_then_404_not_empty_bytes(direct):
    studio = direct
    project_id = studio.project()
    cid = _start_sandbox_conversation(studio)
    run_id = _seed_run(studio, project_id, cid)
    rendered, path, event = _seed_output(studio, cid)
    # The sandbox does not hold the file any more.
    status, body = studio.api.get(
        f"/api/v1/projects/{project_id}/runs/{run_id}/terminal-outputs")
    assert status == 200
    assert body["outputs"][0]["saved_output"] == "missing"
    status, payload = studio.api.get(
        f"/api/v1/projects/{project_id}/runs/{run_id}/terminal-outputs/{cid}/{event['id']}")
    assert status == 404


def test_a_body_over_the_cap_is_a_413_not_a_truncated_log(direct):
    studio = direct
    project_id = studio.project()
    cid = _start_sandbox_conversation(studio)
    run_id = _seed_run(studio, project_id, cid)
    rendered, path, event = _seed_output(studio, cid)
    studio.sandbox.files[path] = b"x" * (to.MAX_OUTPUT_BYTES + 1)
    status, payload = studio.api.get(
        f"/api/v1/projects/{project_id}/runs/{run_id}/terminal-outputs/{cid}/{event['id']}")
    assert status == 413
    assert payload["error"] == "output_too_large"
    # And the list stays honest: the probe does not lie about presence.
    status, body = studio.api.get(
        f"/api/v1/projects/{project_id}/runs/{run_id}/terminal-outputs")
    assert status == 200
    assert body["outputs"][0]["saved_output"] == "available"  # it exists; just too big


def test_a_conversation_from_another_run_is_not_readable_through_this_run(direct):
    studio = direct
    project_id = studio.project()
    cid = _start_sandbox_conversation(studio)
    _seed_run(studio, project_id, cid)
    _, path, event = _seed_output(studio, cid)
    studio.sandbox.files[path] = b"data"
    # A second run, with its own execution, gets a 404 for the first
    # conversation: the run_id scopes what may be read.
    goal, _ = studio.app.projects.create_goal(OWNER, project_id, "Another goal")
    other = studio.app.lab.start_run(OWNER, project_id, str(goal["id"]),
                                     policy="durable")
    status, payload = studio.api.get(
        f"/api/v1/projects/{project_id}/runs/{other['id']}/terminal-outputs/{cid}/{event['id']}")
    assert status == 404


def test_the_route_is_project_scoped_and_the_event_id_is_validated(direct):
    studio = direct
    project_id = studio.project()
    other_project = studio.project("Other")
    cid = _start_sandbox_conversation(studio)
    run_id = _seed_run(studio, project_id, cid)
    rendered, path, event = _seed_output(studio, cid)
    studio.sandbox.files[path] = rendered.encode()
    # The same run read through a different project's URL is a 404.
    status, _ = studio.api.get(
        f"/api/v1/projects/{other_project}/runs/{run_id}/terminal-outputs")
    assert status == 404
    # A malformed event id never reaches the sandbox.
    status, _ = studio.api.get(
        f"/api/v1/projects/{project_id}/runs/{run_id}/terminal-outputs/{cid}/bad%20id%21")
    assert status == 404
    # Traversal-shaped ids do not escape anything either.
    status, _ = studio.api.get(
        f"/api/v1/projects/{project_id}/runs/{run_id}/terminal-outputs/{cid}/..%2F..%2Fetc")
    assert status == 404


def test_an_unknown_conversation_is_reported_not_fatal(direct):
    studio = direct
    project_id = studio.project()
    # The execution names a conversation the sandbox does not know.
    ghost = "11111111-2222-4333-8444-555555555555"
    run_id = _seed_run(studio, project_id, ghost)
    status, body = studio.api.get(
        f"/api/v1/projects/{project_id}/runs/{run_id}/terminal-outputs")
    assert status == 200
    assert body["outputs"] == []
    assert {u["conversation_id"] for u in body["unavailable"]} == {ghost}


def test_the_app_transport_reports_unsupported_rather_than_claiming_files(app_stack):
    studio = app_stack
    project_id = studio.project()
    cid = "33333333-2222-4333-8444-555555555555"  # uuid-shaped, as real conversations
    studio.cloud.cloud.register(cid, "sandbox-1")
    rendered_event = {
        "id": "evt-app-1",
        "kind": "ObservationEvent",
        "source": "environment",
        "tool_name": "terminal",
        "timestamp": "2026-11-15T12:00:00Z",
        "observation": _observation(),
    }
    studio.cloud.cloud.events[cid] = [rendered_event]
    run_id = _seed_run(studio, project_id, cid)
    status, body = studio.api.get(
        f"/api/v1/projects/{project_id}/runs/{run_id}/terminal-outputs")
    assert status == 200
    assert body["outputs"][0]["saved_output"] == "unsupported"
    status, payload = studio.api.get(
        f"/api/v1/projects/{project_id}/runs/{run_id}/terminal-outputs/{cid}/evt-app-1")
    assert status == 501
    assert payload["error"] == "unsupported_transport"


def test_without_a_credential_nothing_is_served(direct):
    studio = direct
    status, _ = Api(studio.api.base, token=None).get(
        "/api/v1/projects/00000000-0000-4000-8000-000000000000/runs/"
        "00000000-0000-4000-8000-000000000001/terminal-outputs")
    assert status == 401
