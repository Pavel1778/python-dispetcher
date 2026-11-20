"""Defaults for the whole test session."""
import os
import socketserver

# The test suites drop and re-create the public schema of the database they
# run against. Giving every pytest process a database of its own (or, when
# that is not allowed, serialising the processes) is what stops two parallel
# runs from corrupting each other; see tests/_test_db.py.
from tests import _test_db  # noqa: F401 - the import does the setup

# Python's BaseServer.serve_forever defaults to a 0.5s select() poll interval,
# which makes every test HTTP server (WebServer, FakeCloudServer,
# FakeGitHubServer, FakeServer, FakeProvider) wait up to 500ms on shutdown().
_orig_serve_forever = socketserver.BaseServer.serve_forever


def _fast_serve_forever(self: socketserver.BaseServer, poll_interval: float = 0.02) -> None:
    return _orig_serve_forever(self, poll_interval=poll_interval)


socketserver.BaseServer.serve_forever = _fast_serve_forever  # type: ignore[method-assign]

_PATCHED_RESET_MODULES: set[str] = set()


def pytest_runtest_setup(item) -> None:
    mod = getattr(item, "module", None)
    if mod is None or mod.__name__ in _PATCHED_RESET_MODULES:
        return
    _PATCHED_RESET_MODULES.add(mod.__name__)
    dsn = getattr(mod, "DSN", None)
    for fn_name in ("_reset_schema", "_reset", "reset_schema"):
        fn = getattr(mod, fn_name, None)
        if callable(fn) and getattr(fn, "__code__", None) and fn.__code__.co_argcount == 0:
            setattr(mod, fn_name, lambda _d=dsn: _test_db.reset_schema(_d))

# A server advances autonomous runs itself unless NEXUS_LAB_INLINE says no
# (``orchestration.web_server.lab_inline_default``). Most tests here start a
# server to exercise something else, and many create a run to look at it as it
# was planned; a loop that started its tasks would be a second actor in every
# one of them. So for the test session the switch is off, and a test about the
# loop turns it on: ``App(..., lab_inline=True)``, or the variable itself.
os.environ.setdefault("NEXUS_LAB_INLINE", "0")

# The SSRF policy refuses loopback/private targets unless the operator opts in.
# A test session's fake Cloud, GitHub and provider servers all listen on
# 127.0.0.1, so the session opts in here; the security tests clear the variable
# to prove the guard refuses a private target. A production deployment leaves it
# unset, so a base URL aimed at its own network is refused. The same switch
# governs the check of a stored connection's address (``net_guard``).
os.environ.setdefault("NEXUS_ALLOW_PRIVATE_TARGETS", "1")

# A test session never speaks to the real GitHub. A server that is given no
# GitHub client of its own takes its token from GITHUB_TOKEN, and a developer's
# shell usually has one. A chat turn reads the project's repository, so with
# that token in place a test's question would go to api.github.com under the
# developer's own identity. Without it such a server has no GitHub identity and
# says so, without a request. A test that needs a token sets one, together
# with the address of the stand-in (``tests/fake_github.py``).
os.environ.pop("GITHUB_TOKEN", None)


def _isolate_database() -> None:
    """Give this pytest process its own database.

    Several test modules drop and re-create the whole schema (``_reset_schema``)
    or create and drop the Supabase roles. Two pytest processes sharing one
    database therefore fight: one drops a table or a role while the other is
    mid-test, and the failures look like real defects. That is exactly what
    happened when two suites were run against one database in parallel
    (``test_schema_bootstrap`` failed with ``DependentObjectsStillExist`` and a
    browser test failed too; each passed alone).

    So, for a **local, disposable** database, each process makes its own copy of
    the base database and points the session at it. The base database is only
    read (its name); a sibling ``<base>_p<id>`` is created, empty, and the
    session's DSN is rewritten to it. The first connection applies the
    migrations, exactly as a fresh database is set up in production.

    It does nothing when:

    * ``NEXUS_TEST_ISOLATE_DB`` is ``0``/``false``/``no``/``off`` (the escape
      hatch for an operator who wants every run on one named database);
    * the DSN is not local (a remote host is not ours to create databases on);
    * the base database name already carries the process suffix (a DSN that was
      already isolated, e.g. exported by CI);
    * the database cannot be created (no privilege, or it already exists) - the
      base DSN is then left in place, so the suite still runs.
    """
    if os.environ.get("NEXUS_TEST_ISOLATE_DB", "").strip().lower() in (
            "0", "false", "no", "off"):
        return
    if os.environ.get("NEXUS_TEST_BASE_DB_URL"):
        return  # already isolated this process; do not isolate twice
    key = ("TEST_SUPABASE_DB_URL" if os.environ.get("TEST_SUPABASE_DB_URL")
           else "SUPABASE_DB_URL")
    url = os.environ.get(key)
    if not url:
        return
    import hashlib
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    if (parts.hostname or "") not in ("127.0.0.1", "localhost", "::1"):
        return  # not a local disposable database; do not touch it
    base = (parts.path or "/").lstrip("/")
    if not base:
        return
    suffix = hashlib.sha1(
        f"{os.getpid()}-{os.getcwd()}-{url}".encode()).hexdigest()[:8]
    if base.endswith(f"_p{suffix}"):
        return
    name = f"{base}_p{suffix}"
    try:
        import psycopg

        with psycopg.connect(url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,))
                if cur.fetchone() is None:
                    cur.execute(f'CREATE DATABASE "{name}"')
    except Exception:  # noqa: BLE001 - isolation is best effort, never fatal
        return
    new_url = urlunsplit((parts.scheme, parts.netloc,
                          "/" + name, parts.query, parts.fragment))
    os.environ["NEXUS_TEST_BASE_DB_URL"] = url
    os.environ[key] = new_url
    os.environ["SUPABASE_DB_URL"] = new_url


_isolate_database()
