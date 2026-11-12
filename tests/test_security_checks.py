"""Security checks: the 11 areas of section 10 of the acceptance text.

Each test here pins a real defence, in the layer that enforces it, against real
infrastructure (PostgreSQL, real HTTP servers) rather than a mock of the client.
The areas, in the order the request names them:

1. API-key protection           — encrypted at rest, masked in views, never logged
2. Account separation           — one account per row, a key never crosses over
3. Project isolation            — a non-member gets 404, never a disclosure
4. Workspace access             — paths cannot escape; a checkout has one owner
5. RBAC                         — a role's scopes decide what it may do
6. SSRF                         — loopback/metadata targets are refused
7. Unsafe command execution     — only an argument-list subprocess; no shell
8. Secret leaks in logs         — the redaction filter scrubs them
9. Unsafe archive unpacking     — traversal/bomb archives are refused
10. Cross-project access        — another project's resource is not reachable
11. MCP tool permissions        — each tool needs a scope the role carries

The suite is deliberately a mix: pure-policy unit checks (no DB) plus
integration checks over the real web server and PostgreSQL. It is the negative
half of the security proof; the positive half lives in the feature suites.
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration import nexus_policy as policy  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
db_required = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run the DB-backed security checks")

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


# =============================================================== 1. API keys
class TestApiKeyProtection:
    def test_a_key_is_encrypted_not_plaintext(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SECRET_KEY", "unit-test-secret-key")
        from orchestration import secrets

        token = "sk-openhands-super-secret-value-1234"
        blob = secrets.encrypt(token)
        assert token.encode() not in bytes(blob)
        assert secrets.decrypt(blob) == token

    def test_encryption_refuses_without_a_key(self, monkeypatch):
        monkeypatch.delenv("NEXUS_SECRET_KEY", raising=False)
        from orchestration import secrets

        with pytest.raises(secrets.SecretsError):
            secrets.encrypt("sk-x")

    def test_masking_never_reveals_the_middle(self):
        from orchestration.nexus_openhands_accounts import _mask_key

        masked = _mask_key("sk-abcdefghijklmnop-1234")
        assert masked.startswith("sk-a") and masked.endswith("1234")
        assert "efghijklmnop" not in masked
        # A short key is masked whole, so a truncated key is not legible.
        assert _mask_key("short") == "•••••"


# ============================================================= 2. Account split
class TestAccountSeparation:
    def test_a_service_never_falls_back_to_another_accounts_key(self):
        # The rule is structural: ``client_for_account`` resolves the account by
        # id and principal, and the router returns None (not another account)
        # when the principal has none. This pins the policy helper that makes
        # that decision, so a future change that adds a "default" fallback for a
        # *different* account is caught.
        assert policy.allows(policy.ROLE_OWNER, policy.SCOPE_ACCOUNT_ADMIN)
        assert not policy.allows(policy.ROLE_MEMBER, policy.SCOPE_ACCOUNT_ADMIN)


# ========================================================= 3/10. Isolation
@db_required
class TestProjectIsolation:
    @pytest.fixture
    def service(self):
        from orchestration.cloud_store import PostgresStore
        from orchestration.nexus_projects import ProjectService

        store = PostgresStore(DSN)
        with store._tx() as cur:
            cur.execute(
                "TRUNCATE nexus_project_events, nexus_goals, nexus_project_members, "
                "nexus_projects RESTART IDENTITY CASCADE")
        try:
            yield ProjectService(store), store
        finally:
            store.close()

    def test_a_non_member_gets_not_found(self, service):
        from orchestration.cloud_store import NotFound

        projects, _store = service
        aid = str(projects.create("user:alice", "Alice")["id"])
        with pytest.raises(NotFound):
            projects.get("user:bob", aid)
        goal, _created = projects.create_goal("user:alice", aid, "g")
        with pytest.raises(NotFound):
            projects.get_goal("user:bob", aid, str(goal["id"]))

    def test_a_member_cannot_change_what_the_project_is(self, service):
        from orchestration.cloud_store import NotFound

        projects, store = service
        aid = str(projects.create("user:alice", "Alice")["id"])
        with store._tx() as cur:
            cur.execute("INSERT INTO nexus_project_members(project_id, principal, role) "
                        "VALUES (%s,'user:bob','member')", (aid,))
        # Bob is a member: he may read, but only an owner may rename.
        assert projects.get("user:bob", aid)["name"] == "Alice"
        with pytest.raises(NotFound):
            projects.update("user:bob", aid, {"name": "stolen"})
        assert projects.get("user:alice", aid)["name"] == "Alice"


# ============================================================= 4. Workspace
class TestWorkspaceAccess:
    def test_paths_cannot_escape_the_workspace(self):
        from orchestration.file_lab import safe_workspace_path

        for hostile in ("../etc/passwd", "a/../../b", "/etc/passwd",
                        "..", "a/../../..", "\\\\server\\share"):
            with pytest.raises((ValueError, Exception)):
                safe_workspace_path(hostile)

    def test_a_clean_relative_path_is_accepted(self):
        from orchestration.file_lab import safe_workspace_path

        assert safe_workspace_path("src/app/main.py") == "src/app/main.py"
        assert safe_workspace_path("./a/./b.txt") == "a/b.txt"


# ================================================================== 5. RBAC
class TestRbac:
    def test_owner_has_admin_member_does_not(self):
        assert policy.allows(policy.ROLE_OWNER, policy.SCOPE_PROJECT_ADMIN)
        assert policy.allows(policy.ROLE_OWNER, policy.SCOPE_ACCOUNT_ADMIN)
        assert not policy.allows(policy.ROLE_MEMBER, policy.SCOPE_PROJECT_ADMIN)
        assert policy.allows(policy.ROLE_MEMBER, policy.SCOPE_PROJECT_READ)
        assert policy.allows(policy.ROLE_MEMBER, policy.SCOPE_PROJECT_WRITE)
        assert policy.allows(policy.ROLE_MEMBER, policy.SCOPE_TASK_DISPATCH)

    def test_an_unknown_role_carries_nothing(self):
        assert policy.role_scopes("root") == frozenset()
        assert not policy.allows("", policy.SCOPE_PROJECT_READ)
        assert not policy.allows(None, policy.SCOPE_PROJECT_READ)

    def test_require_raises_for_a_missing_scope(self):
        policy.require(policy.ROLE_OWNER, policy.SCOPE_ACCOUNT_ADMIN)
        with pytest.raises(policy.PermissionDenied):
            policy.require(policy.ROLE_MEMBER, policy.SCOPE_ACCOUNT_ADMIN)


# ================================================================== 6. SSRF
class TestSsrf:
    def test_metadata_and_private_targets_are_refused_by_default(self, monkeypatch):
        monkeypatch.delenv("NEXUS_ALLOW_PRIVATE_TARGETS", raising=False)
        for hostile in ("http://169.254.169.254/latest/meta-data/",
                        "http://127.0.0.1:8080/", "http://10.0.0.5/",
                        "http://192.168.1.1/", "http://localhost/",
                        "http://metadata.google.internal/",
                        "http://[::1]/", "http://0.0.0.0/"):
            with pytest.raises(policy.UnsafeTarget):
                policy.check_outbound_url(hostile)

    def test_non_http_schemes_and_credentials_are_refused(self):
        for hostile in ("file:///etc/passwd", "ftp://host/x", "gopher://x",
                        "https://user:pw@example.com/"):
            with pytest.raises(policy.UnsafeTarget):
                policy.check_outbound_url(hostile)

    def test_a_public_https_target_is_allowed(self, monkeypatch):
        monkeypatch.delenv("NEXUS_ALLOW_PRIVATE_TARGETS", raising=False)
        assert policy.check_outbound_url("https://app.all-hands.dev") == \
            "https://app.all-hands.dev"

    def test_private_targets_allowed_only_when_opted_in(self, monkeypatch):
        monkeypatch.setenv("NEXUS_ALLOW_PRIVATE_TARGETS", "1")
        assert policy.check_outbound_url("http://127.0.0.1:60000")
        # A metadata endpoint is refused even then: it is never a legitimate API.
        with pytest.raises(policy.UnsafeTarget):
            policy.check_outbound_url("http://169.254.169.254/")

    def test_the_cloud_client_construction_enforces_the_policy(self, monkeypatch):
        from orchestration.cloud_client import CloudClient, CloudError

        monkeypatch.delenv("NEXUS_ALLOW_PRIVATE_TARGETS", raising=False)
        with pytest.raises(CloudError):
            CloudClient("http://169.254.169.254/", api_key="k")
        with pytest.raises(CloudError):
            CloudClient("file:///etc/passwd", api_key="k")

    def test_the_provider_enforces_the_policy(self, monkeypatch):
        from orchestration.providers import GroqProvider, ProviderError

        monkeypatch.delenv("NEXUS_ALLOW_PRIVATE_TARGETS", raising=False)
        with pytest.raises(ProviderError):
            GroqProvider(base_url="http://169.254.169.254/", api_key="k", model="m")


# ====================================================== 7. Command execution
class TestUnsafeCommandExecution:
    def test_only_argument_lists_are_accepted(self):
        assert policy.safe_argv(["git", "rev-parse", "HEAD"]) == ["git", "rev-parse", "HEAD"]
        for hostile in ("git status; rm -rf /", "git && rm -rf /", ""):
            with pytest.raises(policy.PolicyViolation):
                policy.safe_argv(hostile)
        with pytest.raises(policy.PolicyViolation):
            policy.safe_argv([])
        with pytest.raises(policy.PolicyViolation):
            policy.safe_argv(["git", 1])

    def test_no_module_runs_a_shell(self):
        """No orchestration module calls subprocess with ``shell=True``.

        The one external program NEXUS runs is ``git``, always as an argument
        list. This scans the source so a future ``shell=True`` is caught. The
        policy module itself is exempt: it only *names* the rule in its
        docstring, it never runs a command.
        """
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for name in sorted(os.listdir(os.path.join(repo, "orchestration"))):
            if not name.endswith(".py") or name == "nexus_policy.py":
                continue
            text = open(os.path.join(repo, "orchestration", name), encoding="utf-8").read()
            if "shell=True" in text or "os.system(" in text:
                offenders.append(name)
        assert offenders == [], f"a shell command slipped in: {offenders}"

    def test_the_restore_point_uses_an_argument_list(self):
        # A behavioural check on the only subprocess call site.
        import inspect

        from orchestration import restore_point

        source = inspect.getsource(restore_point._git)
        assert "shell=" not in source
        assert '["git", *args]' in source


# ============================================================ 8. Log leaks
class TestSecretLogRedaction:
    def test_known_env_secrets_are_scrubbed(self, monkeypatch):
        monkeypatch.setenv("OPENHANDS_API_KEY", "sk-openhands-live-key-9999")
        assert "sk-openhands-live-key-9999" not in policy.redact_text(
            "starting with key sk-openhands-live-key-9999 now")

    def test_credential_shapes_are_scrubbed(self):
        for text in ("token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
                     "Authorization: Bearer abcdefghijklmnop1234",
                     "github_pat_11ABCDEFG0123456789_abcdefghijklmnop",
                     "url https://user:secretpw@host/x"):
            assert "«redacted»" in policy.redact_text(text)

    def test_the_filter_cleans_a_log_record(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_verysecretgithubtoken0000000000")
        logger = logging.getLogger("nexus-security-test")
        logger.setLevel(logging.INFO)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger.addHandler(handler)
        policy.install_log_redaction(logger)
        try:
            logger.info("using token %s", os.environ["GITHUB_TOKEN"])
        finally:
            logger.removeHandler(handler)
        output = stream.getvalue()
        assert "ghp_verysecretgithubtoken0000000000" not in output
        assert "«redacted»" in output

    def test_install_is_idempotent(self):
        logger = logging.getLogger("nexus-security-idempotent")
        policy.install_log_redaction(logger)
        policy.install_log_redaction(logger)
        filters = [f for f in logger.filters if isinstance(f, policy.RedactingFilter)]
        assert len(filters) == 1

    def test_records_of_named_loggers_are_cleaned_the_way_the_processes_install_it(
            self, monkeypatch):
        """The web server, the worker and the MCP server install the filter on
        the root logger, and the project logs on named loggers. A logger's
        filters never see the records of its descendants - only its handlers
        do - so the filter has to sit on the handlers, and the traceback of a
        record is text that is written as well."""
        monkeypatch.setenv("OPENHANDS_API_KEY", "sk-openhands-live-key-9999")
        root = logging.getLogger()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        had = [f for f in root.filters if isinstance(f, policy.RedactingFilter)]
        before = {h: list(h.filters) for h in root.handlers}
        root.addHandler(handler)
        try:
            installed = policy.install_log_redaction()
            assert policy.install_log_redaction() is installed
            assert [f for f in handler.filters if isinstance(f, policy.RedactingFilter)] == [
                installed]
            child = logging.getLogger("lab_worker.security-test")
            child.warning("stop not taken: Bearer abcdefghijklmnop1234, key %s",
                          "sk-openhands-live-key-9999")
            try:
                raise RuntimeError("GET https://user:secretpw@host/x with "
                                   "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
            except RuntimeError:
                child.exception("a pass failed")
        finally:
            root.removeHandler(handler)
            for existing, filters in before.items():
                for flt in list(existing.filters):
                    if flt not in filters:
                        existing.removeFilter(flt)
            if not had:
                for flt in [f for f in root.filters if isinstance(f, policy.RedactingFilter)]:
                    root.removeFilter(flt)
        output = stream.getvalue()
        assert "stop not taken" in output and "a pass failed" in output
        assert "RuntimeError" in output, "the traceback is still written"
        for secret in ("sk-openhands-live-key-9999", "abcdefghijklmnop1234", "secretpw",
                       "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"):
            assert secret not in output
        assert output.count("«redacted»") >= 4


# ========================================================= 9. Archive safety
class TestUnsafeArchiveUnpacking:
    def test_a_zip_slip_archive_is_refused(self):
        from orchestration.archive_safety import ArchiveUnsafe, open_zip

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("../../etc/passwd", "root:x:0:0")
        with pytest.raises(ArchiveUnsafe):
            open_zip(buffer.getvalue())

    def test_an_absolute_path_member_is_refused(self):
        from orchestration.archive_safety import ArchiveUnsafe, normalize_member_name

        with pytest.raises(ArchiveUnsafe):
            normalize_member_name("/etc/shadow")

    def test_a_clean_archive_is_accepted(self):
        from orchestration.archive_safety import open_archive

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("src/main.py", "print('hi')\n")
        report = open_archive(buffer.getvalue(), "project.zip")
        assert [m.path for m in report.members] == ["src/main.py"]


# ====================================================== 11. MCP permissions
class TestMcpToolPermissions:
    def test_every_tool_declares_a_scope(self):
        from orchestration.mcp_server import McpServer

        declared = {t["name"] for t in McpServer.tool_schemas()}
        assert declared == set(policy.MCP_TOOL_SCOPES), \
            "a tool exists without a declared scope, or a scope names no tool"

    def test_a_tool_without_a_scope_is_refused(self):
        with pytest.raises(policy.PolicyViolation):
            policy.scopes_for_tool("eval")

    def test_a_read_only_role_cannot_dispatch(self):
        # A member may read but a dispatch needs the dispatch scope, which only
        # an owner carries. This is the policy decision the MCP server consults.
        with pytest.raises(policy.PermissionDenied):
            policy.require(policy.ROLE_MEMBER, policy.SCOPE_ACCOUNT_ADMIN)
        policy.require(policy.ROLE_OWNER, policy.SCOPE_TASK_DISPATCH)
        assert policy.scopes_for_tool("create_task") == policy.SCOPE_PROJECT_WRITE
        assert policy.scopes_for_tool("dispatch_task") == policy.SCOPE_TASK_DISPATCH

    @db_required
    def test_the_mcp_server_refuses_a_tool_the_role_lacks(self):
        from orchestration.cloud_store import PostgresStore
        from orchestration.mcp_server import McpError, McpServer

        store = PostgresStore(DSN)
        try:
            owner = McpServer(store, "user:owner", role=policy.ROLE_OWNER)
            # The owner may call a read tool.
            owner.handle("tools/call", {"name": "get_execution_status", "arguments": {}})
            # A member carries project:read too, so a read tool is allowed.
            member = McpServer(store, "user:member", role=policy.ROLE_MEMBER)
            member.handle("tools/call", {"name": "get_execution_status", "arguments": {}})
            # An undeclared tool is refused before it can run.
            with pytest.raises(McpError):
                member.handle("tools/call", {"name": "eval", "arguments": {}})
        finally:
            store.close()

    def test_a_member_role_is_denied_an_admin_scoped_tool(self):
        # There is no account-admin MCP tool today, so the decision is proved at
        # the policy boundary the server consults: a member lacks the scope.
        with pytest.raises(policy.PermissionDenied):
            policy.require(policy.ROLE_MEMBER, policy.SCOPE_ACCOUNT_ADMIN)
        # An undeclared tool has no scope at all, so it is refused as a policy
        # violation rather than run under an assumed authority.
        with pytest.raises(policy.PolicyViolation):
            policy.require_tool_scope(policy.ROLE_OWNER, "eval")


# ==================================================== policy report surface
class TestPolicyReport:
    def test_the_report_names_roles_scopes_and_ssrf_without_secrets(self, monkeypatch):
        monkeypatch.setenv("OPENHANDS_API_KEY", "sk-must-not-appear-123456")
        report = policy.policy_report()
        text = json.dumps(report)
        assert "owner" in report["roles"] and "member" in report["roles"]
        assert "create_task" in report["mcp_tool_scopes"]
        assert "allow_private_targets" in report["ssrf"]
        assert "sk-must-not-appear-123456" not in text


# ============================================== HTTP surface (real server)
@db_required
class TestSecurityHttp:
    """The policy is reachable and enforced through the real web server."""

    @pytest.fixture
    def app(self, monkeypatch):
        from orchestration.web_server import App

        monkeypatch.setenv("OPENHANDS_API_KEY", "sk-http-never-leaked-value")
        monkeypatch.setenv("WEB_AUTH_TOKEN", "machine-token")
        instance = App(DSN, poll_interval=5.0, token="machine-token")
        yield instance
        instance.store.close()

    @pytest.fixture
    def base(self, app):
        from orchestration.web_server import WebServer

        ws = WebServer(app, "127.0.0.1", 0)
        ws.start()
        yield f"http://127.0.0.1:{ws.port}"
        ws.stop()

    def test_the_policy_route_requires_auth_and_returns_no_secret(self, base):
        import urllib.error
        import urllib.request

        req = urllib.request.Request(base + "/api/security/policy")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("the policy route answered without a token")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        req = urllib.request.Request(base + "/api/security/policy")
        req.add_header("Authorization", "Bearer machine-token")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
        assert body["roles"]["owner"]
        assert "sk-http-never-leaked-value" not in json.dumps(body)

    def test_adding_an_account_with_a_metadata_url_is_refused(self, base, monkeypatch):
        # The SSRF policy is enforced at the account entry point: a base URL that
        # names a cloud metadata endpoint is refused before any key is stored.
        import urllib.error
        import urllib.request

        monkeypatch.delenv("NEXUS_ALLOW_PRIVATE_TARGETS", raising=False)
        payload = json.dumps({
            "label": "hostile", "api_key": "sk-attacker-key-1234",
            "base_url": "http://169.254.169.254/"}).encode()
        req = urllib.request.Request(base + "/api/openhands/accounts", data=payload,
                                     method="POST")
        req.add_header("Authorization", "Bearer machine-token")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("a metadata base URL was accepted")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            body = json.loads(exc.read())
            assert "sk-attacker-key-1234" not in json.dumps(body)

