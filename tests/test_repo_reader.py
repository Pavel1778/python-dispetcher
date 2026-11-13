"""The repository as a chat turn reads it (``orchestration/nexus_repo_reader``).

The defect behind it, seen in production: asked "Ты вообще видишь репозиторий
и проект?", the assistant answered that it had no access to the repository.
That was true - no file list and no file ever reached the model. These tests
hold the reader to what replaced that: the real head commit, the real list of
files shaped to a budget, any text file on request, and a refusal with a
reason for everything else.

No database: the real ``GitHubClient`` against the socket stand-in for GitHub
(``tests/fake_github.py``), which records every path it was asked for - so a
test can say not only what was shown but what was never fetched.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration import nexus_repo_reader as reader  # noqa: E402
from orchestration.github_client import GitHubClient  # noqa: E402
from orchestration.nexus_repo_reader import (  # noqa: E402
    ReadRefused,
    RepositoryUnavailable,
    RepositoryView,
    Request,
    looks_secret,
    parse_requests,
)
from orchestration.providers import estimate_tokens  # noqa: E402
from tests.fake_github import FakeGitHub, FakeGitHubServer  # noqa: E402

REPO = "Pavel1778/python-dispetcher"


@pytest.fixture
def github():
    """The stand-in for GitHub, a client for it, and an empty store: what one
    test read must not be what the next one is shown."""
    reader._STORE.clear()
    gh = FakeGitHub()
    with FakeGitHubServer(gh) as server:
        gh.client = GitHubClient(base_url=server.base_url, token="reader-token")
        yield gh
    reader._STORE.clear()


def files(gh: FakeGitHub, contents: dict) -> None:
    """Make the repository hold exactly *contents*: ``{path: text or bytes}``."""
    gh.contents = dict(contents)
    gh.tree = [{"path": path, "type": "blob",
                "size": len(value if isinstance(value, bytes) else value.encode())}
               for path, value in contents.items()]


def fetched(gh: FakeGitHub, section: str) -> list[str]:
    """The paths GitHub was asked for under ``/repos/<repo>/<section>``."""
    prefix = f"/repos/{REPO}/{section}"
    return [path for path in gh.paths if path.startswith(prefix)]


# ------------------------------------------------------------------- opening
def test_a_view_is_the_head_commit_and_the_files_at_it(github):
    view = RepositoryView.open(github.client, REPO, "main")

    assert view.revision == "abc123" and view.branch == "main"
    assert view.paths == ["README.md", "requirements.txt", "src/app.py", "tests/test_app.py"]
    header = view.header()
    assert REPO in header and "abc123" in header and "4 files" in header
    # The commit is named by what it says, so a person can recognise it.
    assert "Initial commit" in header and "2026-01-03" in header
    # The list is read at the commit, not at the branch: a push between the
    # two requests cannot make the list belong to another commit.
    assert fetched(github, "git/trees") == [f"/repos/{REPO}/git/trees/abc123"]


def test_the_default_branch_is_asked_for_when_none_is_chosen(github):
    github.repos[0]["default_branch"] = "trunk"
    view = RepositoryView.open(github.client, REPO)
    assert view.branch == "trunk"


def test_the_head_is_asked_on_every_open_and_the_list_once_per_commit(github):
    RepositoryView.open(github.client, REPO, "main")
    RepositoryView.open(github.client, REPO, "main")
    assert len(fetched(github, "commits")) == 2
    assert len(fetched(github, "git/trees")) == 1

    # A push: the next question is answered from the new commit.
    github.push(REPO, "f00d", "Add the frontend")
    github.tree.append({"path": "web/index.html", "type": "blob", "size": 10})
    view = RepositoryView.open(github.client, REPO, "main")
    assert view.revision == "f00d"
    assert "web/index.html" in view.paths
    assert len(fetched(github, "git/trees")) == 2


def test_what_is_kept_is_never_shown_to_an_identity_github_refuses(github):
    """The list is kept by commit, but the commit is asked for as the caller:
    a token that cannot see the repository gets nothing out of the store."""
    RepositoryView.open(github.client, REPO, "main")
    github.token_repos["stranger"] = set()
    stranger = GitHubClient(base_url=github.client.base_url, token="stranger")
    with pytest.raises(RepositoryUnavailable) as refused:
        RepositoryView.open(stranger, REPO, "main")
    assert "404" in str(refused.value) and REPO in str(refused.value)


@pytest.mark.parametrize("status, said", [(401, "auth"), (500, "500"), (403, "auth")])
def test_a_repository_github_will_not_show_is_unavailable_with_the_reason(github, status, said):
    github.force_status = status
    with pytest.raises(RepositoryUnavailable) as refused:
        RepositoryView.open(github.client, REPO, "main")
    assert said in str(refused.value)


def test_no_token_is_said_in_words(github):
    # An empty token, not ``None``: ``None`` means "take the server's own".
    with pytest.raises(RepositoryUnavailable) as refused:
        RepositoryView.open(GitHubClient(base_url=github.client.base_url, token=""), REPO)
    assert "GITHUB_TOKEN" in str(refused.value)
    assert github.paths == []


def test_a_repository_with_no_commit_is_a_repository_with_no_files(github):
    github.force_status = 409                      # GitHub: "Git Repository is empty."
    view = RepositoryView.open(github.client, REPO, "main")
    assert len(view) == 0 and view.revision == ""
    assert "no commit" in view.header()


def test_a_name_that_is_not_owner_and_name_is_refused_before_any_request(github):
    with pytest.raises(RepositoryUnavailable):
        RepositoryView.open(github.client, "not-a-repository")
    assert github.paths == []


# ------------------------------------------------------------------ the list
def test_every_path_is_listed_with_its_size_when_they_fit(github):
    view = RepositoryView.open(github.client, REPO, "main")
    assert view.listing(2_000).splitlines() == [
        "README.md (120 B)", "requirements.txt (30 B)", "src/app.py (340 B)",
        "tests/test_app.py (210 B)"]


def test_a_list_that_does_not_fit_folds_directories_and_stays_within_the_budget(github):
    """Installed and crowded directories first, then the deepest ones - and
    whatever is folded says how many files it holds, so it can be opened."""
    contents = {"README.md": "# x\n", "package.json": "{}\n"}
    contents.update({f"node_modules/pkg{i}/index.js": "x" for i in range(300)})
    contents.update({f"src/components/Widget{i}.tsx": "x" for i in range(120)})
    contents.update({f"src/pages/admin/users/page{i}.tsx": "x" for i in range(30)})
    contents.update({f"src/lib/util{i}.ts": "x" for i in range(5)})
    files(github, contents)
    view = RepositoryView.open(github.client, REPO, "main")

    listing = view.listing(400)
    assert estimate_tokens(listing) <= 400
    lines = listing.splitlines()
    assert "node_modules/  [300 files, not listed]" in lines
    assert "src/components/  [120 files here, not listed]" in lines
    # What is small and near the top is still there by name.
    assert "README.md (4 B)" in lines and "package.json (3 B)" in lines
    assert any(line.startswith("src/lib/util0.ts") for line in lines)
    assert not any("node_modules/pkg" in line for line in lines)

    # A folded directory opens, with its own files however many they are.
    opened = view.directory("src/components", 4_000)
    assert opened.startswith("=== DIRECTORY src/components (120 files) ===")
    assert "src/components/Widget119.tsx" in opened
    # The whole list is given when the budget allows it.
    assert len(view.listing(40_000).splitlines()) == len(contents) - 300 + 1


def test_a_directory_too_large_for_full_paths_is_listed_by_relative_names(github):
    contents = {f"app/ui/components/forms/Field{i}.tsx": "x" for i in range(400)}
    contents.update({"app/ui/components/index.ts": "x", "app/ui/components/icons/a.svg": "x"})
    files(github, contents)
    view = RepositoryView.open(github.client, REPO, "main")

    small = view.directory("app/ui/components/icons", 3_000)
    assert small.splitlines()[:2] == ["=== DIRECTORY app/ui/components/icons (1 file) ===",
                                      "app/ui/components/icons/a.svg (1 B)"]

    block = view.directory("app/ui/components/forms", 3_000)
    assert block.startswith("=== DIRECTORY app/ui/components/forms (400 files; names below "
                            "are relative to app/ui/components/forms/) ===")
    lines = block.splitlines()[1:-1]
    assert len(lines) == 400 and lines[0] == "Field0.tsx (1 B)"
    assert estimate_tokens(block) <= 3_100
    # With full paths the same budget would not have held them.
    assert estimate_tokens("\n".join(sorted(contents)[:400])) > 3_000

    # One level up, the 400 are one line - relative as well - and the rest by name.
    above = view.directory("app/ui/components", 600).splitlines()
    assert "forms/  [400 files here, not listed]" in above
    assert "index.ts (1 B)" in above and "icons/a.svg (1 B)" in above

    # More than fits even so: what is left out is counted.
    files(github, {f"gen/Model{i:05d}.cs": "x" for i in range(3_000)})
    reader._STORE.clear()
    github.push(REPO, "beef", "generated")
    huge = RepositoryView.open(github.client, REPO, "main").directory("gen", 1_000)
    assert estimate_tokens(huge) <= 1_100
    assert huge.splitlines()[-2].startswith("... and ") and "more entries, not shown" in huge


def test_a_budget_too_small_for_any_folding_says_what_was_left_out(github):
    files(github, {f"dir{i}/file{i}.txt": "x" for i in range(400)})
    view = RepositoryView.open(github.client, REPO, "main")
    listing = view.listing(120)
    assert estimate_tokens(listing) <= 120
    assert "more entries, not shown" in listing.splitlines()[-1]


def test_a_directory_that_is_not_there_is_refused(github):
    view = RepositoryView.open(github.client, REPO, "main")
    with pytest.raises(ReadRefused) as refused:
        view.directory("frontend", 500)
    assert "no directory frontend" in str(refused.value)
    assert view.directory("", 500).startswith("=== DIRECTORY (the top of the repository)")


# ----------------------------------------------------------------- the files
def test_a_file_is_read_whole_at_the_views_commit(github):
    view = RepositoryView.open(github.client, REPO, "main")
    found = view.read("src/app.py")

    assert found.text == "def add(a, b):\n    return a + b"
    assert (found.first_line, found.last_line, found.total_lines) == (1, 2, 2)
    assert not found.cut
    assert found.block() == ("=== FILE src/app.py (lines 1-2 of 2) ===\n"
                             "def add(a, b):\n    return a + b\n=== END src/app.py ===")
    # Read once: the same file at the same commit is not asked for again.
    view.read("src/app.py")
    assert fetched(github, "contents") == [f"/repos/{REPO}/contents/src/app.py"]


def test_a_long_file_is_cut_to_the_budget_and_says_how_to_read_on(github):
    text = "\n".join(f"line {i} of the module" for i in range(1, 2001)) + "\n"
    files(github, {"big.py": text})
    view = RepositoryView.open(github.client, REPO, "main")

    head = view.read("big.py", budget_tokens=300)
    assert head.first_line == 1 and head.total_lines == 2000
    assert 1 < head.last_line < 200 and head.cut
    assert estimate_tokens(head.text) <= 300
    assert f"READ: big.py:{head.last_line + 1}-" in head.block()

    part = view.read("big.py", first=1500, last=1502)
    assert part.text.splitlines() == [f"line {i} of the module" for i in (1500, 1501, 1502)]
    assert "lines 1500-1502 of 2000" in part.block()
    # A range beyond the end is the end, not an error.
    assert view.read("big.py", first=5000, last=6000).first_line == 2000


def test_one_enormous_line_is_shown_by_its_beginning(github):
    files(github, {"app.min.js": "x" * 200_000})
    view = RepositoryView.open(github.client, REPO, "main")
    found = view.read("app.min.js", budget_tokens=500)
    assert found.text.endswith("[... the rest of this line is cut]")
    assert len(found.text) < 2_000


@pytest.mark.parametrize("asked, meant", [
    ("src/app.py", "src/app.py"), ("./src/app.py", "src/app.py"), ("/src/app.py", "src/app.py"),
    ("`src/app.py`", "src/app.py"), ("SRC/App.py", "src/app.py"), ("app.py", "src/app.py"),
    ("readme.md", "README.md"), ("src\\app.py", "src/app.py"),
])
def test_a_path_is_understood_as_it_is_usually_written(github, asked, meant):
    view = RepositoryView.open(github.client, REPO, "main")
    assert view.resolve(asked) == meant


@pytest.mark.parametrize("asked", [
    "../../etc/passwd", "src/../../secret", "", "docs/missing.md", "https://example.com/x.py"])
def test_a_path_outside_the_list_is_refused_and_nothing_is_fetched(github, asked):
    """The model names paths; only a path of the listed tree is ever fetched."""
    view = RepositoryView.open(github.client, REPO, "main")
    with pytest.raises(ReadRefused):
        view.read(asked)
    assert fetched(github, "contents") == []


def test_a_refusal_names_what_is_near(github):
    files(github, {"src/a/config.py": "A = 1\n", "src/b/config.py": "B = 2\n", "web/app.ts": ""})
    view = RepositoryView.open(github.client, REPO, "main")
    with pytest.raises(ReadRefused) as refused:
        view.read("config.py")                     # two files end with it
    assert "src/a/config.py" in str(refused.value) and "src/b/config.py" in str(refused.value)
    with pytest.raises(ReadRefused) as refused:
        view.read("src")
    assert "LIST: src" in str(refused.value)


@pytest.mark.parametrize("path", [
    ".env", ".env.production", "config/.env.local", "prod.env", "deploy/id_rsa",
    "certs/server.key", "certs/server.pem", ".npmrc", "keys/release.jks",
    "gcp/service-account.json", ".git-credentials"])
def test_a_file_named_like_a_credential_is_never_fetched(github, path):
    """What the model reads travels to a model provider. A secret committed by
    mistake is not sent there because somebody asked a question."""
    assert looks_secret(path)
    files(github, {path: "TOKEN=do-not-send-this\n", "README.md": "# x\n"})
    view = RepositoryView.open(github.client, REPO, "main")
    # It is in the list - the list is of names - and it is not opened.
    assert path in view.listing(2_000)
    with pytest.raises(ReadRefused) as refused:
        view.read(path)
    assert "credentials" in str(refused.value)
    assert fetched(github, "contents") == []


@pytest.mark.parametrize("path", [
    ".env.example", ".env.sample", "config/.env.template", "environment.py", "keyboard.py",
    "docs/secrets.md", "src/token.ts"])
def test_a_template_or_an_ordinary_file_is_not_taken_for_a_credential(path):
    assert not looks_secret(path)


def test_a_binary_file_is_refused_as_binary(github):
    files(github, {"logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\xff\xfe", "README.md": "# x\n"})
    view = RepositoryView.open(github.client, REPO, "main")
    with pytest.raises(ReadRefused) as refused:
        view.read("logo.png")
    assert "not a text file" in str(refused.value)


def test_a_file_github_does_not_return_inline_is_refused_by_its_size(github):
    files(github, {"data/dump.sql": "x"})
    github.tree[0]["size"] = 5_000_000
    view = RepositoryView.open(github.client, REPO, "main")
    with pytest.raises(ReadRefused) as refused:
        view.read("data/dump.sql")
    assert "4.8 MB" in str(refused.value) and "1 MB" in str(refused.value)
    assert fetched(github, "contents") == []


def test_content_that_did_not_arrive_is_not_shown_as_an_empty_file(github):
    files(github, {"notes.txt": ""})
    github.tree[0]["size"] = 900_000               # the list says it has bytes
    view = RepositoryView.open(github.client, REPO, "main")
    with pytest.raises(ReadRefused) as refused:
        view.read("notes.txt")
    assert "without its content" in str(refused.value)
    # A file that really is empty is an empty file.
    files(github, {"empty.txt": ""})
    reader._STORE.clear()
    assert RepositoryView.open(github.client, REPO, "main").read("empty.txt").block() == \
        "=== FILE empty.txt (empty) ==="


def test_a_file_github_fails_to_return_is_refused_with_the_reason(github):
    view = RepositoryView.open(github.client, REPO, "main")
    github.force_status = 502
    with pytest.raises(ReadRefused) as refused:
        view.read("src/app.py")
    assert "502" in str(refused.value)


def test_files_named_in_a_question_are_found(github):
    view = RepositoryView.open(github.client, REPO, "main")
    assert view.mentioned("Что делает src/app.py и зачем tests/test_app.py?") == [
        "tests/test_app.py", "src/app.py"]
    assert view.mentioned("Как устроен проект?") == []
    assert view.readme() == "README.md"


# -------------------------------------------------------------- the requests
@pytest.mark.parametrize("reply, expected", [
    ("READ: src/app.py", Request((("src/app.py", None, None),))),
    ("READ: src/app.py, tests/test_app.py",
     Request((("src/app.py", None, None), ("tests/test_app.py", None, None)))),
    ("read: `src/app.py`", Request((("src/app.py", None, None),))),
    ("**READ:** src/app.py", Request((("src/app.py", None, None),))),
    ("```\nREAD: src/app.py\n```", Request((("src/app.py", None, None),))),
    ("READ: big.py:200-400", Request((("big.py", 200, 400),))),
    ("READ: big.py#L10-L20", Request((("big.py", 10, 20),))),
    ("LIST: src/components", Request((), ("src/components",))),
    ("READ: a.py\nLIST: web\nМне нужно посмотреть эти файлы.",
     Request((("a.py", None, None),), ("web",))),
    ('{"read": ["a.py", "b.py:1-5"], "list": ["web"]}',
     Request((("a.py", None, None), ("b.py", 1, 5)), ("web",))),
])
def test_a_reply_that_asks_to_open_something_is_recognised(reply, expected):
    assert parse_requests(reply) == expected


@pytest.mark.parametrize("reply", [
    "Да, я вижу репозиторий: в нём 4 файла.",
    "Файл src/app.py содержит функцию add.",
    "Чтобы узнать больше, откройте README: там описан запуск.",
    "Ответ:\nREAD: src/app.py",                    # not the first line: it is an answer
    "READ:", "LIST:   ", "", "   ", '{"tasks": []}', "{not json",
])
def test_an_answer_is_not_taken_for_a_request(reply):
    assert parse_requests(reply) is None


def test_one_request_opens_a_bounded_number_of_files():
    reply = "READ: " + ", ".join(f"f{i}.py" for i in range(30))
    assert len(parse_requests(reply).files) == reader.MAX_PATHS_PER_REQUEST
