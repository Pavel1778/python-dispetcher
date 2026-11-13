"""The project's repository as a chat turn reads it: from GitHub, when asked.

Before this module the chat knew a repository only through a stored summary
(``nexus_context``): its languages, its top-level folders, a few known files,
as they were when somebody last pressed "refresh". No file list and no file
ever reached the model, and its instructions told it not to speak of files it
was not given. Asked "do you see the repository?", it answered that it did
not - and that was true.

A chat turn now gets a :class:`RepositoryView`:

* **the head commit** of the branch, asked for on every turn with the GitHub
  identity the project reads that repository with. Everything else is read at
  that commit, so one answer never mixes two states of the repository;
* **the list of files** at that commit, shaped to fit a budget of model
  tokens: every path when they fit, otherwise the crowded and the deep
  directories as one line each - and any of those can be opened
  (:meth:`RepositoryView.listing`, :meth:`RepositoryView.directory`);
* **any text file**, whole or by line range, cut to a budget and saying so
  when it is cut (:meth:`RepositoryView.read`).

The models behind the chat take a prompt and return text; they have no tool
calls. So reading is a short exchange in plain text (``READ: path``,
``LIST: dir`` - :func:`parse_requests`) that ``nexus_chat`` runs for a bounded
number of rounds.

What cannot be read, and why:

* a path that is not in the file list - the model names paths, the server
  decides what a path is, and nothing outside the listed tree is ever fetched;
* a file that looks like a credential (``.env``, a private key, ``.npmrc``):
  what the model reads is sent to a model provider, and a secret that was
  committed by mistake should not travel further because of a question;
* a binary file, and a file GitHub does not return inline (over a megabyte).

What is read from GitHub is kept for a while in this process, keyed by the
commit it was read at: a file at a commit never changes. The head commit is
never taken from that store - it is asked for with the caller's own identity
on every turn, which is also what proves that this caller may read the
repository at all before anything stored is shown to it.
"""
from __future__ import annotations

import json
import posixpath
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .github_client import GitHubClient, GitHubError
from .providers import estimate_tokens

#: Entries of a repository tree that are read at most. GitHub itself returns
#: up to 100 000; a list this long is already far more than a prompt can show.
TREE_LIMIT = 20_000
#: Bytes of one file that are fetched. A longer file is read up to here.
MAX_FILE_BYTES = 400_000
#: GitHub returns a file inline up to this size; a larger one comes back with
#: no content at all, which must not be taken for an empty file.
GITHUB_INLINE_BYTES = 1_000_000
#: Files one request may name, and directories.
MAX_PATHS_PER_REQUEST = 8

#: Directories whose contents are never listed by default: what is installed
#: or built, not written. Each is one line, and can be opened like any other.
NOISE_DIRS = frozenset({
    "node_modules", "vendor", ".git", "dist", "build", "out", "target", "__pycache__",
    ".venv", "venv", ".next", ".nuxt", ".idea", ".gradle", "Pods", ".terraform",
    "bower_components", "coverage", ".cache", ".pytest_cache", ".mypy_cache",
})
#: A directory with more files of its own than this is shown as one line when
#: the whole list does not fit.
WIDE_DIRECTORY = 40

_README_NAMES = ("readme.md", "readme.rst", "readme.txt", "readme")

_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx", ".ppk")
_SECRET_NAMES = frozenset({
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".npmrc", ".pypirc", ".netrc",
    ".git-credentials", ".htpasswd", "credentials.json", "secrets.json", "secrets.yaml",
    "secrets.yml", "service-account.json", "serviceaccount.json",
})
_ENV_TEMPLATES = (".example", ".sample", ".template", ".dist", ".defaults", ".tpl")


def looks_secret(path: str) -> bool:
    """Whether *path* is named like a file that holds credentials.

    By name only - the content is not looked at, because the point is not to
    fetch it. ``.env`` and its variants count, except the templates a
    repository is meant to carry (``.env.example``).
    """
    name = posixpath.basename(path).lower()
    if name == ".env" or name.startswith(".env."):
        return not name.endswith(_ENV_TEMPLATES)
    if name.endswith(".env"):
        return True
    return name in _SECRET_NAMES or name.endswith(_SECRET_SUFFIXES)


class RepositoryUnavailable(RuntimeError):
    """The repository could not be read for this turn; the message says why."""


@dataclass(frozen=True)
class RepositoryAccess:
    """What a chat turn was given of the project's repository.

    One of three things, and the turn says which to the model and to the
    person: a :class:`RepositoryView` to read; the reason the repository could
    not be read this time (``error``); or nothing at all, because no
    repository is connected to the project (``full_name`` is empty).
    """

    full_name: str = ""
    branch: str = ""
    view: "RepositoryView | None" = None
    error: str | None = None


class ReadRefused(RuntimeError):
    """One file or directory could not be shown; the message says why."""


@dataclass(frozen=True)
class RepoFile:
    """A file, or a part of one, as it is shown to the model."""

    path: str
    text: str
    first_line: int
    last_line: int
    total_lines: int
    #: The file is longer than what GitHub was asked for (``MAX_FILE_BYTES``).
    incomplete: bool = False

    @property
    def cut(self) -> bool:
        return self.first_line > 1 or self.last_line < self.total_lines or self.incomplete

    def block(self) -> str:
        """The file as a block of a prompt, saying which part of it this is."""
        if not self.total_lines:
            return f"=== FILE {self.path} (empty) ==="
        span = f"lines {self.first_line}-{self.last_line} of {self.total_lines}"
        if self.incomplete:
            span += "+ (the file is longer than what is read of one file)"
        if self.last_line < self.total_lines:
            span += (f"; for more: READ: {self.path}:{self.last_line + 1}-"
                     f"{min(self.total_lines, self.last_line + 400)}")
        return f"=== FILE {self.path} ({span}) ===\n{self.text}\n=== END {self.path} ==="


@dataclass(frozen=True)
class Request:
    """What a model's reply asks to open: files (with an optional line range)
    and directories."""

    files: tuple[tuple[str, int | None, int | None], ...] = ()
    directories: tuple[str, ...] = ()


_REQUEST_LINE = re.compile(r"^[\s>*`\-]*(READ|LIST)\s*\**\s*:[\s*`]*(.+?)[\s`*]*$",
                           re.IGNORECASE)
_RANGE = re.compile(r"^(?P<path>.+?)[:#]L?(?P<a>\d+)\s*[-–]\s*L?(?P<b>\d+)$")


def _clean(item: str) -> str:
    return item.strip().strip("`'\"[]()<>*").strip()


def _file_item(item: str) -> tuple[str, int | None, int | None] | None:
    item = _clean(item)
    if not item:
        return None
    ranged = _RANGE.match(item)
    if ranged:
        return _clean(ranged["path"]), int(ranged["a"]), int(ranged["b"])
    return item, None, None


def parse_requests(reply: str) -> Request | None:
    """What *reply* asks to open, or ``None`` when it is an answer.

    A request is a reply whose first line is ``READ: path, path`` or
    ``LIST: dir`` - the form the model is told to use. Markdown around it is
    tolerated (a code fence, a bullet, bold), further ``READ``/``LIST`` lines
    are taken too, and anything else after the first line is ignored: a model
    that explains why it wants a file still gets the file. A JSON object with
    ``read`` and ``list`` arrays is understood as well. Everything else -
    including an answer that merely mentions a path - is an answer.
    """
    lines = [line for line in (reply or "").strip().splitlines()
             if line.strip() and not line.strip().startswith("```")]
    if not lines:
        return None
    files: list[tuple[str, int | None, int | None]] = []
    directories: list[str] = []
    if lines[0].lstrip().startswith("{"):
        try:
            data = json.loads("\n".join(lines))
        except ValueError:
            return None
        if not isinstance(data, dict) or not (set(data) & {"read", "list"}):
            return None
        for value in data.get("read") or []:
            item = _file_item(str(value))
            if item:
                files.append(item)
        directories = [_clean(str(value)) for value in data.get("list") or []]
    else:
        if not _REQUEST_LINE.match(lines[0]):
            return None
        for line in lines:
            found = _REQUEST_LINE.match(line)
            if not found:
                continue
            for raw in found.group(2).split(","):
                if found.group(1).upper() == "READ":
                    item = _file_item(raw)
                    if item:
                        files.append(item)
                elif _clean(raw):
                    directories.append(_clean(raw))
    directories = [d for d in directories if d]
    if not files and not directories:
        return None
    return Request(tuple(files[:MAX_PATHS_PER_REQUEST]),
                   tuple(directories[:MAX_PATHS_PER_REQUEST]))


class _Store:
    """What was read from GitHub, kept by the commit it was read at.

    Bounded twice over: by the number of entries and, for file contents, by
    their total size. The least recently used go first. Used from the request
    threads of the web server, hence the lock.
    """

    def __init__(self, *, trees: int = 8, files: int = 300, file_chars: int = 4_000_000):
        self._lock = threading.Lock()
        self._trees: OrderedDict[tuple, list] = OrderedDict()
        self._files: OrderedDict[tuple, Any] = OrderedDict()
        self._max_trees, self._max_files, self._max_chars = trees, files, file_chars
        self._chars = 0

    @staticmethod
    def _size(value: Any) -> int:
        return len(value["content"]) if isinstance(value, dict) else 0

    def tree(self, key: tuple) -> list | None:
        with self._lock:
            if key in self._trees:
                self._trees.move_to_end(key)
            return self._trees.get(key)

    def keep_tree(self, key: tuple, value: list) -> None:
        with self._lock:
            self._trees[key] = value
            self._trees.move_to_end(key)
            while len(self._trees) > self._max_trees:
                self._trees.popitem(last=False)

    def file(self, key: tuple) -> tuple[bool, Any]:
        with self._lock:
            if key not in self._files:
                return False, None
            self._files.move_to_end(key)
            return True, self._files[key]

    def keep_file(self, key: tuple, value: Any) -> None:
        with self._lock:
            if key in self._files:
                self._chars -= self._size(self._files[key])
            self._files[key] = value
            self._files.move_to_end(key)
            self._chars += self._size(value)
            while self._files and (len(self._files) > self._max_files
                                   or self._chars > self._max_chars):
                _key, dropped = self._files.popitem(last=False)
                self._chars -= self._size(dropped)

    def clear(self) -> None:
        with self._lock:
            self._trees.clear()
            self._files.clear()
            self._chars = 0


_STORE = _Store()


def _size_text(size: Any) -> str:
    if not isinstance(size, int) or size < 0:
        return ""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _plural(count: int) -> str:
    return f"{count} file" + ("" if count == 1 else "s")


class RepositoryView:
    """One repository at one commit, as a chat turn may read it."""

    def __init__(self, client: GitHubClient, full_name: str, branch: str, *,
                 revision: str, commit: dict | None, entries: list[dict],
                 incomplete: bool = False):
        self.client = client
        self.full_name = full_name
        self.branch = branch
        #: The commit everything is read at; empty for a repository with no commit.
        self.revision = revision
        self.commit = commit or {}
        #: ``True`` when the repository has more entries than were read.
        self.incomplete = incomplete
        self._sizes: dict[str, Any] = {}
        for entry in entries:
            path = entry.get("path")
            if entry.get("type") == "blob" and isinstance(path, str) and path:
                self._sizes[path] = entry.get("size")
        self._paths = sorted(self._sizes)
        self._lower = {}
        for path in self._paths:
            self._lower.setdefault(path.lower(), path)

    # ------------------------------------------------------------- opening
    @classmethod
    def open(cls, client: GitHubClient, full_name: str,
             branch: str | None = None) -> "RepositoryView":
        """Read the head of *branch* and the list of files there.

        Raises :class:`RepositoryUnavailable` with the reason when GitHub
        cannot be asked, refuses the identity, or has no such repository or
        branch. A repository with no commit yet is not an error: it is a
        repository with no files.
        """
        full_name = (full_name or "").strip()
        if full_name.count("/") != 1:
            raise RepositoryUnavailable("the repository is not named as owner/name")
        try:
            if not branch:
                branch = (client.get_repository(full_name) or {}).get("default_branch") or "main"
            try:
                commits = client.list_commits(full_name, branch=branch, limit=1)
            except GitHubError as e:
                if "409" not in str(e):          # 409: "Git Repository is empty"
                    raise
                return cls(client, full_name, branch, revision="", commit=None, entries=[])
            if not commits or not commits[0].get("sha"):
                raise RepositoryUnavailable(f"GitHub reports no commit on branch {branch}")
            commit = commits[0]
            revision = str(commit["sha"])
            # The commit above was asked for as this caller; what is kept is
            # kept by commit, so it is shown only to a caller GitHub has just
            # answered about this repository.
            key = (full_name.lower(), revision)
            entries = _STORE.tree(key)
            if entries is None:
                entries = client.list_tree(full_name, branch=revision, limit=TREE_LIMIT)
                _STORE.keep_tree(key, entries)
        except RepositoryUnavailable:
            raise
        except GitHubError as e:
            reason = str(e)
            if "404" in reason:
                reason = (f"GitHub has no repository {full_name} with a branch {branch} "
                          "that this project's GitHub identity can see (404)")
            raise RepositoryUnavailable(reason) from e
        except ValueError as e:
            raise RepositoryUnavailable(str(e)) from e
        return cls(client, full_name, branch, revision=revision, commit=commit,
                   entries=entries, incomplete=len(entries) >= TREE_LIMIT)

    # ------------------------------------------------------------- the list
    @property
    def paths(self) -> list[str]:
        """Every file path, sorted."""
        return list(self._paths)

    def __len__(self) -> int:
        return len(self._paths)

    def header(self) -> str:
        """One paragraph saying what is being looked at."""
        if not self.revision:
            return (f"Repository {self.full_name}, branch {self.branch}: it has no commit "
                    "yet, so there are no files to read.")
        when = str(self.commit.get("date") or "")[:10]
        subject = str(self.commit.get("message") or "").strip()
        about = ", ".join(part for part in (when, f'"{subject}"' if subject else "") if part)
        text = (f"Repository {self.full_name}, branch {self.branch}, at commit "
                f"{self.revision[:12]}" + (f" ({about})" if about else "")
                + f". Read from GitHub for this request. {_plural(len(self))}.")
        if self.incomplete:
            text += (f" The list is incomplete: only the first {TREE_LIMIT} entries of the "
                     "repository were read.")
        return text

    def readme(self) -> str | None:
        """The README at the top of the repository, if it has one."""
        for name in _README_NAMES:
            if name in self._lower and "/" not in self._lower[name]:
                return self._lower[name]
        return None

    def mentioned(self, text: str, limit: int = 4) -> list[str]:
        """Files of the repository that *text* names by their full path."""
        found = [path for path in self._paths
                 if len(path) >= 5 and ("/" in path or "." in path) and path in text]
        # The longest first: "src/app.py" rather than an "app.py" inside it.
        found.sort(key=lambda path: (-len(path), path))
        kept: list[str] = []
        for path in found:
            if not any(path in other for other in kept):
                kept.append(path)
        return kept[:limit]

    def listing(self, budget_tokens: int, *, under: str = "", relative: bool = False) -> str:
        """The files as lines of ``path (size)``, within *budget_tokens*.

        Every path, when they fit. Otherwise the list is shortened in the
        order that loses least: first the directories that hold what is
        installed or built (``NOISE_DIRS``) and the crowded ones
        (``WIDE_DIRECTORY``) become one line each; then the deepest
        directories, level by level, until the list fits. A directory shown
        as one line says how many files it holds and can be opened with
        ``LIST``. With *under*, only that directory is listed - and its own
        files are shown however many they are, since that is what was asked.
        With *relative*, names are written from *under* down, which is less
        than half the text for a deep directory.
        """
        prefix = under.rstrip("/") + "/" if under else ""
        paths = [path for path in self._paths if path.startswith(prefix)]
        if not paths:
            return "(no files)"
        direct: dict[str, int] = {}
        below: dict[str, int] = {}
        for path in paths:
            folder = posixpath.dirname(path)
            direct[folder] = direct.get(folder, 0) + 1
            while folder and folder != under.rstrip("/"):
                below[folder] = below.get(folder, 0) + 1
                folder = posixpath.dirname(folder)

        cut = len(prefix) if relative else 0

        def render(closed: set[str], crowded: set[str]) -> list[str]:
            lines: list[str] = []
            seen: set[str] = set()
            for path in paths:
                folder = posixpath.dirname(path)
                top = next((c for c in _ancestors(folder) if c in closed), None)
                if top is not None:
                    if top not in seen:
                        seen.add(top)
                        lines.append(f"{top[cut:]}/  [{_plural(below[top])}, not listed]")
                    continue
                if folder in crowded:
                    if folder not in seen:
                        seen.add(folder)
                        lines.append(
                            f"{folder[cut:]}/  [{_plural(direct[folder])} here, not listed]")
                    continue
                size = _size_text(self._sizes.get(path))
                lines.append(f"{path[cut:]} ({size})" if size else path[cut:])
            return lines

        def fits(lines: list[str]) -> bool:
            return estimate_tokens("\n".join(lines)) <= budget_tokens

        closed = {folder for folder in below
                  if posixpath.basename(folder) in NOISE_DIRS and folder != under.rstrip("/")}
        crowded: set[str] = set()
        lines = render(closed, crowded)
        if not fits(lines):
            crowded = {folder for folder, count in direct.items()
                       if count > WIDE_DIRECTORY and folder != under.rstrip("/")}
            lines = render(closed, crowded)
        base = prefix.count("/")
        depth = max((folder.count("/") + 1 for folder in below), default=0)
        while not fits(lines) and depth > base:
            closed |= {folder for folder in below if folder.count("/") + 1 == depth}
            lines = render(closed, crowded)
            depth -= 1
        if not fits(lines):
            kept: list[str] = []
            for line in lines:
                if estimate_tokens("\n".join(kept + [line])) > budget_tokens - 30:
                    break
                kept.append(line)
            kept.append(f"... and {len(lines) - len(kept)} more entries, not shown")
            lines = kept
        return "\n".join(lines)

    def directory(self, name: str, budget_tokens: int) -> str:
        """The block that answers ``LIST: name``.

        Full paths, as everywhere else, while every file of the directory can
        be shown that way. A directory too large for that is listed by names
        relative to it - the block says so - which shows two or three times
        as many of them; what still does not fit is counted, not dropped
        silently.
        """
        folder = self._folder(name)
        count = sum(1 for path in self._paths if path.startswith(folder + "/")) if folder \
            else len(self._paths)
        shown = folder or "(the top of the repository)"
        body, note = self.listing(budget_tokens, under=folder), ""
        if folder and ("not listed]" in body or body.endswith("not shown")):
            body = self.listing(budget_tokens, under=folder, relative=True)
            note = f"; names below are relative to {folder}/"
        return (f"=== DIRECTORY {shown} ({_plural(count)}{note}) ===\n"
                f"{body}\n=== END {shown} ===")

    def _folder(self, name: str) -> str:
        folder = _normal(name)
        if folder in ("", "."):
            return ""
        if any(path.startswith(folder + "/") for path in self._paths):
            return folder
        lowered = folder.lower()
        for path in self._paths:
            if path.lower().startswith(lowered + "/"):
                return path[:len(folder)]
        raise ReadRefused(f"there is no directory {name} in the repository")

    # ------------------------------------------------------------ the files
    def resolve(self, name: str) -> str:
        """The path in the repository that *name* means.

        The path as listed; else the same path in another letter case; else,
        when exactly one file ends with it, that file (a model that writes
        ``app.py`` for ``src/app.py``). Anything else is refused, with the
        nearest names: a path is never taken on trust.
        """
        path = _normal(name)
        if not path or ".." in path.split("/"):
            raise ReadRefused(f"{name!r} is not a path inside the repository")
        if path in self._sizes:
            return path
        if path.lower() in self._lower:
            return self._lower[path.lower()]
        ending = [p for p in self._paths if p.endswith("/" + path)]
        if len(ending) == 1:
            return ending[0]
        base = posixpath.basename(path).lower()
        near = ending or [p for p in self._paths if posixpath.basename(p).lower() == base]
        hint = f"; files with that name: {', '.join(near[:5])}" if near else ""
        if any(p.startswith(path + "/") for p in self._paths):
            hint = f"; it is a directory - ask LIST: {path}"
        raise ReadRefused(f"there is no file {path} in the list{hint}")

    def read(self, name: str, *, first: int | None = None, last: int | None = None,
             budget_tokens: int = 5_000) -> RepoFile:
        """One file at this view's commit, or the lines *first*..*last* of it,
        within *budget_tokens*. What does not fit is left off the end, and the
        result says which lines it holds. Raises :class:`ReadRefused`."""
        path = self.resolve(name)
        if looks_secret(path):
            raise ReadRefused(
                f"{path} is named like a file that holds credentials; such files are "
                "not opened in the chat")
        size = self._sizes.get(path)
        if isinstance(size, int) and size > GITHUB_INLINE_BYTES:
            raise ReadRefused(
                f"{path} is {_size_text(size)}; GitHub does not return a file larger than "
                "1 MB inline, so it cannot be opened in the chat")
        key = (self.full_name.lower(), self.revision, path)
        known, found = _STORE.file(key)
        if not known:
            try:
                found = self.client.read_text_file(self.full_name, path, branch=self.revision,
                                                   max_bytes=MAX_FILE_BYTES)
            except GitHubError as e:
                raise ReadRefused(f"{path} could not be read from GitHub: {e}") from e
            _STORE.keep_file(key, found)
        if found is None:
            raise ReadRefused(
                f"{path} is not a text file GitHub returns (binary, or larger than 1 MB)")
        if not found["content"] and isinstance(size, int) and size > 0:
            # The list says the file has bytes and GitHub sent none of them.
            raise ReadRefused(
                f"{path} ({_size_text(size)}) came back from GitHub without its content")
        lines = found["content"].splitlines()
        total = len(lines)
        begin = min(max(1, int(first or 1)), max(total, 1))
        end = min(total, int(last)) if last else total
        if end < begin:
            end = begin
        kept: list[str] = []
        used = 0
        for line in lines[begin - 1:end]:
            cost = estimate_tokens(line)
            if kept and used + cost > budget_tokens:
                break
            if used + cost > budget_tokens:
                # One line longer than the whole budget (a minified file): its
                # beginning is shown, and it says that it is only that.
                line = line[:max(200, budget_tokens * 2)] + " [... the rest of this line is cut]"
            kept.append(line)
            used += cost
        return RepoFile(path=path, text="\n".join(kept), first_line=begin,
                        last_line=begin + len(kept) - 1 if kept else 0, total_lines=total,
                        incomplete=bool(found.get("truncated")))


def _ancestors(folder: str):
    """*folder* and every directory above it, outermost first."""
    parts = folder.split("/") if folder else []
    for i in range(1, len(parts) + 1):
        yield "/".join(parts[:i])


def _normal(name: str) -> str:
    """A path as GitHub writes it: no quotes, no leading ``./`` or ``/``."""
    path = _clean(name or "").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return posixpath.normpath(path.strip("/")) if path.strip("/") else ""
