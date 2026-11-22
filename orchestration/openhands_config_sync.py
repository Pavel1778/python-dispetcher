"""Reconcile NEXUS's stored OpenHands configuration with what OpenHands reports.

The mission's Phase One asks for an idempotent reconciliation service: load the
persisted configuration, compare it with the *effective* configuration the
OpenHands account reports, record drift, and never rewrite what already matches.

This module is deliberately narrow and honest:

* It reads the account's own ``GET /api/v1/settings`` (the same call
  ``account_capabilities`` uses) and extracts the *effective* ``llm.model`` and
  ``llm.base_url``. It compares them with NEXUS's stored ``default_model`` and
  ``llm_base_url``.
* The result is one of ``synced`` (they agree), ``drift`` (they disagree),
  ``pending`` (could not be compared — OpenHands unreachable), or ``blocked``
  (the account has no usable key / is revoked).
* It records the outcome on the account row (``config_synced_at``,
  ``config_sync_state``, ``config_sync_error``) so an operator can see it and a
  later pass can retry. It never raises on a comparison failure: a side record
  must not fail the work it observes.

**What it does NOT do, on purpose:** it does not write settings back to
OpenHands. The Cloud V1 ``/api/v1/settings`` surface is read-only for the fields
this mission touches (there is no documented PATCH for ``agent_settings.llm``),
so NEXUS applies the configuration at *session creation* (``agent_settings`` on
the create call — see ``cloud_direct._sandbox_agent_settings``), which is the
supported mechanism. The reconciler therefore *reports* drift; it does not
pretend to fix a server-side default it cannot write.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .cloud_client import CloudAuthError, CloudError, CloudUnavailable
from .cloud_store import PostgresStore, StoreError
from .nexus_openhands_accounts import OpenHandsAccountService

#: The four states an account's configuration can be in.
SYNCED = "synced"
DRIFT = "drift"
PENDING = "pending"
BLOCKED = "blocked"
STATES = (SYNCED, DRIFT, PENDING, BLOCKED)


@dataclass(frozen=True)
class ReconcileResult:
    account_id: str
    state: str
    detail: str
    desired: dict[str, Any]
    effective: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"account_id": self.account_id, "state": self.state,
                "detail": self.detail, "desired": self.desired,
                "effective": self.effective}


def _norm(value: Any) -> str | None:
    """Compare model/URL values the way a person would: trimmed, no trailing
    slash, case-insensitive scheme/host but case-sensitive path is overkill —
    a model id and a base URL are compared as trimmed strings with the URL's
    trailing slash removed."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("http://") or text.startswith("https://"):
        return text.rstrip("/")
    return text


class ConfigReconciler:
    """Compare desired configuration with the account's effective configuration."""

    def __init__(self, store: PostgresStore, accounts: OpenHandsAccountService | None = None):
        self.store = store
        self.accounts = accounts or OpenHandsAccountService(store)

    # ------------------------------------------------------------- effective
    def effective(self, principal: str, account_id: str) -> dict[str, Any]:
        """The model and provider endpoint OpenHands reports, or raise.

        Raises ``CloudAuthError``/``CloudUnavailable`` so the caller can map it
        to ``blocked``/``pending`` rather than guessing.
        """
        client = self.accounts.client_for_account(principal, account_id)
        settings = client.get_settings()
        agent_settings = settings.get("agent_settings") or {}
        llm = agent_settings.get("llm") or {}
        return {"model": llm.get("model"), "base_url": llm.get("base_url")}

    # -------------------------------------------------------------- reconcile
    def reconcile(self, principal: str, account_id: str) -> ReconcileResult:
        """Compare, record the outcome, and return it. Never raises."""
        desired = self._desired(principal, account_id)
        try:
            effective = self.effective(principal, account_id)
        except CloudAuthError as e:
            return self._record(account_id, BLOCKED, f"key rejected: {e}",
                                desired, {})
        except (CloudUnavailable, CloudError) as e:
            return self._record(account_id, PENDING, f"unreachable: {e}",
                                desired, {})
        except (StoreError, ValueError, KeyError) as e:
            # No usable account (revoked), or the store hiccupped. Not a drift
            # verdict — nothing was compared.
            return self._record(account_id, BLOCKED, f"cannot read config: {e}",
                                desired, {})

        drift = self._drift(desired, effective)
        if drift:
            return self._record(account_id, DRIFT, "; ".join(drift), desired, effective)
        return self._record(account_id, SYNCED, "desired configuration is in effect",
                            desired, effective)

    # ---------------------------------------------------------------- helpers
    def _desired(self, principal: str, account_id: str) -> dict[str, Any]:
        try:
            cfg = self.accounts.account_config(principal, account_id)
        except (StoreError, ValueError, KeyError):
            cfg = {}
        return {"model": cfg.get("default_model"),
                "base_url": cfg.get("llm_base_url")}

    @staticmethod
    def _drift(desired: dict[str, Any], effective: dict[str, Any]) -> list[str]:
        """Which desired fields the effective configuration does not match.

        Only a field NEXUS *set* can drift: an unset ``llm_base_url`` (None)
        means "the provider default", so it never counts as drift whatever the
        server reports there. A set model must match exactly.
        """
        out: list[str] = []
        want_model = _norm(desired.get("model"))
        if want_model is not None and want_model != _norm(effective.get("model")):
            out.append(f"model: want {want_model!r}, have {effective.get('model')!r}")
        want_url = _norm(desired.get("base_url"))
        if want_url is not None and want_url != _norm(effective.get("base_url")):
            out.append(f"base_url: want {want_url!r}, have {effective.get('base_url')!r}")
        return out

    def _record(self, account_id: str, state: str, detail: str,
                desired: dict[str, Any], effective: dict[str, Any]) -> ReconcileResult:
        try:
            with self.store._tx() as cur:
                cur.execute(
                    "UPDATE nexus_openhands_accounts SET config_synced_at=now(), "
                    "config_sync_state=%s, config_sync_error=%s WHERE id=%s",
                    (state, None if state == SYNCED else detail[:500], account_id))
        except StoreError:
            # The record is a side effect; a failed write must not lose the
            # result the caller is about to see.
            pass
        return ReconcileResult(account_id, state, detail, desired, effective)

    # ------------------------------------------------------------ bulk sweep
    def reconcile_all(self, principal: str | None = None) -> list[ReconcileResult]:
        """Reconcile every active account (of *principal* when given).

        Bounded and read-only against OpenHands: one ``get_settings`` per
        account. A disabled or revoked account is skipped, not failed.
        """
        with self.store._tx() as cur:
            if principal is None:
                cur.execute("SELECT id, principal FROM nexus_openhands_accounts "
                            "WHERE state='active' AND enabled ORDER BY created_at")
            else:
                cur.execute("SELECT id, principal FROM nexus_openhands_accounts "
                            "WHERE state='active' AND enabled AND principal=%s "
                            "ORDER BY created_at", (principal,))
            rows = cur.fetchall()
        results: list[ReconcileResult] = []
        for row in rows:
            results.append(self.reconcile(row["principal"], str(row["id"])))
        return results
