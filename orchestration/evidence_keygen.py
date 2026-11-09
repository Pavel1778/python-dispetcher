"""Generate an Ed25519 key pair for the Evidence Trust Engine.

Prints the private key (base64, for ``NEXUS_EVIDENCE_PRIVATE_KEY``) and the
matching keyring entry (for ``NEXUS_EVIDENCE_KEYRING``). Run once per operator
and keep the private key out of version control.

Usage::

    python -m orchestration.evidence_keygen --key-id nexus-lab
"""

from __future__ import annotations

import argparse
import base64
import sys

from .evidence_trust import generate_signer, public_key_b64


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evidence_keygen",
                                     description="Generate an Ed25519 evidence key pair.")
    parser.add_argument("--key-id", default="nexus-lab",
                        help="key id to publish the public key under")
    args = parser.parse_args(argv)
    signer = generate_signer(args.key_id)
    seed = signer.private_key.private_bytes_raw()
    private_b64 = base64.urlsafe_b64encode(seed).decode("ascii")
    print("# Add to the environment (server-side only; never commit):")
    print(f"NEXUS_EVIDENCE_KEY_ID={args.key_id}")
    print(f"NEXUS_EVIDENCE_PRIVATE_KEY={private_b64}")
    print(f"NEXUS_EVIDENCE_KEYRING={args.key_id}:{public_key_b64(signer)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
