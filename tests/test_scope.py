"""Scope-token tests: binding, expiry, tampering."""

from __future__ import annotations

import pytest

from app.scope import ScopeError, mint_scope_token, verify_scope_token

SECRET = "test-scope-secret"


def test_round_trip():
    token = mint_scope_token(SECRET, workflow_id="wf1", task_id="t1", now=1000.0)
    scope = verify_scope_token(
        SECRET, token, workflow_id="wf1", task_id="t1", now=1001.0
    )
    assert scope.workflow_id == "wf1"
    assert scope.task_id == "t1"


def test_wrong_workflow_rejected():
    token = mint_scope_token(SECRET, workflow_id="wf1", task_id="t1", now=1000.0)
    with pytest.raises(ScopeError, match="does not authorise"):
        verify_scope_token(SECRET, token, workflow_id="wf2", task_id="t1", now=1001.0)


def test_wrong_task_rejected():
    token = mint_scope_token(SECRET, workflow_id="wf1", task_id="t1", now=1000.0)
    with pytest.raises(ScopeError, match="does not authorise"):
        verify_scope_token(SECRET, token, workflow_id="wf1", task_id="t2", now=1001.0)


def test_expired_rejected():
    token = mint_scope_token(
        SECRET, workflow_id="wf1", task_id="t1", ttl_seconds=10, now=1000.0
    )
    with pytest.raises(ScopeError, match="expired"):
        verify_scope_token(SECRET, token, workflow_id="wf1", task_id="t1", now=2000.0)


def test_tampered_signature_rejected():
    token = mint_scope_token(SECRET, workflow_id="wf1", task_id="t1", now=1000.0)
    encoded, _sig = token.rsplit(".", 1)
    forged = f"{encoded}.{'A' * 43}"
    with pytest.raises(ScopeError):
        verify_scope_token(SECRET, forged, workflow_id="wf1", task_id="t1", now=1001.0)


def test_token_signed_with_other_secret_rejected():
    token = mint_scope_token("other-secret", workflow_id="wf1", task_id="t1", now=1000.0)
    with pytest.raises(ScopeError, match="signature"):
        verify_scope_token(SECRET, token, workflow_id="wf1", task_id="t1", now=1001.0)


def test_missing_secret_is_error():
    with pytest.raises(ScopeError):
        mint_scope_token("", workflow_id="wf1", task_id="t1")
