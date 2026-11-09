"""Evidence Trust Engine: signing, verification and project isolation.

Two layers are covered:

* Pure crypto/verdict logic in :mod:`orchestration.evidence_trust` — no
  database needed. These pin the rule that **only** a trusted asymmetric
  signature yields VERIFIED, and that tampering with any signed field breaks it.
* The stored-record service in :mod:`orchestration.nexus_evidence` against real
  PostgreSQL: signing binds ``project_id``, legacy records stay unscoped, reads
  are project-scoped, and re-verification uses the real verifier.

The security-negative cases live in :mod:`tests.test_project_binding`; here the
focus is the engine itself.
"""
from __future__ import annotations

import base64
import json
import os

import pytest

from orchestration.cloud_store import PostgresStore
from orchestration.evidence_trust import (
    FAILED,
    UNVERIFIED,
    VERIFIED,
    EvidenceError,
    EvidenceTrustEngine,
    Keyring,
    Signer,
    canonical_bytes,
    entry_hash,
    generate_signer,
    public_key_b64,
)

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")


def _engine(key_id: str = "k1") -> tuple[EvidenceTrustEngine, Signer]:
    signer = generate_signer(key_id)
    ring = Keyring()
    ring.add(key_id, public_key_b64(signer))
    return EvidenceTrustEngine(ring, signer), signer


def _body(project_id: str | None = "11111111-1111-1111-1111-111111111111") -> dict:
    body = {
        "format_version": 2 if project_id else 1,
        "subject_kind": "task",
        "subject_id": "22222222-2222-2222-2222-222222222222",
        "repository_full_name": "acme/repo",
        "commit_sha": "abc123",
        "branch": "main",
        "summary": "build green",
        "detail": {"tests": 12},
        "created_by": "user:owner",
        "created_at": 1,
    }
    if project_id:
        body["project_id"] = project_id
    return body


def _record(engine: EvidenceTrustEngine, body: dict, *, prev: str | None = None) -> dict:
    fv, payload, kid, sig = engine.sign_body(body)
    return {
        "project_id": body.get("project_id"),
        "format_version": fv,
        "repository_full_name": body.get("repository_full_name"),
        "commit_sha": body.get("commit_sha"),
        "branch": body.get("branch"),
        "subject_kind": body["subject_kind"],
        "subject_id": body["subject_id"],
        "signed_payload": payload,
        "key_id": kid,
        "signature": sig,
        "prev_hash": prev,
        "entry_hash": entry_hash(prev, fv, body),
    }


# ------------------------------------------------------------------ happy path
def test_valid_signature_is_verified():
    engine, _ = _engine()
    rec = _record(engine, _body())
    verdict = engine.verify(rec)
    assert verdict.verdict == VERIFIED
    assert verdict.checks.get("signature_valid") is True
    assert verdict.checks.get("key_trusted") is True


def test_canonical_bytes_is_order_independent():
    a = {"b": 1, "a": 2}
    b = {"a": 2, "b": 1}
    assert canonical_bytes(a) == canonical_bytes(b)


# --------------------------------------------------------------- key handling
def test_unknown_key_is_unverified_not_verified():
    engine, _ = _engine()
    rec = _record(engine, _body())
    rec["key_id"] = "someone-else"
    assert engine.verify(rec).verdict == UNVERIFIED


def test_revoked_key_is_unverified():
    signer = generate_signer("k1")
    ring = Keyring()
    ring.add("k1", public_key_b64(signer), revoked=True)
    engine = EvidenceTrustEngine(ring, signer)
    rec = _record(engine, _body())
    assert engine.verify(rec).verdict == UNVERIFIED


def test_unsigned_record_is_unverified():
    engine, _ = _engine()
    rec = _record(engine, _body())
    rec["signature"] = ""
    assert engine.verify(rec).verdict == UNVERIFIED


def test_hmac_style_legacy_record_never_verifies():
    """A record with a symmetric-looking tag and no trusted key is UNVERIFIED."""
    engine, _ = _engine()
    rec = _record(engine, _body())
    # Simulate an HMAC-era record: a tag exists but the key id is not trusted.
    rec["key_id"] = "hmac:legacy"
    rec["signature"] = base64.urlsafe_b64encode(b"not-a-real-signature").decode()
    assert engine.verify(rec).verdict == UNVERIFIED


# ------------------------------------------------------------------- tampering
def test_tampering_with_project_id_column_fails():
    engine, _ = _engine()
    rec = _record(engine, _body())
    rec["project_id"] = "99999999-9999-9999-9999-999999999999"
    verdict = engine.verify(rec)
    assert verdict.verdict == FAILED
    assert verdict.checks.get("column_mismatch") == "project_id"


def test_tampering_with_commit_column_fails():
    engine, _ = _engine()
    rec = _record(engine, _body())
    rec["commit_sha"] = "deadbeef"
    assert engine.verify(rec).verdict == FAILED


def test_tampering_with_signed_payload_fails():
    engine, _ = _engine()
    rec = _record(engine, _body())
    body = json.loads(rec["signed_payload"])
    body["summary"] = "forged"
    rec["signed_payload"] = json.dumps(body, sort_keys=True, separators=(",", ":"))
    assert engine.verify(rec).verdict == FAILED


def test_expected_commit_mismatch_is_unverified():
    engine, _ = _engine()
    rec = _record(engine, _body())
    assert engine.verify(rec, expected_commit="abc123").verdict == VERIFIED
    assert engine.verify(rec, expected_commit="other").verdict == UNVERIFIED


def test_broken_chain_is_failed():
    engine, _ = _engine()
    r1 = _record(engine, _body())
    r2 = _record(engine, _body(), prev=r1["entry_hash"])
    # Claim a previous hash that is not the real one.
    assert engine.verify(r2, prev_expected_hash="wrong").verdict == FAILED
    assert engine.verify(r2, prev_expected_hash=r1["entry_hash"]).verdict == VERIFIED


def test_index_integrity_detects_a_break():
    engine, _ = _engine()
    r1 = _record(engine, _body())
    r2 = _record(engine, _body(), prev=r1["entry_hash"])
    r3 = _record(engine, _body(), prev="not-r2")
    report = engine.index_integrity([r1, r2, r3])
    assert report["intact"] is False
    assert report["broken_at"]


def test_no_signer_raises_rather_than_storing_unsigned():
    engine = EvidenceTrustEngine(Keyring(), None)
    with pytest.raises(EvidenceError):
        engine.sign_body(_body())


# ----------------------------------------------------------- stored records
pytest_db = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run evidence storage tests"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


@pytest_db
class TestEvidenceService:
    @pytest.fixture
    def store(self):
        _reset_schema()
        s = PostgresStore(DSN)
        yield s
        s.close()

    def _service(self, store):
        from orchestration.nexus_evidence import EvidenceService

        signer = generate_signer("k1")
        ring = Keyring()
        ring.add("k1", public_key_b64(signer))
        return EvidenceService(store, EvidenceTrustEngine(ring, signer))

    def _project(self, store, name: str = "P") -> str:
        with store._tx() as cur:
            cur.execute("INSERT INTO nexus_projects(name) VALUES (%s) RETURNING id", (name,))
            return str(cur.fetchone()["id"])

    def test_project_scoped_record_is_verified_and_chained(self, store):
        svc = self._service(store)
        pid = self._project(store)
        r1 = svc.record(subject_kind="project", subject_id=pid, project_id=pid,
                        summary="one", created_by="user:o")
        r2 = svc.record(subject_kind="project", subject_id=pid, project_id=pid,
                        summary="two", created_by="user:o")
        assert r1["verdict"] == VERIFIED
        assert r2["prev_hash"] == r1["entry_hash"]
        assert svc.index_integrity(project_id=pid)["intact"] is True

    def test_legacy_unscoped_record_stays_unscoped(self, store):
        svc = self._service(store)
        rec = svc.record(subject_kind="manual", subject_id=None, project_id=None,
                         summary="legacy", created_by="user:o")
        assert rec["project_id"] is None
        assert rec["format_version"] == 1

    def test_cross_project_read_is_not_found(self, store):
        from orchestration.cloud_store import NotFound

        svc = self._service(store)
        pid = self._project(store, "A")
        other = self._project(store, "B")
        rec = svc.record(subject_kind="project", subject_id=pid, project_id=pid,
                         summary="x", created_by="user:o")
        with pytest.raises(NotFound):
            svc.get(rec["id"], project_id=other)

    def test_project_list_does_not_leak_other_projects(self, store):
        svc = self._service(store)
        pid = self._project(store, "A")
        other = self._project(store, "B")
        svc.record(subject_kind="project", subject_id=pid, project_id=pid,
                   summary="mine", created_by="user:o")
        svc.record(subject_kind="project", subject_id=other, project_id=other,
                   summary="theirs", created_by="user:o")
        mine = svc.list(project_id=pid)
        assert len(mine) == 1
        assert mine[0]["body"]["summary"] == "mine"

    def test_reverify_uses_the_real_verifier(self, store):
        svc = self._service(store)
        pid = self._project(store)
        rec = svc.record(subject_kind="project", subject_id=pid, project_id=pid,
                         commit_sha="abc", summary="x", created_by="user:o")
        out = svc.reverify(rec["id"], project_id=pid, expected_commit="abc")
        assert out["verdict"] == VERIFIED
        out2 = svc.reverify(rec["id"], project_id=pid, expected_commit="different")
        assert out2["verdict"] == UNVERIFIED

    def test_column_tamper_is_detected_on_reverify(self, store):
        svc = self._service(store)
        pid = self._project(store)
        other = self._project(store, "B")
        rec = svc.record(subject_kind="project", subject_id=pid, project_id=pid,
                         summary="x", created_by="user:o")
        # Move the row to another project directly in the database. The signed
        # body still names the original project, so verification must fail.
        with store._tx() as cur:
            cur.execute("UPDATE nexus_evidence SET project_id=%s WHERE id=%s",
                        (other, rec["id"]))
        out = svc.reverify(rec["id"], project_id=other)
        assert out["verdict"] == FAILED
