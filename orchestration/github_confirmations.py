"""Human-session-only, short-lived, one-use GitHub action confirmations."""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from typing import Any

from .github_client import GitHubClient

NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
OWNER = re.compile(r"[A-Za-z0-9-]{1,100}\Z")


def _name(value: Any) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value) or value in (".", ".."):
        raise ValueError("invalid repository name")
    return value


def _session_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Confirmations:
    def __init__(self, *, ttl: int = 120):
        self.ttl = ttl
        self._items: dict[str, tuple[float, str, dict]] = {}
        self._lock = threading.Lock()

    def prepare(self, github: GitHubClient, session: str, body: Any) -> dict:
        if not isinstance(body, dict) or set(body) != {"action", "name", "private"}:
            raise ValueError("provide action, name and private")
        action, name, private = body["action"], body["name"], body["private"]
        if type(private) is not bool:
            raise ValueError("private must be boolean")
        actor = github.current_user()["login"]
        if action == "create":
            name = _name(name)
            data = {"action": action, "name": name, "private": private, "actor": actor,
                    "full_name": f"{actor}/{name}"}
        elif action == "visibility":
            if not isinstance(name, str) or name.count("/") != 1:
                raise ValueError("name must be owner/repository")
            owner, repo_name = name.split("/")
            if not OWNER.fullmatch(owner):
                raise ValueError("invalid owner")
            _name(repo_name)
            repo = github.repository_details(name)
            if not repo["admin"]:
                raise ValueError("GitHub admin permission required")
            if repo["full_name"].lower() != name.lower():
                raise ValueError("GitHub returned a different repository")
            if repo["private"] == private:
                raise ValueError("repository already has that visibility")
            data = {"action": action, "name": repo["full_name"], "full_name": repo["full_name"],
                    "private": private, "actor": actor, "original": repo}
        else:
            raise ValueError("unknown action")
        nonce = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            self._items = {k: v for k, v in self._items.items() if v[0] > now}
            if len(self._items) >= 100:
                raise ValueError("too many pending confirmations")
            self._items[nonce] = (now + self.ttl, _session_key(session), data)
        return {"confirmation": nonce, "action": action, "full_name": data["full_name"],
                "private": private, "actor": actor, "expires_in": self.ttl}

    def confirm(self, github: GitHubClient, session: str, nonce: Any) -> dict:
        if not isinstance(nonce, str) or len(nonce) > 100:
            raise ValueError("invalid confirmation")
        with self._lock:
            entry = self._items.get(nonce)
            if entry is None or entry[0] <= time.monotonic() or entry[1] != _session_key(session):
                raise ValueError("confirmation is invalid or expired")
            del self._items[nonce]  # Consume before any network call, including ambiguous failures.
        data = entry[2]
        if github.current_user()["login"] != data["actor"]:
            raise ValueError("GitHub identity changed; prepare again")
        if data["action"] == "create":
            return github.create_repository(data["name"], data["private"])
        current = github.repository_details(data["name"])
        if current != data["original"]:
            raise ValueError("repository state or permission changed; prepare again")
        return github.set_visibility(data["name"], data["private"])
