"""GitHub REST adapter: discovery, and a small set of *guarded* writes.

Discovery (``list_repositories``) feeds the dispatcher's allowlist so an
operator never has to type ``owner/name`` by hand.

The write methods (``create_repository``, ``set_repository_visibility``) are the
only code in this repository that changes anything on GitHub. They exist for the
"Integrations -> GitHub" tab, and the server calls them **only after** a human
confirmed the exact action through :mod:`orchestration.nexus_actions`; they are
not reachable from a single request or from a model. A visibility change to
public is refused here unless the caller passes ``acknowledge_public=True``, so
even a caller that forgot the confirmation step cannot publish a repository by
accident.

The token is read from ``GITHUB_TOKEN`` and is never logged, returned or
persisted: only non-secret metadata leaves this module. Reads and the guarded
writes live in separate classes: ``GitHubClient`` can read and also exposes the
legacy ``create_repository``/``set_visibility`` used by the confirmation flow,
while ``GitHubWriter`` is the guarded writer for the Integrations tab and
refuses a public change without ``acknowledge_public=True``.
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def quote_path_segment(segment: str) -> str:
    """Percent-encode one path segment as UTF-8, keeping ``/`` structural.

    Repository owners, names, refs and file paths are user-controlled and may
    contain non-ASCII characters (a Cyrillic owner or filename is legal on
    GitHub). A raw non-ASCII byte reaching the request line is encoded with
    ``latin-1`` by ``http.client``, which raises
    ``UnicodeEncodeError: 'latin-1' codec can't encode character``. Encoding
    here -- with ``safe=""`` so a segment cannot introduce a path separator --
    is the correct boundary: the wire path is always ASCII and GitHub decodes
    it back to UTF-8.
    """
    return urllib.parse.quote(segment, safe="")


def repo_path(full_name: str, *extra: str) -> str:
    """``/repos/{owner}/{name}[/{extra...}]`` with every segment encoded.

    ``full_name`` is split on ``/`` and each half is encoded independently, so
    a Cyrillic owner or name is transmitted correctly while the separator stays
    structural. Extra segments (``branches``, a ref, a file path) are encoded
    the same way; a file path may itself contain ``/``, which is preserved.
    """
    owner, _, name = full_name.partition("/")
    path = "/repos/" + quote_path_segment(owner) + "/" + quote_path_segment(name)
    for part in extra:
        path += "/" + "/".join(quote_path_segment(seg) for seg in part.split("/"))
    return path


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
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("invalid GitHub API URL")
        self._opener = urllib.request.build_opener(_NoRedirect())
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
    def _request(self, path: str, params: dict[str, Any] | None = None, *,
                 method: str = "GET", payload: dict | None = None,
                 body: dict[str, Any] | None = None) -> Any:
        if not self._token:
            raise GitHubAuthError("no GitHub token configured (GITHUB_TOKEN)")
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        # ``payload`` is the name used by the legacy write helpers; ``body`` is
        # the name GitHubWriter uses. Accept either so both call sites work.
        if body is not None:
            payload = body
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("User-Agent", "python-dispetcher")
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
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

    def current_user(self) -> dict:
        data = self._request("/user")
        if not isinstance(data, dict) or not isinstance(data.get("login"), str):
            raise GitHubError("github returned invalid user")
        return {"login": data["login"]}

    def repository_details(self, full_name: str) -> dict:
        data = self._request(repo_path(full_name))
        if (not isinstance(data, dict) or not isinstance(data.get("id"), int)
                or not isinstance(data.get("full_name"), str) or not isinstance(data.get("private"), bool)
                or data.get("visibility") not in ("public", "private")
                or not isinstance(data.get("permissions"), dict)):
            raise GitHubError("github returned incomplete repository metadata")
        return {"id": data["id"], "full_name": data["full_name"], "private": data["private"],
                "visibility": data["visibility"], "admin": data["permissions"].get("admin") is True}

    def create_repository(self, name: str, private: bool) -> dict:
        data = self._request("/user/repos", method="POST", payload={"name": name, "private": private})
        return self._safe_write_result(data)

    def set_visibility(self, full_name: str, private: bool) -> dict:
        data = self._request(repo_path(full_name), method="PATCH", payload={"private": private})
        return self._safe_write_result(data)

    @staticmethod
    def _safe_write_result(data: Any) -> dict:
        if (not isinstance(data, dict) or not isinstance(data.get("full_name"), str)
                or not isinstance(data.get("private"), bool)):
            raise GitHubError("github returned incomplete result; check GitHub before retrying")
        return {"full_name": data["full_name"], "private": data["private"]}

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

    # --------------------------------------------------------- read details
    def get_repository(self, full_name: str) -> dict[str, Any]:
        """One repository's non-secret metadata, or ``{}`` if unusable."""
        full_name = (full_name or "").strip()
        if full_name.count("/") != 1:
            raise ValueError("full_name must be owner/name")
        return _repo_dict(self._request(repo_path(full_name)))

    def list_branches(self, full_name: str, *, limit: int = 100) -> list[dict[str, Any]]:
        full_name = (full_name or "").strip()
        if full_name.count("/") != 1:
            raise ValueError("full_name must be owner/name")
        batch = self._request(repo_path(full_name, "branches"),
                              {"per_page": min(limit, 100)})
        out = []
        for item in batch if isinstance(batch, list) else []:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                out.append({"name": item["name"],
                            "sha": (item.get("commit") or {}).get("sha"),
                            "protected": bool(item.get("protected"))})
        return out[:limit]

    def list_commits(self, full_name: str, *, branch: str | None = None,
                     limit: int = 30) -> list[dict[str, Any]]:
        full_name = (full_name or "").strip()
        if full_name.count("/") != 1:
            raise ValueError("full_name must be owner/name")
        params: dict[str, Any] = {"per_page": min(limit, 100)}
        if branch:
            params["sha"] = branch
        batch = self._request(repo_path(full_name, "commits"), params)
        out = []
        for item in batch if isinstance(batch, list) else []:
            if not isinstance(item, dict):
                continue
            commit = item.get("commit") or {}
            author = commit.get("author") or {}
            out.append({
                "sha": item.get("sha"),
                "message": (commit.get("message") or "").split("\n", 1)[0][:200],
                "author": author.get("name"),
                "date": author.get("date"),
            })
        return out[:limit]

    def list_issues(self, full_name: str, *, state: str = "open",
                    limit: int = 50) -> list[dict[str, Any]]:
        full_name = (full_name or "").strip()
        if full_name.count("/") != 1:
            raise ValueError("full_name must be owner/name")
        if state not in ("open", "closed", "all"):
            raise ValueError("state must be open, closed or all")
        batch = self._request(repo_path(full_name, "issues"),
                              {"state": state, "per_page": min(limit, 100)})
        out = []
        for item in batch if isinstance(batch, list) else []:
            if not isinstance(item, dict):
                continue
            # The issues endpoint also returns pull requests; exclude them so a
            # caller asking for issues does not get PRs mixed in.
            if "pull_request" in item:
                continue
            out.append({
                "number": item.get("number"),
                "title": item.get("title"),
                "state": item.get("state"),
                "user": (item.get("user") or {}).get("login"),
                "created_at": item.get("created_at"),
            })
        return out[:limit]

    def list_pulls(self, full_name: str, *, state: str = "open",
                   limit: int = 50) -> list[dict[str, Any]]:
        full_name = (full_name or "").strip()
        if full_name.count("/") != 1:
            raise ValueError("full_name must be owner/name")
        if state not in ("open", "closed", "all"):
            raise ValueError("state must be open, closed or all")
        batch = self._request(repo_path(full_name, "pulls"),
                              {"state": state, "per_page": min(limit, 100)})
        out = []
        for item in batch if isinstance(batch, list) else []:
            if not isinstance(item, dict):
                continue
            out.append({
                "number": item.get("number"),
                "title": item.get("title"),
                "state": item.get("state"),
                "user": (item.get("user") or {}).get("login"),
                "head": (item.get("head") or {}).get("ref"),
                "base": (item.get("base") or {}).get("ref"),
                "created_at": item.get("created_at"),
            })
        return out[:limit]

    # ------------------------------------------------------------- tree/contents
    def default_branch_sha(self, full_name: str, branch: str | None = None) -> str | None:
        """The commit SHA a branch points at, or ``None`` if it cannot be read."""
        full_name = (full_name or "").strip()
        if full_name.count("/") != 1:
            raise ValueError("full_name must be owner/name")
        params = {"sha": branch, "per_page": 1} if branch else {"per_page": 1}
        batch = self._request(repo_path(full_name, "commits"), params)
        if isinstance(batch, list) and batch and isinstance(batch[0], dict):
            return batch[0].get("sha")
        return None

    def list_tree(self, full_name: str, *, branch: str | None = None,
                  recursive: bool = True, limit: int = 500) -> list[dict[str, Any]]:
        """The repository tree at a branch. Read-only; used for context.

        Returns ``[{path, type, size}]``. ``truncated`` trees are returned as-is
        up to ``limit`` entries; the caller must not treat a short list as the
        whole repository.
        """
        full_name = (full_name or "").strip()
        if full_name.count("/") != 1:
            raise ValueError("full_name must be owner/name")
        ref = branch or self._default_branch(full_name)
        params: dict[str, Any] = {"recursive": "1"} if recursive else {}
        data = self._request(repo_path(full_name, "git", "trees", ref), params)
        tree = data.get("tree") if isinstance(data, dict) else None
        out: list[dict[str, Any]] = []
        for item in tree if isinstance(tree, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            out.append({"path": item["path"], "type": item.get("type"),
                        "size": item.get("size")})
            if len(out) >= limit:
                break
        return out

    def read_file(self, full_name: str, path: str, *, branch: str | None = None,
                  max_bytes: int = 200_000) -> str | None:
        """The decoded text of one file, or ``None`` if it is missing/binary."""
        full_name = (full_name or "").strip()
        if full_name.count("/") != 1:
            raise ValueError("full_name must be owner/name")
        data = self._request(repo_path(full_name, "contents", path),
                             {"ref": branch} if branch else None)
        if not isinstance(data, dict):
            return None
        if data.get("encoding") == "base64" and isinstance(data.get("content"), str):
            import base64
            try:
                raw = base64.b64decode(data["content"])
            except (ValueError, TypeError):
                return None
            if len(raw) > max_bytes:
                raw = raw[:max_bytes]
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if isinstance(data.get("content"), str):
            return data["content"]
        return None

    def _default_branch(self, full_name: str) -> str:
        repo = self.get_repository(full_name)
        return repo.get("default_branch") or "main"


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


class VisibilityError(ValueError):
    """A visibility change was requested without the required acknowledgement."""


def _repo_dict(item: Any) -> dict[str, Any]:
    """The safe subset of a write response. Never includes a token."""
    if not isinstance(item, dict):
        return {}
    return {
        "full_name": item.get("full_name"),
        "private": bool(item.get("private")),
        "default_branch": item.get("default_branch") or "main",
        "html_url": item.get("html_url"),
    }


class GitHubWriter:
    """Guarded GitHub writes. Every method must be called after a confirmation.

    This is a separate class from :class:`GitHubClient` on purpose: the read
    client has no write capability at all, so a code path that only holds a
    ``GitHubClient`` cannot change GitHub even by mistake. The server keeps one
    ``GitHubWriter`` and reaches for it only inside a confirmed-action handler.
    """

    def __init__(self, client: GitHubClient):
        self._client = client

    def __repr__(self) -> str:  # keep the token out of any repr
        return f"GitHubWriter(configured={self._client.configured})"

    @property
    def configured(self) -> bool:
        return self._client.configured

    def create_repository(self, *, name: str, private: bool,
                          description: str = "", auto_init: bool = True) -> dict[str, Any]:
        """Create a repository for the authenticated user.

        ``name`` must be a bare repository name, not ``owner/name``: the create
        endpoint is scoped to the authenticated account, and a slash would be a
        malformed name GitHub rejects. The call is made only after the caller
        has confirmed the action; the parameters are echoed back for the audit.
        """
        name = (name or "").strip()
        if not name or "/" in name or len(name) > 100:
            raise ValueError("repository name must be 1-100 characters without '/'")
        result = self._client._request(
            "/user/repos", method="POST",
            body={"name": name, "private": bool(private),
                  "description": (description or "")[:350],
                  "auto_init": bool(auto_init)},
        )
        return _repo_dict(result)

    def set_repository_visibility(self, *, full_name: str, private: bool,
                                  acknowledge_public: bool = False) -> dict[str, Any]:
        """Flip a repository between private and public.

        Making a repository public is irreversible in the sense that its code
        and history become world-readable (and it may already be cached, forked
        or indexed). It therefore requires ``acknowledge_public=True``, which
        the server sets only after the user has confirmed a proposal that named
        the change. Without the flag a public change is refused here.
        """
        full_name = (full_name or "").strip()
        if full_name.count("/") != 1:
            raise ValueError("full_name must be owner/name")
        if not private and not acknowledge_public:
            raise VisibilityError(
                "making a repository public requires explicit acknowledgement"
            )
        result = self._client._request(
            repo_path(full_name), method="PATCH", body={"private": bool(private)},
        )
        return _repo_dict(result)
