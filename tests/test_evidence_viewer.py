"""The evidence viewer: the Trust Engine made visible in the studio.

Roadmap P1 #3 names the studio screens for backend-only capabilities. The
evidence viewer is the one that matters most: the Evidence Trust Engine is what
makes a "verified" result mean something, and until now an operator could not see
a single record without the CLI.

The cases here drive the **real** server over real PostgreSQL with a real
Ed25519 signer, and the **real** endpoint the viewer calls:

* ``GET .../evidence`` lists the project's records, newest first, and never
  another project's;
* ``POST .../evidence/{id}/verify`` re-verifies against the trusted keyring and
  answers with the engine's verdict - a record signed by an untrusted key is
  ``UNVERIFIED``, never ``VERIFIED``;
* a tampered record is ``FAILED``;
* ``/api/studio`` says whether records can be signed at all, so an empty list
  can be explained instead of reading as "nothing was verified".

The verdicts are the engine's; nothing here is asserted from the UI's word.
"""
from __future__ import annotations

import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.evidence_trust import (  # noqa: E402
    FAILED,
    UNVERIFIED,
    VERIFIED,
    EvidenceTrustEngine,
    Keyring,
    generate_signer,
    public_key_b64,
    signer_from_env,
)
from orchestration.nexus_evidence import EvidenceService  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run the evidence viewer tests")

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
OWNER = "user:owner"


def _reset() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


def _signer_env(monkeypatch, key_id: str = "nexus-viewer"):
    signer = generate_signer(key_id)
    seed = signer.private_key.private_bytes_raw()
    monkeypatch.setenv("NEXUS_EVIDENCE_KEY_ID", key_id)
    monkeypatch.setenv("NEXUS_EVIDENCE_PRIVATE_KEY",
                       base64.urlsafe_b64encode(seed).decode("ascii"))
    monkeypatch.setenv("NEXUS_EVIDENCE_KEYRING", f"{key_id}:{public_key_b64(signer)}")
    return signer


@pytest.fixture
def served(monkeypatch):
    """The real web server on real PostgreSQL, with a real signer configured."""
    _reset()
    _signer_env(monkeypatch)
    from tests.test_studio_api import studio_stack

    with studio_stack(monkeypatch) as stack:
        yield stack


def _record(stack, project_id, **kwargs):
    """Sign one record through the server's own evidence service."""
    body = {"subject_kind": "manual", "subject_id": None, "project_id": project_id,
            "repository_full_name": "acme/repo", "commit_sha": "a" * 40,
            "branch": "main", "summary": "checked", "created_by": "tester"}
    body.update(kwargs)
    return stack.app.evidence.record(**body)


# ------------------------------------------------------------------ the list
def test_the_list_returns_the_projects_records(served):
    project = served.project("Evidence")
    other = served.project("Other")
    rec = _record(served, project)

    status, body = served.api.get(f"/api/v1/projects/{project}/evidence")
    assert status == 200
    ids = [item["id"] for item in body["evidence"]]
    assert rec["id"] in ids
    # The fields the viewer shows are on the record.
    item = next(i for i in body["evidence"] if i["id"] == rec["id"])
    assert item["verdict"] == VERIFIED
    assert item["repository_full_name"] == "acme/repo"
    assert item["commit_sha"] == "a" * 40
    assert item["created_at"] is not None

    # Another project never sees it.
    status, body = served.api.get(f"/api/v1/projects/{other}/evidence")
    assert status == 200
    assert rec["id"] not in [i["id"] for i in body["evidence"]]


def test_the_list_requires_auth(served):
    project = served.project("Evidence")
    from tests.test_studio_api import Api

    anonymous = Api(served.api.base, token=None)
    status, _ = anonymous.get(f"/api/v1/projects/{project}/evidence")
    assert status == 401


# ---------------------------------------------------------------- the re-check
def test_a_record_signed_by_the_trusted_key_verifies(served):
    project = served.project("Evidence")
    rec = _record(served, project)
    status, body = served.api.post(
        f"/api/v1/projects/{project}/evidence/{rec['id']}/verify", {})
    assert status == 200
    assert body["evidence"]["verdict"] == VERIFIED
    assert body["evidence"]["verification_detail"]["checks"]


def test_a_record_signed_by_an_unknown_key_is_unverified(served):
    """A key_id the keyring does not know cannot produce VERIFIED."""
    project = served.project("Evidence")
    rec = _record(served, project)
    # The server now trusts only some *other* key: the record's key_id is not
    # in the keyring at all, so nothing about it is established.
    keyring = Keyring()
    keyring.add("some-other-key", public_key_b64(generate_signer("some-other-key")))
    served.app.evidence = EvidenceService(
        served.app.store,
        EvidenceTrustEngine(keyring, signer_from_env()))
    status, body = served.api.post(
        f"/api/v1/projects/{project}/evidence/{rec['id']}/verify", {})
    assert status == 200
    assert body["evidence"]["verdict"] == UNVERIFIED


def test_a_trusted_key_id_with_the_wrong_key_is_failed(served):
    """The key_id is trusted but the signature was made by a different key: the
    engine calls that a failed signature, which is stronger than 'unknown'."""
    project = served.project("Evidence")
    rec = _record(served, project)
    keyring = Keyring()
    keyring.add("nexus-viewer", public_key_b64(generate_signer("nexus-viewer")))
    served.app.evidence = EvidenceService(
        served.app.store,
        EvidenceTrustEngine(keyring, signer_from_env()))
    status, body = served.api.post(
        f"/api/v1/projects/{project}/evidence/{rec['id']}/verify", {})
    assert status == 200
    assert body["evidence"]["verdict"] == FAILED
    assert "signature" in body["evidence"]["verification_detail"]["reason"]


def test_a_tampered_record_fails(served):
    """Changing a signed field after signing is caught by the engine."""
    import psycopg

    project = served.project("Evidence")
    rec = _record(served, project)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("UPDATE nexus_evidence SET commit_sha=%s WHERE id=%s",
                     ("b" * 40, rec["id"]))
    status, body = served.api.post(
        f"/api/v1/projects/{project}/evidence/{rec['id']}/verify", {})
    assert status == 200
    assert body["evidence"]["verdict"] == FAILED


def test_a_foreign_record_is_a_404(served):
    """A record of another project is "not found", never disclosed."""
    project = served.project("Evidence")
    other = served.project("Other")
    rec = _record(served, other)
    status, _ = served.api.post(
        f"/api/v1/projects/{project}/evidence/{rec['id']}/verify", {})
    assert status == 404


# ------------------------------------------------------- what the studio is told
def test_the_studio_says_whether_records_can_be_signed(served):
    """With a signer the page says evidence is available; without one, it does not."""
    status, body = served.api.get("/api/studio")
    assert status == 200
    assert body["lab"]["evidence"] is True


def test_without_a_signer_the_studio_says_so(served, monkeypatch):
    """An empty evidence list has to be explainable: no signer, no records."""
    for name in ("NEXUS_EVIDENCE_PRIVATE_KEY", "NEXUS_EVIDENCE_KEYRING",
                 "NEXUS_EVIDENCE_KEY_ID"):
        monkeypatch.delenv(name, raising=False)
    # A new App reads the environment again, as a redeploy would.
    served.boot()
    status, body = served.api.get("/api/studio")
    assert status == 200
    assert body["lab"]["evidence"] is False


def test_an_empty_project_returns_an_empty_list_not_an_error(served):
    """The viewer's "nothing yet" case is a plain empty list, so the page can
    show its own explanation (which needs ``lab.evidence`` to say *why*)."""
    project = served.project("Empty")
    status, body = served.api.get(f"/api/v1/projects/{project}/evidence")
    assert status == 200
    assert body["evidence"] == []
