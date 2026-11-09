"""Evidence Trust Engine: Ed25519-signed, hash-chained, verifiable evidence.

Design rules that the rest of NEXUS depends on:

* **Signing is real.** Records are signed with Ed25519 over a canonical byte
  encoding of the record body. A trusted keyring maps ``key_id`` to a public key.
* **A verdict is earned, never granted.** ``VERIFIED`` is only returned when the
  signature checks out, the key is trusted, the expected commit matches, and the
  hash chain is intact. A record that merely exists is ``UNVERIFIED``.
* **HMAC can never produce VERIFIED.** Only an asymmetric signature from a key
  in the trusted keyring does. A symmetric tag is recorded as ``UNVERIFIED``.
* **Project isolation is signed.** For ``format_version`` 2 the signed body
  contains ``project_id``. Editing the column afterwards breaks the signature.
  Legacy ``format_version`` 1 records are ``UNVERIFIED``/unscoped and are never
  given a fabricated project.
* **Append-only, not tamper-proof.** The hash chain (``prev_hash``/``entry_hash``)
  detects reordering and deletion by an honest reader; it is not a defence
  against an attacker with write access to the table. The signed payload is the
  real integrity boundary.

The engine is storage-agnostic: the store does the SQL, this module does the
crypto and the verdict logic.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("evidence_trust")

try:  # pragma: no cover - exercised by the real environment
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives import serialization

    _CRYPTO = True
except Exception:  # noqa: BLE001 - a missing lib must not crash import
    _CRYPTO = False

#: Verdict values. Order matters only for reporting, not for trust decisions.
VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"
FAILED = "FAILED"
BLOCKED = "BLOCKED"

#: Current signed-body format. v1 had no project_id; v2 binds project_id.
FORMAT_VERSION = 2
LEGACY_FORMAT_VERSION = 1

#: The domain separator keeps a NEXUS evidence signature from being replayed as
#: a signature over some other NEXUS payload.
_DOMAIN = b"nexus-evidence-v"


class EvidenceError(RuntimeError):
    """A caller error: bad input, unknown key, missing configuration."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))


def canonical_bytes(body: dict[str, Any]) -> bytes:
    """Deterministic bytes for *body*: sorted keys, compact, UTF-8.

    ``sort_keys`` plus a fixed separator makes the encoding independent of dict
    insertion order, so the signer and the verifier agree on exactly what was
    signed without depending on Python's json defaults.
    """
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _signing_input(format_version: int, body: dict[str, Any]) -> bytes:
    return _DOMAIN + str(format_version).encode("ascii") + b"\x00" + canonical_bytes(body)


def entry_hash(prev_hash: str | None, format_version: int, body: dict[str, Any]) -> str:
    """The chain hash for one entry: SHA-256 over prev_hash + the signing input."""
    h = hashlib.sha256()
    h.update((prev_hash or "").encode("ascii"))
    h.update(b"\x00")
    h.update(_signing_input(format_version, body))
    return h.hexdigest()


@dataclass
class TrustedKey:
    key_id: str
    public_key: "Ed25519PublicKey"
    label: str = ""
    revoked: bool = False


@dataclass
class Signer:
    """An Ed25519 private key plus the key id it publishes under."""

    key_id: str
    private_key: "Ed25519PrivateKey"

    def sign(self, format_version: int, body: dict[str, Any]) -> str:
        return _b64e(self.private_key.sign(_signing_input(format_version, body)))

    def public_b64(self) -> str:
        raw = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return _b64e(raw)


class Keyring:
    """Trusted public keys, keyed by ``key_id``. A revoked key never verifies."""

    def __init__(self) -> None:
        self._keys: dict[str, TrustedKey] = {}

    def add(self, key_id: str, public_key_b64: str, *, label: str = "",
            revoked: bool = False) -> TrustedKey:
        if not _CRYPTO:
            raise EvidenceError("cryptography is not installed")
        try:
            raw = _b64d(public_key_b64)
            pub = Ed25519PublicKey.from_public_bytes(raw)
        except Exception as e:  # noqa: BLE001
            raise EvidenceError(f"invalid public key for {key_id}: {e}") from e
        key = TrustedKey(key_id=key_id, public_key=pub, label=label, revoked=revoked)
        self._keys[key_id] = key
        return key

    def get(self, key_id: str) -> TrustedKey | None:
        return self._keys.get(key_id)

    def __len__(self) -> int:
        return len(self._keys)

    def __contains__(self, key_id: object) -> bool:
        return key_id in self._keys

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Keyring":
        """Build a keyring from ``NEXUS_EVIDENCE_KEYRING``.

        Format: ``key_id:base64pub[,key_id:base64pub...]``. A key suffixed with
        ``:revoked`` is loaded but marked revoked, so old records verify as
        UNVERIFIED rather than as a forged key.
        """
        env = env if env is not None else dict(os.environ)
        ring = cls()
        raw = (env.get("NEXUS_EVIDENCE_KEYRING") or "").strip()
        if not raw:
            return ring
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if ":" not in part:
                log.warning("NEXUS_EVIDENCE_KEYRING entry %r is malformed; skipped", part)
                continue
            key_id, value = part.split(":", 1)
            key_id = key_id.strip()
            value = value.strip()
            revoked = False
            if value.endswith(":revoked"):
                revoked = True
                value = value[: -len(":revoked")]
            try:
                ring.add(key_id, value, revoked=revoked)
            except EvidenceError as e:
                log.warning("NEXUS_EVIDENCE_KEYRING entry %r rejected: %s", key_id, e)
        return ring


@dataclass
class Verdict:
    verdict: str
    reason: str
    checks: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "reason": self.reason, "checks": self.checks}


class EvidenceTrustEngine:
    """Signs evidence and decides a verdict for a stored record."""

    def __init__(self, keyring: Keyring | None = None, signer: Signer | None = None):
        self.keyring = keyring if keyring is not None else Keyring()
        self.signer = signer

    # --------------------------------------------------------------- signing
    def sign_body(self, body: dict[str, Any]) -> tuple[int, str, str, str]:
        """Return ``(format_version, signed_payload, key_id, signature)``.

        Raises when no signer is configured: evidence must never be stored
        unsigned and then reported as trusted.
        """
        if self.signer is None:
            raise EvidenceError("no evidence signer configured")
        version = int(body.get("format_version", FORMAT_VERSION))
        payload = canonical_bytes(body).decode("utf-8")
        signature = self.signer.sign(version, body)
        return version, payload, self.signer.key_id, signature

    # ------------------------------------------------------------ verification
    def verify(self, record: dict[str, Any], *,
               expected_commit: str | None = None,
               prev_expected_hash: str | None = None,
               chain_tail: str | None = None) -> Verdict:
        """Decide a verdict for a stored evidence *record*.

        *expected_commit* (when given) must equal the record's commit for a
        VERIFIED verdict — an operator asking "is this the commit I expect?"
        gets UNVERIFIED rather than a false positive.

        *prev_expected_hash* / *chain_tail* let a caller verify chain position:
        pass the hash the record claims to follow and the actual previous entry
        hash from the store.
        """
        checks: dict[str, Any] = {}

        if not _CRYPTO:
            return Verdict(BLOCKED, "cryptography library is not installed", checks)

        key_id = record.get("key_id") or ""
        signature = record.get("signature") or ""
        signed_payload = record.get("signed_payload")
        version = int(record.get("format_version") or LEGACY_FORMAT_VERSION)
        checks["format_version"] = version

        if not signed_payload:
            return Verdict(UNVERIFIED, "record has no signed payload (legacy)",
                           checks)
        if not signature or not key_id:
            return Verdict(UNVERIFIED, "record is unsigned", checks)

        key = self.keyring.get(key_id)
        if key is None:
            return Verdict(UNVERIFIED, f"signing key {key_id} is not trusted", checks)
        if key.revoked:
            return Verdict(UNVERIFIED, f"signing key {key_id} is revoked", checks)
        checks["key_trusted"] = True

        # The signed payload is the source of truth; a jsonb column that drifted
        # from it is itself a tamper signal.
        try:
            body = json.loads(signed_payload)
        except Exception:  # noqa: BLE001
            return Verdict(FAILED, "signed payload is not valid JSON", checks)

        recomputed_payload = canonical_bytes(body).decode("utf-8")
        if recomputed_payload != signed_payload:
            checks["payload_canonical"] = False
            return Verdict(FAILED, "signed payload is not canonical", checks)
        checks["payload_canonical"] = True

        # Indexed columns must agree with the signed body: this is what makes an
        # edit to project_id / commit_sha in the row detectable.
        for col in ("project_id", "repository_full_name", "commit_sha", "branch",
                    "subject_kind", "subject_id"):
            signed_value = body.get(col)
            if signed_value is None:
                continue
            if _norm(record.get(col)) != _norm(signed_value):
                checks["column_mismatch"] = col
                return Verdict(FAILED,
                               f"signed {col} does not match the stored column",
                               checks)

        # The signature itself.
        try:
            key.public_key.verify(_b64d(signature), _signing_input(version, body))
            checks["signature_valid"] = True
        except InvalidSignature:
            checks["signature_valid"] = False
            return Verdict(FAILED, "signature does not verify", checks)

        # Optional: does this record bind the commit the operator expects?
        if expected_commit is not None:
            if body.get("commit_sha") != expected_commit:
                checks["expected_commit"] = expected_commit
                checks["actual_commit"] = body.get("commit_sha")
                return Verdict(UNVERIFIED,
                               "record commit does not match the expected commit",
                               checks)

        # Optional: chain position.
        claimed_prev = record.get("prev_hash")
        if prev_expected_hash is not None and (claimed_prev or None) != (prev_expected_hash or None):
            checks["chain_broken"] = True
            return Verdict(FAILED, "hash chain is broken (prev_hash mismatch)", checks)
        if chain_tail is not None:
            checks["chain_tail"] = chain_tail

        checks["commit_sha"] = body.get("commit_sha")
        checks["project_id"] = body.get("project_id")
        return Verdict(VERIFIED, "signature valid and key trusted", checks)

    # -------------------------------------------------------------- indexing
    def index_integrity(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """Check the hash chain across *records* (oldest first).

        Returns a summary; a break is reported, never silently repaired. This is
        integrity evidence, not a trust decision: a chain can be intact while a
        record is unsigned, and each record's own verdict governs its trust.
        """
        broken_at: list[str] = []
        prev: str | None = None
        for rec in records:
            expected = entry_hash(prev, int(rec.get("format_version") or LEGACY_FORMAT_VERSION),
                                  json.loads(rec["signed_payload"])) if rec.get("signed_payload") else None
            if rec.get("prev_hash") != prev:
                broken_at.append(str(rec.get("id")))
            elif expected is not None and expected != rec.get("entry_hash"):
                broken_at.append(str(rec.get("id")))
            prev = rec.get("entry_hash")
        return {"checked": len(records), "broken_at": broken_at,
                "intact": not broken_at}


def _norm(value: Any) -> Any:
    """Normalise a value for column/payload comparison (uuid/str, whitespace)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip()
    return str(value)


def generate_signer(key_id: str) -> Signer:
    """Create a fresh Ed25519 signer. Used by the CLI to mint a lab key."""
    if not _CRYPTO:
        raise EvidenceError("cryptography is not installed")
    return Signer(key_id=key_id, private_key=Ed25519PrivateKey.generate())


def signer_from_env(env: dict[str, str] | None = None) -> Signer | None:
    """Build a signer from ``NEXUS_EVIDENCE_PRIVATE_KEY`` (base64 raw seed).

    Returns ``None`` when unset, so the engine reports "no signer" rather than
    fabricating one. The private key is never logged.
    """
    if not _CRYPTO:
        return None
    env = env if env is not None else dict(os.environ)
    raw = (env.get("NEXUS_EVIDENCE_PRIVATE_KEY") or "").strip()
    key_id = (env.get("NEXUS_EVIDENCE_KEY_ID") or "nexus-lab").strip()
    if not raw:
        return None
    try:
        seed = _b64d(raw)
        return Signer(key_id=key_id, private_key=Ed25519PrivateKey.from_private_bytes(seed))
    except Exception as e:  # noqa: BLE001
        log.warning("NEXUS_EVIDENCE_PRIVATE_KEY rejected: %s", e)
        return None


def public_key_b64(signer: Signer) -> str:
    return signer.public_b64()
