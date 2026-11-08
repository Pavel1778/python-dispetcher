"""Project uploads: real database, HTTP boundaries, and concurrent quotas."""
import http.client
import os
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlsplit

import pytest

from orchestration.cloud_store import NotFound, PostgresStore
from orchestration.nexus_files import FileService
from orchestration.nexus_projects import ProjectService

pytest_plugins = ["tests.test_nexus_projects"]
DSN = os.environ.get("TEST_SUPABASE_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable PostgreSQL")


def req(base, path, method="GET", data=None, session=None, csrf=True, name=None, bearer=None):
    url = urlsplit(base)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
    headers = {}
    if session:
        headers["Cookie"] = "dispatcher_session=" + session.token
        if csrf:
            headers["X-CSRF-Token"] = session.csrf_token
    if name is not None:
        headers["X-File-Name"] = quote(name, safe="")
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    conn.request(method, path, body=data, headers=headers)
    response = conn.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    conn.close()
    return result


def test_files_are_opaque_scoped_and_owner_deletable(http_server):
    base, alice, bob = http_server
    import json
    status, _, body = req(base, "/api/v1/projects", "POST", b'{"name":"test"}', alice)
    assert status == 201
    project = json.loads(body)["project"]["id"]
    path = f"/api/v1/projects/{project}/files"
    archive = b"PK\x03\x04\x00some opaque bytes"
    assert req(base, path, "POST", archive, alice, csrf=False, name="a.zip")[0] == 403
    assert req(base, path, "POST", archive, bob, name="a.zip")[0] == 404
    assert req(base, path, "POST", archive, alice, name="../bad.zip")[0] == 400
    status, _, body = req(base, path, "POST", archive, alice, name="данные.zip")
    assert status == 201
    metadata = json.loads(body)["file"]
    assert metadata["name"] == "данные.zip" and metadata["size"] == len(archive)
    file_path = path + "/" + metadata["id"]
    assert req(base, file_path, session=bob)[0] == 404
    assert req(base, file_path)[0] == 401
    status, headers, content = req(base, file_path, session=alice)
    assert status == 200 and content == archive
    assert headers["Content-Type"] == "application/octet-stream"
    assert headers["Content-Disposition"].startswith("attachment;")
    assert headers["Cache-Control"] == "no-store"
    assert req(base, file_path + "/delete", "POST", b"", bob)[0] == 404
    assert req(base, file_path + "/delete", "POST", b"", alice, csrf=False)[0] == 403
    assert req(base, file_path + "/delete", "POST", b"", alice)[0] == 200
    assert req(base, file_path, session=alice)[0] == 404


def test_project_quotas_serialize_across_connections(store, monkeypatch):
    import orchestration.nexus_files as files
    monkeypatch.setattr(files, "MAX_PROJECT_BYTES", 10)
    project = str(ProjectService(store).create("user:alice", "quota")["id"])

    def upload(i):
        other = PostgresStore(DSN, auto_migrate=False)
        try:
            try:
                FileService(other).upload("user:alice", project, str(i), b"123456")
                return True
            except ValueError:
                return False
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(upload, range(2))) == 1
    assert len(FileService(store).list("user:alice", project)) == 1
    with pytest.raises(NotFound):
        FileService(store).list("user:bob", project)


def test_file_table_is_rls_protected(store):
    with store._tx() as cur:
        cur.execute("SELECT relrowsecurity FROM pg_class WHERE oid='nexus_files'::regclass")
        assert cur.fetchone()["relrowsecurity"]
        cur.execute("SELECT count(*) AS n FROM pg_class c, aclexplode(c.relacl) a "
                    "WHERE c.oid='nexus_files'::regclass AND a.grantee=0")
        assert cur.fetchone()["n"] == 0


def test_upload_validation_and_membership(store):
    from orchestration.nexus_files import MAX_FILE_SIZE

    project = str(ProjectService(store).create("user:alice", "limits")["id"])
    service = FileService(store)
    for name in ("", ".", "..", "a/b", "a\\b", "bad\nname", "bad\u202ename", "x" * 256):
        with pytest.raises(ValueError):
            service.upload("user:alice", project, name, b"")
    with pytest.raises(ValueError):
        service.upload("user:alice", project, "large.zip", b"0" * (MAX_FILE_SIZE + 1))
    empty = service.upload("user:alice", project, "empty.bin", b"")
    with pytest.raises(NotFound):
        service.download("user:bob", project, str(empty["id"]))
    assert bytes(service.download("user:alice", project, str(empty["id"]))["content"]) == b""
