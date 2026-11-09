"""Repository analysis: turn a GitHub repository into a context summary.

This module is the only place NEXUS reads a repository to understand it. It
uses the read-only :class:`~orchestration.github_client.GitHubClient`, never a
model, so the summary is observed fact rather than a guess. Every summary
records the revision it was built from, so a refresh is skipped when nothing
changed.

Nothing here writes to the repository. Git *writes* (clone/branch/commit/push)
belong to the executor path, not the analyzer.
"""
from __future__ import annotations

from typing import Any

from .github_client import GitHubClient, GitHubError
from .nexus_context import analyze_tree

#: Files we read to understand a project. Kept small on purpose: each read is a
#: request, and a summary does not need the whole repository.
_READ_FILES = (
    "README.md", "README.rst", "README.txt", "README",
    "package.json", "pyproject.toml", "requirements.txt", "Makefile",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "Dockerfile",
)


def analyze_repository(client: GitHubClient, full_name: str, *,
                       branch: str | None = None,
                       tree_limit: int = 500) -> dict[str, Any]:
    """Build a context summary for *full_name* at *branch*.

    Raises :class:`GitHubError` if the repository cannot be read; the caller
    decides whether that is transient. The returned dict carries the observed
    ``revision`` so a caller can skip a redundant refresh.
    """
    full_name = (full_name or "").strip()
    if full_name.count("/") != 1:
        raise ValueError("full_name must be owner/name")
    repo = client.get_repository(full_name)
    if not repo:
        raise GitHubError(f"repository {full_name} not found")
    ref = branch or repo.get("default_branch") or "main"
    revision = client.default_branch_sha(full_name, ref) or ""
    tree = client.list_tree(full_name, branch=ref, limit=tree_limit)
    files: dict[str, str] = {}
    for path in _READ_FILES:
        try:
            content = client.read_file(full_name, path, branch=ref)
        except GitHubError:
            continue
        if content:
            files[path] = content
    summary = analyze_tree(tree, files)
    summary["repository"] = full_name
    summary["branch"] = ref
    summary["revision"] = revision
    summary["default_branch"] = repo.get("default_branch")
    return summary
