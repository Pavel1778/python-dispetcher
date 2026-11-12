"""Evidence service: builds, signs and verifies evidence records.

This sits between the HTTP/CLI/MCP surfaces and the two lower layers:
:mod:`orchestration.evidence_trust` (crypto + verdicts) and
:mod:`orchestration.cloud_store` (SQL). It owns the record *shape* and the
project-scoping rules:

* A project-scoped record signs ``project_id`` (format_version 2). Editing the
  column afterwards breaks the signature.
* A legacy record (no project) is stored as format_version 1 and reports as
  ``UNVERIFIED``/unscoped; it is never given a fabricated project.
* Reading is scoped: a project read never returns another project's rows, and
  only returns unscoped rows when explicitly asked.
"""

from __future__ import annotations

import time
from typing import Any

from .cloud_store import NotFound, PostgresStore, StoreError
from .evidence_trust import (
    FORMAT_VERSION,
    LEGACY_FORMAT_VERSION,
    EvidenceTrustEngine,
    entry_hash,
)


class EvidenceScopeError(StoreError):
    """A read or re-verify that would cross a project boundary."""


class EvidenceService:
    def __init__(self, store: PostgresStore, engine: EvidenceTrustEngine):
        self.store = store
        self.engine = engine

    # ----------------------------------------------------------------- create
    def record(self, *, subject_kind: str, subject_id: str | None,
               project_id: str | None, repository_full_name: str | None = None,
               commit_sha: str | None = None, branch: str | None = None,
               summary: str = "", detail: dict[str, Any] | None = None,
               created_by: str = "system") -> dict:
        """Sign and append one evidence record.

        A project-scoped record must have a project that exists. The record is
        signed over a canonical body that includes every field a reader trusts.
        """
        if subject_kind not in ("task", "workflow", "project", "manual"):
            raise ValueError("invalid subject_kind")

        repository_id = None
        if repository_full_name:
            with self.store._tx() as cur:
                cur.execute("SELECT id FROM repositories WHERE full_name=%s",
                            (repository_full_name,))
                row = cur.fetchone()
                repository_id = str(row["id"]) if row else None

        if project_id is not None:
            with self.store._tx() as cur:
                cur.execute("SELECT 1 FROM nexus_projects WHERE id=%s", (project_id,))
                if cur.fetchone() is None:
                    raise NotFound("project not found")

        version = FORMAT_VERSION if project_id is not None else LEGACY_FORMAT_VERSION
        body: dict[str, Any] = {
            "format_version": version,
            "subject_kind": subject_kind,
            "subject_id": str(subject_id) if subject_id else None,
            "repository_full_name": repository_full_name,
            "commit_sha": commit_sha,
            "branch": branch,
            "summary": summary,
            "detail": detail or {},
            "created_by": created_by,
            "created_at": int(time.time()),
        }
        if project_id is not None:
            body["project_id"] = str(project_id)

        fmt, payload, key_id, signature = self.engine.sign_body(body)
        prev_hash = self.store.evidence_chain_tail(project_id)
        ehash = entry_hash(prev_hash, fmt, body)

        # Verify what we are about to store, with the same engine a reader uses,
        # so a misconfigured keyring fails here rather than at read time.
        provisional = {
            "project_id": project_id, "format_version": fmt,
            "repository_full_name": repository_full_name, "commit_sha": commit_sha,
            "branch": branch, "subject_kind": subject_kind, "subject_id": subject_id,
            "signed_payload": payload, "key_id": key_id, "signature": signature,
            "prev_hash": prev_hash, "entry_hash": ehash,
        }
        verdict = self.engine.verify(provisional, prev_expected_hash=prev_hash)

        record = {
            "project_id": project_id,
            "format_version": fmt,
            "repository_id": repository_id,
            "repository_full_name": repository_full_name,
            "commit_sha": commit_sha,
            "branch": branch,
            "subject_kind": subject_kind,
            "subject_id": str(subject_id) if subject_id else None,
            "body": body,
            "signed_payload": payload,
            "key_id": key_id,
            "signature": signature,
            "verdict": verdict.verdict,
            "verification_detail": verdict.as_dict(),
            "verified_at": None,
            "prev_hash": prev_hash,
            "entry_hash": ehash,
            "created_by": created_by,
        }
        stored = self.store.insert_evidence(record)
        if project_id is not None:
            self._event(project_id, created_by, "evidence.recorded", stored["id"])
        return stored

    # ------------------------------------------------------- lab task results
    def record_task_result(self, task: dict, verdict: Any, report: Any) -> dict:
        """Sign one evidence record for a settled autonomous-lab task.

        This is the EVIDENCE stage of the lab pipeline (``nexus_lab``): a task
        that reached a terminal verdict is recorded through the Trust Engine, so
        its result is a signed, independently re-verifiable record rather than
        only a column on the task row. The commit the verifier observed
        (``remote_sha``) is what the record binds, so re-verification with
        ``expected_commit`` is meaningful.

        Called by the scheduler as a side effect; it must never change the
        task's own state, which is already written when it runs.
        """
        evidence = dict(getattr(verdict, "evidence", {}) or {})
        commit = evidence.get("remote_sha") or None
        detail = {
            "verdict": getattr(verdict, "verdict", None),
            "reason": getattr(verdict, "reason", ""),
            "task_key": task.get("task_key"),
            "run_id": str(task.get("run_id") or ""),
            "conversation_id": task.get("conversation_id"),
            "final_text": (getattr(report, "final_text", "") or "")[:2000],
            **{k: evidence[k] for k in ("branch", "named_sha", "state") if k in evidence},
        }
        return self.record(
            subject_kind="task",
            subject_id=str(task["id"]),
            project_id=str(task["project_id"]) if task.get("project_id") else None,
            repository_full_name=task.get("repository") or None,
            commit_sha=commit,
            branch=task.get("branch") or None,
            summary=getattr(verdict, "reason", "") or "task result",
            detail=detail,
            created_by="lab:verifier",
        )

    # ------------------------------------------------------------------- read
    def get(self, evidence_id: str, *, project_id: str | None,
            allow_unscoped: bool = False) -> dict:
        """Fetch one record, enforcing scope.

        A project-scoped caller may only read its own project's records (or an
        unscoped legacy record when *allow_unscoped* is set). A cross-project id
        is a 404, never a disclosure that it exists.
        """
        rec = self.store.get_evidence(evidence_id)
        if project_id is not None:
            rec_project = rec.get("project_id")
            if rec_project != project_id and not (rec_project is None and allow_unscoped):
                raise NotFound("evidence not found")
        return rec

    def list(self, *, project_id: str | None, include_unscoped: bool = False,
             repository_full_name: str | None = None, subject_id: str | None = None,
             limit: int = 100) -> list[dict]:
        return self.store.list_evidence(
            project_id=project_id, include_unscoped=include_unscoped,
            repository_full_name=repository_full_name, subject_id=subject_id,
            limit=limit,
        )

    # ------------------------------------------------------------- re-verify
    def reverify(self, evidence_id: str, *, project_id: str | None,
                 expected_commit: str | None = None,
                 allow_unscoped: bool = False) -> dict:
        """Re-run the real verifier over a stored record and persist the verdict.

        Uses the trusted keyring, checks the signature, the commit (when the
        caller names one) and the hash chain position. The verdict is written
        back; the signed payload is never modified.
        """
        rec = self.get(evidence_id, project_id=project_id, allow_unscoped=allow_unscoped)
        prev_expected = None
        if rec.get("prev_hash"):
            # The record before this one in the same scope must hash to prev_hash.
            with self.store._tx() as cur:
                cur.execute(
                    "SELECT entry_hash FROM nexus_evidence WHERE seq < "
                    "(SELECT seq FROM nexus_evidence WHERE id=%s) "
                    "AND project_id IS NOT DISTINCT FROM %s ORDER BY seq DESC LIMIT 1",
                    (evidence_id, rec.get("project_id")),
                )
                row = cur.fetchone()
            prev_expected = row["entry_hash"] if row else ""
        verdict = self.engine.verify(rec, expected_commit=expected_commit,
                                     prev_expected_hash=prev_expected)
        self.store.update_evidence_verification(
            evidence_id, verdict=verdict.verdict, detail=verdict.as_dict())
        out = dict(rec)
        out["verdict"] = verdict.verdict
        out["verification_detail"] = verdict.as_dict()
        return out

    def index_integrity(self, *, project_id: str | None) -> dict[str, Any]:
        """Check the hash chain for a scope (oldest first)."""
        records = self.store.list_evidence(project_id=project_id, limit=1000)
        records = list(reversed(records))
        return self.engine.index_integrity(records)

    # --------------------------------------------------------------- helper
    def _event(self, project_id: str, principal: str, kind: str, resource_id: str) -> None:
        with self.store._tx() as cur:
            cur.execute(
                "INSERT INTO nexus_project_events(project_id, actor, kind, resource_id) "
                "VALUES (%s,%s,%s,%s)",
                (project_id, principal, kind, resource_id),
            )
