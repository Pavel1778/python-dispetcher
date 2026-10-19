"""Dashboard auth primitives: scrypt hashing and HMAC-signed sessions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.auth import (
    AuthConfigError,
    SessionSigner,
    hash_password,
    verify_password,
)


def test_hash_verify_roundtrip():
    stored = hash_password("correct horse battery staple")
    assert stored.startswith("scrypt$")
    assert "correct horse battery staple" not in stored
    assert verify_password("correct horse battery staple", stored)


def test_hash_is_salted_and_nondeterministic():
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b
    assert verify_password("same-password", a)
    assert verify_password("same-password", b)


def test_verify_rejects_wrong_password():
    stored = hash_password("right")
    assert not verify_password("wrong", stored)
    assert not verify_password("", stored)


@pytest.mark.parametrize(
    "stored",
    ["", "not-a-hash", "scrypt$bad", "bcrypt$1$2$3$4$5", "scrypt$16384$8$1$!!!$???"],
)
def test_verify_rejects_malformed_hash_without_raising(stored):
    assert not verify_password("anything", stored)


def test_hash_rejects_empty_password():
    with pytest.raises(AuthConfigError):
        hash_password("")


def test_session_mint_verify_roundtrip():
    signer = SessionSigner("s3cret", ttl_seconds=3600)
    token = signer.mint("operator")
    session = signer.verify(token)
    assert session is not None
    assert session.username == "operator"
    assert session.expires_at > datetime.now(timezone.utc)


def test_session_rejects_tampered_payload():
    signer = SessionSigner("s3cret", ttl_seconds=3600)
    token = signer.mint("operator")
    payload, sig = token.split(".", 1)
    # Flip the payload but keep the original signature.
    tampered = payload[:-2] + ("AA" if not payload.endswith("AA") else "BB") + "." + sig
    assert signer.verify(tampered) is None


def test_session_rejects_foreign_signature():
    a = SessionSigner("secret-a", ttl_seconds=3600)
    b = SessionSigner("secret-b", ttl_seconds=3600)
    assert b.verify(a.mint("operator")) is None


def test_session_rejects_expired_token():
    signer = SessionSigner("s3cret", ttl_seconds=60)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    token = signer.mint("operator", now=past)
    assert signer.verify(token) is None


def test_session_rejects_garbage():
    signer = SessionSigner("s3cret", ttl_seconds=3600)
    for bad in [None, "", "no-dot", "a.b.c", ".", "..", "!!!.???"]:
        assert signer.verify(bad) is None


def test_signer_requires_secret_and_positive_ttl():
    with pytest.raises(AuthConfigError):
        SessionSigner("", ttl_seconds=60)
    with pytest.raises(AuthConfigError):
        SessionSigner("secret", ttl_seconds=0)


def test_csrf_token_is_bound_to_session_token():
    signer = SessionSigner("s3cret", ttl_seconds=3600)
    t1 = signer.mint("a")
    t2 = signer.mint("b")
    assert signer.csrf_token(t1) != signer.csrf_token(t2)
    assert signer.csrf_token(t1) == signer.csrf_token(t1)
    assert signer.csrf_token(None) == ""
