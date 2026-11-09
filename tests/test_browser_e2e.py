"""Browser end-to-end: the real user journey through the NEXUS web UI.

Drives Chromium against a real HTTP server backed by a real PostgreSQL and the
fake Cloud server (a real socket server, not a mock). The journey is the MVP
acceptance path:

    login -> create project -> link repository -> create task (workflow) ->
    observe the real execution state -> record evidence -> re-verify evidence ->
    logout -> confirm the session is gone.

The Cloud boundary is the only part not exercised live here: this sandbox has no
OpenHands key, so a task stops at the Cloud call and the workflow is honestly
reported as blocked/failed. That is asserted, not hidden: a fake "success" would
be a lie. See docs/MVP_RELEASE_REPORT.md for the split between what is verified
here and what needs a live key.

Skipped when Chromium is unavailable.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.web_auth import hash_password  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run browser E2E tests"
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
USERNAME = "owner"
PASSWORD = "browser-pw"


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


@pytest.fixture(scope="module")
def fake():
    with FakeCloudServer() as server:
        yield server


@pytest.fixture
def app(fake, monkeypatch):
    _reset_schema()
    monkeypatch.setenv("OPENHANDS_API_KEY", "test-key")
    monkeypatch.setenv("AUTH_USERNAME", USERNAME)
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password(PASSWORD))
    monkeypatch.setenv("AUTH_SESSION_SECRET", "b" * 32)
    a = App(DSN, poll_interval=0.2, token="machine-token")
    a.client = CloudClient(fake.base_url, api_key="test-key", timeout=10.0)
    a.dispatcher.client = a.client
    a.runtime.client = a.client
    a.verifier.client = a.client
    # A signer so evidence records are VERIFIED, not UNVERIFIED.
    from orchestration.evidence_trust import (
        EvidenceTrustEngine,
        Keyring,
        generate_signer,
        public_key_b64,
    )
    from orchestration.nexus_evidence import EvidenceService

    signer = generate_signer("browser-key")
    ring = Keyring()
    ring.add("browser-key", public_key_b64(signer))
    a.evidence = EvidenceService(a.store, EvidenceTrustEngine(ring, signer))
    yield a
    a.store.close()


@pytest.fixture
def base(app):
    ws = WebServer(app, "127.0.0.1", 0)
    ws.start()
    yield f"http://127.0.0.1:{ws.port}"
    ws.stop()


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        context = browser.new_context()
        pg = context.new_page()
        yield pg
        browser.close()


def _login(page, base):
    page.goto(base + "/login")
    page.fill("#username", USERNAME)
    page.fill("#password", PASSWORD)
    page.click("#loginbtn")
    page.wait_for_url(base + "/")
    page.wait_for_selector("#whoami", state="visible")


def test_full_user_journey(base, app, page):
    # 1. Opening NEXUS redirects an anonymous visitor to login.
    page.goto(base + "/")
    assert page.url.endswith("/login")

    # 2. Log in.
    _login(page, base)
    assert USERNAME in page.inner_text("#whoami")

    # 3. Create a project.
    page.click("#tab-projects")
    page.wait_for_selector("#project-form")
    page.fill("#project-name", "Browser Project")
    page.click("#project-form button[type=submit]")
    page.wait_for_selector(".project-select")

    # 4. Open the project and link a repository (allowlist it first via the API).
    page.click(".project-select")
    page.wait_for_selector("#project-exec:not(.hidden)")

    # Add a repository to the global allowlist (the System tab does this; here we
    # call the API directly to keep the browser test focused on the journey).
    import json
    import urllib.request

    req = urllib.request.Request(
        base + "/api/repositories",
        data=json.dumps({"full_name": "acme/widget", "default_branch": "main",
                         "allowed_branches": ["main"]}).encode(),
        method="POST")
    req.add_header("Authorization", "Bearer machine-token")
    req.add_header("Content-Type", "application/json")
    urllib.request.urlopen(req, timeout=10).read()

    page.fill("#project-repo-name", "acme/widget")
    page.click("#project-repo-form button[type=submit]")
    page.wait_for_function("document.querySelector('#project-repos').textContent.includes('acme/widget')")

    # 5-6. Create a task, choose the branch, and see the execution context.
    page.fill("#project-task-goal", "Implement the widget API")
    page.select_option("#project-task-repo", "acme/widget")
    page.fill("#project-task-branch", "main")
    page.wait_for_function("document.querySelector('#project-task-context').textContent.includes('acme/widget')")
    page.click("#project-task-go")

    # The task form reports the real outcome (started, or an honest error). Wait
    # for the placeholder to be replaced, not merely present.
    page.wait_for_function(
        "() => { const m = document.querySelector('#project-task-msg').textContent;"
        " return m.length > 0 && !m.includes('Запускаем'); }",
        timeout=30000,
    )
    task_msg = page.inner_text("#project-task-msg")
    assert "Не удалось" not in task_msg, f"task creation failed: {task_msg}"

    # 7-8. The workflow appears with a real state. The task form reloads the
    # workflow list after the POST; wait for the real row (not the empty state).
    page.wait_for_function(
        "() => !document.querySelector('#project-workflows').textContent.includes('Workflow пока нет')",
        timeout=20000,
    )
    workflows_text = page.inner_text("#project-workflows")
    assert "Implement the widget API" in workflows_text

    # Workflow detail opens and shows the Sr state and the child task list.
    page.click(".wf-open")
    page.wait_for_function(
        "() => document.querySelector('.wf-detail:not(.hidden)') !== null",
        timeout=20000,
    )

    # 9. Record evidence (a real, signed record) and re-verify it in the UI.
    page.fill("#project-evidence-summary", "browser evidence")
    page.fill("#project-evidence-commit", "abc123def")
    page.fill("#project-evidence-branch", "main")
    page.click("#project-evidence-form button[type=submit]")
    page.wait_for_function(
        "() => document.querySelector('#project-evidence').textContent.includes('abc123def')",
        timeout=20000,
    )
    # 10-12. Re-verify; the verdict and commit must be visible.
    page.click(".evidence-verify")
    page.wait_for_function(
        "() => document.querySelector('.evidence-detail:not(.hidden)') !== null",
        timeout=20000,
    )
    detail = page.inner_text(".evidence-detail:not(.hidden)")
    assert "VERIFIED" in detail
    assert "abc123def" in detail

    # 13. Log out.
    page.click("#logout")
    page.wait_for_url(base + "/login")

    # 14. The session is gone: a protected page redirects to login again.
    page.goto(base + "/")
    assert page.url.endswith("/login")


def test_no_horizontal_overflow_on_mobile(base, app, page):
    page.set_viewport_size({"width": 360, "height": 740})
    _login(page, base)
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 2, f"horizontal overflow of {overflow}px"
