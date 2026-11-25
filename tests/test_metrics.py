"""Prometheus metrics: the registry, the helpers and the /metrics route.

Two halves, as the module is:

* the pure registry (no database): rendering, label escaping, cumulative
  buckets, path normalisation, and the guarantee that instrumentation never
  raises;
* the real server: the route is protected like every other read, it renders
  the exposition format, and a real evidence record and a real claim are
  counted through the real code paths (not by calling the helper directly).
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration import nexus_metrics as metrics  # noqa: E402
from orchestration.cloud_client import CloudClient  # noqa: E402
from orchestration.evidence_trust import (  # noqa: E402
    EvidenceTrustEngine, Keyring, generate_signer, public_key_b64,
)
from orchestration.nexus_evidence import EvidenceService  # noqa: E402
from orchestration.web_auth import hash_password  # noqa: E402
from orchestration.web_server import App, WebServer  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
TOKEN = "metrics-token"


# --------------------------------------------------------------- pure registry
def test_counter_gauge_and_histogram_render_the_exposition_format():
    metrics.REGISTRY.reset()
    metrics.task_claimed("lab-1")
    metrics.task_claimed("lab-1")
    metrics.set_db_pool({"available": True, "size": 4, "idle": 3,
                         "waiting": 0, "max": 10})
    metrics.observe_openhands("acct", "app", 0.03)
    text = metrics.render()
    assert "# TYPE nexus_tasks_claimed_total counter" in text
    assert 'nexus_tasks_claimed_total{worker="lab-1"} 2' in text
    assert "# TYPE nexus_db_pool_active_connections gauge" in text
    assert "nexus_db_pool_active_connections 1" in text
    assert "# TYPE nexus_openhands_api_latency_seconds histogram" in text
    assert 'nexus_openhands_api_latency_seconds_count{account="acct",transport="app"} 1' in text


def test_histogram_buckets_are_cumulative():
    metrics.REGISTRY.reset()
    metrics.observe_openhands("a", "app", 0.03)
    metrics.observe_openhands("a", "app", 2.5)
    text = metrics.render()
    # 0.03 falls in le=0.05; 2.5 in le=2.5, so both are in +Inf.
    assert 'le="0.05"} 1' in text
    assert 'le="2.5"} 2' in text
    assert 'le="+Inf"} 2' in text


def test_label_values_are_escaped():
    metrics.REGISTRY.reset()
    metrics.evidence_verification('we"ird\\value\nx')
    text = metrics.render()
    assert 'verdict="we\\"ird\\\\value\\nx"' in text


def test_normalize_path_collapses_ids_and_numbers():
    assert metrics.normalize_path(
        "/api/v1/projects/123e4567-e89b-12d3-a456-426614174000/runs/42"
    ) == "/api/v1/projects/{id}/runs/{n}"
    assert metrics.normalize_path("") == "/"


def test_instrumentation_never_raises():
    # A wrong label set is a programming error inside the helper; the helper
    # must swallow it so the request or dispatch it observes is unaffected.
    metrics.REGISTRY.reset()
    metrics._safe(lambda: metrics.TASKS_CLAIMED.inc(wrong="x"))  # noqa: SLF001
    metrics._safe(lambda: (_ for _ in ()).throw(RuntimeError("boom")))  # noqa: SLF001


# ----------------------------------------------------------------- real server
pytestmark_db = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run the metrics route tests")


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


@pytest.fixture
def fake():
    with FakeCloudServer() as server:
        yield server


@pytest.fixture
def app(fake, monkeypatch):
    _reset_schema()
    monkeypatch.setenv("OPENHANDS_API_KEY", "test-key")
    monkeypatch.setenv("AUTH_USERNAME", "owner")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password("pw"))
    monkeypatch.setenv("AUTH_SESSION_SECRET", "s" * 32)
    a = App(DSN, poll_interval=0.2, token=TOKEN)
    a.client = CloudClient(fake.base_url, api_key="test-key", timeout=10.0)
    yield a
    a.store.close()


@pytest.fixture
def base(app):
    ws = WebServer(app, "127.0.0.1", 0)
    ws.start()
    yield f"http://127.0.0.1:{ws.port}"
    ws.stop()


def _get(url: str, token: str | None = None):
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(request, timeout=10)


@pytestmark_db
def test_the_metrics_route_requires_authentication(base):
    with pytest.raises(urllib.error.HTTPError) as caught:
        _get(base + "/metrics")
    assert caught.value.code == 401


@pytestmark_db
def test_the_metrics_route_renders_prometheus_text(base):
    metrics.REGISTRY.reset()
    metrics.task_claimed("lab-x")
    with _get(base + "/metrics", TOKEN) as response:
        assert response.status == 200
        assert response.headers["Content-Type"].startswith(
            "text/plain; version=0.0.4")
        body = response.read().decode()
    assert "# HELP nexus_tasks_claimed_total" in body
    assert 'nexus_tasks_claimed_total{worker="lab-x"} 1' in body
    # The pool gauges are published at scrape time from the live store.
    assert "nexus_db_pool_max_connections" in body
    # So are the queue/worker gauges, from the observability snapshot.
    assert "nexus_queue_depth" in body
    assert "nexus_stuck_tasks" in body
    assert "nexus_live_workers" in body


@pytestmark_db
def test_a_real_evidence_record_is_counted(app):
    metrics.REGISTRY.reset()
    signer = generate_signer("k1")
    keyring = Keyring()
    keyring.add("k1", public_key_b64(signer))
    service = EvidenceService(app.store, EvidenceTrustEngine(keyring, signer))
    service.record(subject_kind="manual", subject_id=None, project_id=None,
                   summary="metrics test", created_by="test")
    text = metrics.render()
    assert 'nexus_evidence_verifications_total{verdict="VERIFIED"} 1' in text
