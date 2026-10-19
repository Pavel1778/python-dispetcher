"""GitHub REST adapter used to discover the repositories an account can reach.

The dispatcher refuses to run a task in a repository that is not in its own
allowlist. Typing ``owner/name`` by hand is both tedious and easy to get wrong,
so this module pulls the list straight from GitHub and feeds the allowlist.

The token is read from ``GITHUB_TOKEN`` and is never logged, returned or
persisted: only non-secret metadata (full name, default branch, flags) leaves
this module. Nothing here writes to GitHub.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_GITHUB_API_URL = "https://api.github.com"


class GitHubError(RuntimeError):
    """Base class for GitHub API failures."""


class GitHubAuthError(GitHubError):
    """401/403, or no token at all. Retrying cannot succeed until fixed."""


class GitHubUnavailable(GitHubError):
    """Transport failure, 5xx or an exhausted rate limit. Transient."""


@dataclass(frozen=True)
class GitHubRepo:
    """The non-secret subset of a repository this dispatcher needs."""

    full_name: str
    default_branch: str
    private: bool = False
    archived: bool = False
    pushed_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "default_branch": self.default_branch,
            "private": self.private,
            "archived": self.archived,
            "pushed_at": self.pushed_at,
        }


class GitHubClient:
    """Minimal, dependency-free GitHub REST client for repository discovery."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        page_size: int = 100,
        max_pages: int = 10,
    ):
        self.base_url = (
            base_url or os.environ.get("GITHUB_API_URL") or DEFAULT_GITHUB_API_URL
        ).rstrip("/")
        self._token = (
            token if token is not None else os.environ.get("GITHUB_TOKEN") or ""
        )
        self.timeout = timeout
        self.page_size = page_size
        self.max_pages = max_pages

    @property
    def configured(self) -> bool:
        """Whether a token is present. The value itself is never exposed."""
        return bool(self._token)

    def __repr__(self) -> str:
        """Redacted: a stray ``repr()`` in a log must not print the token."""
        return (
            f"GitHubClient(base_url={self.base_url!r}, "
            f"configured={self.configured})"
        )

    # ------------------------------------------------------------------ http
    def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self._token:
            raise GitHubAuthError("no GitHub token configured (GITHUB_TOKEN)")
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("User-Agent", "python-dispetcher")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                # A 403 with the rate limit at zero is not an auth problem: the
                # token is fine and the quota resets. Treating it as auth would
                # tell the operator to rotate a working token.
                remaining = e.headers.get("X-RateLimit-Remaining") if e.headers else None
                if e.code == 403 and remaining == "0":
                    raise GitHubUnavailable("github rate limit exhausted") from e
                raise GitHubAuthError(f"github auth failed -> {e.code}") from e
            if e.code >= 500:
                raise GitHubUnavailable(f"github -> {e.code}") from e
            raise GitHubError(f"github -> {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise GitHubUnavailable(f"github: {type(e).__name__}") from e

    # ------------------------------------------------------------ discovery
    def list_repositories(self, *, include_archived: bool = False) -> list[GitHubRepo]:
        """Every repository the token can reach, newest push first.

        Archived repositories are skipped by default: they are read-only on
        GitHub, so a task dispatched into one can never land a change.
        """
        out: list[GitHubRepo] = []
        for page in range(1, self.max_pages + 1):
            batch = self._request(
                "/user/repos",
                {
                    "per_page": self.page_size,
                    "page": page,
                    "sort": "pushed",
                    "direction": "desc",
                    "affiliation": "owner,collaborator,organization_member",
                },
            )
            if not isinstance(batch, list) or not batch:
                break
            for item in batch:
                repo = _repo_from_payload(item)
                if repo is None:
                    continue
                if repo.archived and not include_archived:
                    continue
                out.append(repo)
            # A short page means there is no next one. Comparing against the
            # requested size (not a literal 100) keeps this correct if the page
            # size is ever tuned.
            if len(batch) < self.page_size:
                break
        return out


def _repo_from_payload(item: Any) -> GitHubRepo | None:
    """Map one API object to :class:`GitHubRepo`, or ``None`` if unusable."""
    if not isinstance(item, dict):
        return None
    full_name = item.get("full_name")
    if not isinstance(full_name, str) or "/" not in full_name:
        return None
    return GitHubRepo(
        full_name=full_name,
        default_branch=item.get("default_branch") or "main",
        private=bool(item.get("private")),
        archived=bool(item.get("archived")),
        pushed_at=item.get("pushed_at"),
    )
