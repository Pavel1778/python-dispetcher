"""Documentation integrity: a doc the README promises must exist.

The README's "Start here" list is how an operator finds the self-hosting and
backup guides. A link to a file that does not exist sends them to a dead end, so
this case fails when a promised document is missing. It also checks the five
deliverables the self-hosting increment added, by name, so deleting one is
caught here rather than by a reader.
"""
from __future__ import annotations

import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_REQUIRED = (
    "docs/PRODUCT_STATUS.md",
    "docs/SELF_HOSTING.md",
    "docs/CLOUDFLARE_TUNNEL.md",
    "docs/BACKUP_AND_RESTORE.md",
    "docs/PRODUCT_ROADMAP.md",
    "docs/OPERATIONS.md",
    "docs/ENVIRONMENT.md",
    "scripts/nexus.sh",
)


def test_every_promised_document_exists():
    missing = [p for p in _REQUIRED if not os.path.exists(os.path.join(_ROOT, p))]
    assert not missing, f"promised but missing: {missing}"


def test_no_readme_doc_link_is_broken():
    src = open(os.path.join(_ROOT, "README.md")).read()
    links = re.findall(
        r"\]\((docs/[^)#]+\.md|AGENTS\.md|CHANGELOG\.md|DEPLOY\.md)\)", src)
    broken = [l for l in sorted(set(links))
              if not os.path.exists(os.path.join(_ROOT, l))]
    assert not broken, f"broken README links: {broken}"


def test_the_readme_points_at_the_self_hosting_guide():
    src = open(os.path.join(_ROOT, "README.md")).read()
    assert "docs/SELF_HOSTING.md" in src
    assert "scripts/nexus.sh" in src
