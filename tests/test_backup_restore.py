"""Backup/restore drill: a real pg_dump -> pg_restore into a fresh database.

This is the automated proxy for the operator procedure in DEPLOY.md. It proves
that a dump of a populated NEXUS database restores into an empty one with the
schema guard satisfied and the data intact — so a restore is not a leap of
faith.

It needs the PostgreSQL client tools (`pg_dump`, `pg_restore`, `createdb`) and a
disposable server, both available in CI. It is skipped when they are absent or
when ``TEST_SUPABASE_DB_URL`` is unset.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from cryptography.fernet import Fernet, InvalidToken
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.cloud_store import NotFound, PostgresStore  # noqa: E402
from orchestration.evidence_trust import (  # noqa: E402
    VERIFIED, EvidenceTrustEngine, Keyring, generate_signer, public_key_b64,
)
from orchestration.nexus_conversations import ConversationService  # noqa: E402
from orchestration.nexus_evidence import EvidenceService  # noqa: E402
from orchestration.nexus_files import FileService  # noqa: E402
from orchestration.nexus_lab_store import LabStore  # noqa: E402
from orchestration.nexus_projects import ProjectService  # noqa: E402
from orchestration.nexus_openhands_accounts import OpenHandsAccountService  # noqa: E402
from tests.fake_cloud import FakeCloudServer  # noqa: E402

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

_HAVE_TOOLS = all(shutil.which(t) for t in ("pg_dump", "pg_restore", "createdb", "dropdb"))
pytestmark = pytest.mark.skipif(
    not DSN or not _HAVE_TOOLS,
    reason="set TEST_SUPABASE_DB_URL and install the PostgreSQL client tools",
)

_MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def _conninfo(dsn: str) -> dict[str, str]:
    """The connection parameters of *dsn*, parsed by psycopg itself.

    The DSN is **not** necessarily a URL: ``tests/_test_db.py`` rewrites the
    session DSN with ``psycopg.conninfo.make_conninfo``, which produces a
    ``key=value`` conninfo string. Parsing that with ``urllib.parse.urlsplit``
    yields ``hostname=None, port=None``, and a fallback to port 5432 then sends
    ``createdb``/``pg_dump`` to a *different* server than the one the suite runs
    on - which is how this test once hung on a password prompt. psycopg's own
    parser understands both a URL and a conninfo string, so it is the only
    correct reader here, and there is no port fallback: the port the DSN names
    is the port used.
    """
    from psycopg.conninfo import conninfo_to_dict

    try:
        params = conninfo_to_dict(dsn)
    except Exception as e:  # noqa: BLE001 - a bad DSN is a clear test failure
        raise AssertionError(f"the test DSN is not a usable PostgreSQL DSN: {e}") from e
    if not params.get("port"):
        raise AssertionError(
            "the test DSN does not name a port; refusing to guess one (a wrong "
            "port would reach another server). Set TEST_SUPABASE_DB_URL with an "
            "explicit port.")
    return params


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(_MIG)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(_MIG, name)).read())


def _swap_db(dsn: str, dbname: str) -> str:
    """The same connection as *dsn* but to *dbname*, as a conninfo string.

    Built from the parsed parameters, so a URL or a conninfo DSN both come out
    as one canonical form and the port is preserved.
    """
    from psycopg.conninfo import make_conninfo

    params = dict(_conninfo(dsn))
    params["dbname"] = dbname
    return make_conninfo(**params)


def _run(*cmd: str, env_extra: dict | None = None) -> None:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise AssertionError(
            f"{cmd[0]} failed ({result.returncode}): {result.stderr[:400]}"
        )


def test_dump_and_restore_into_a_fresh_database(tmp_path, monkeypatch):
    _reset_schema()
    source = PostgresStore(DSN)
    restore_db = "nexus_restore_" + uuid.uuid4().hex[:8]
    dump_path = str(tmp_path / "drill.dump")
    # The connection parameters are read once, by psycopg, so the tools reach
    # the same host and port the suite itself uses - never a guessed 5432.
    params = _conninfo(DSN)
    host = params.get("host") or "127.0.0.1"
    port = params["port"]
    user = params.get("user") or "postgres"
    env = {"PGPASSWORD": params.get("password", "")}
    try:
        # Populate the source with a project, a repository link and a task, so
        # the dump has real NEXUS data to carry.
        source.add_repository("acme/repo", allowed_branches=["main"])
        owner = "user:drill"
        projects = ProjectService(source)
        pid = str(projects.create(owner, "Drill")["id"])
        projects.set_quality_gate(owner, pid, "on")
        with source._tx() as cur:
            cur.execute("INSERT INTO nexus_project_repositories(project_id, repository_id) "
                        "SELECT %s, id FROM repositories WHERE full_name='acme/repo'", (pid,))
        task = source.submit_task("drill work", project_id=pid,
                                  selected_repository="acme/repo", selected_branch="main")

        # The account/key exist only for this drill. Store one real Fernet
        # ciphertext via the account service against a loopback provider.
        # Neither the plaintext nor the wrapping key needs to enter pg_dump.
        wrapping_key = Fernet.generate_key().decode()
        secret = secrets.token_urlsafe(32)
        digest = hashlib.sha256(secret.encode()).digest()
        with monkeypatch.context() as mp, FakeCloudServer() as cloud:
            mp.setenv("NEXUS_SECRET_KEY", wrapping_key)
            accounts = OpenHandsAccountService(source)
            account = accounts.add(owner, label="backup drill", api_key=secret,
                                   base_url=cloud.base_url, transport="app")
            accounts.bind_project(owner, pid, account['id'])
        del secret
        with source._tx() as cur:
            cur.execute("SELECT api_key_encrypted FROM nexus_openhands_accounts WHERE id=%s", (account['id'],))
            cipher_digest = hashlib.sha256(cur.fetchone()['api_key_encrypted']).digest()
        assert cipher_digest != digest

        # A dump taken in the middle of a run must neither lose the claim nor
        # make its dependent runnable. Keep both IDs and the fencing token.
        chat = ConversationService(source)
        cid = chat.create(owner, pid, title="Restore")['id']
        chat.add_message(owner, pid, cid, role="user", content="Сохранить историю")
        messages = chat.list_messages(owner, pid, cid)
        goal, _ = projects.create_goal(owner, pid, "Restore with a live lease")
        lab = LabStore(source)
        run = lab.start_run(owner, pid, goal['id'], policy="durable", conversation_id=cid,
                            plan=[{"id": "a", "title": "A"},
                                  {"id": "b", "title": "B", "dependencies": ["a"]}])
        claimed = lab.claim_task(run['id'], "pre-backup-worker")
        assert claimed['task_key'] == 'a'
        tasks = lab.list_tasks_internal(run['id'])
        attachment = b"\x00\xffNEXUS\n"
        file = FileService(source).upload(owner, pid, "payload.bin", attachment)
        # The verification key is held separately from the dump. Verify the
        # restored records with the public key alone; never sign a replacement.
        signer = generate_signer("restore-drill")
        keys = Keyring()
        keys.add(signer.key_id, public_key_b64(signer))
        evidence = EvidenceService(source, EvidenceTrustEngine(keys, signer))
        records = [evidence.record(subject_kind="project", subject_id=pid, project_id=pid,
                                   summary=summary) for summary in ("before claim", "after claim")]
        assert records[1]['prev_hash'] == records[0]['entry_hash']

        # Dump the whole database in custom format.
        _run("pg_dump", "--format=custom", "--file", dump_path,
             "--no-owner", "--no-privileges", DSN, env_extra=env)

        # Create an empty database and restore into it, on the same server.
        _run("createdb", "-h", host, "-p", port, "-U", user,
             restore_db, env_extra=env)
        try:
            _run("pg_restore", "--dbname", _swap_db(DSN, restore_db),
                 "--no-owner", "--no-privileges", "--exit-on-error",
                 dump_path, env_extra=env)
            # Do not allow startup to repair a broken/incomplete backup.
            restored = PostgresStore(_swap_db(DSN, restore_db), auto_migrate=False)
            try:
                # The schema guard is satisfied and the data survived.
                assert restored.verify_schema() == []
                assert restored.get_task(task.id).goal == "drill work"
                assert restored.project_repository_linked(pid, "acme/repo")
                assert ProjectService(restored).get(owner, pid)['quality_gate'] == 'on'
                assert ConversationService(restored).list_messages(owner, pid, cid) == messages
                assert LabStore(restored).list_tasks_internal(run['id']) == tasks
                assert LabStore(restored).claim_task(run['id'], "after-restore") is None
                files = FileService(restored)
                assert files.download(owner, pid, file['id'])['content'] == attachment
                with pytest.raises(NotFound):
                    files.download("user:outsider", pid, file['id'])
                # The provider is already stopped. A digest sink verifies the
                # bound client's recovered key, without printing or using it.
                restored_accounts = OpenHandsAccountService(
                    restored, client_factory=lambda _, key: hashlib.sha256(key.encode()).digest())
                with restored._tx() as cur:
                    cur.execute("SELECT api_key_encrypted FROM nexus_openhands_accounts WHERE id=%s", (account['id'],))
                    assert hashlib.sha256(cur.fetchone()['api_key_encrypted']).digest() == cipher_digest
                with monkeypatch.context() as mp:
                    mp.setenv("NEXUS_SECRET_KEY", Fernet.generate_key().decode())
                    with pytest.raises(InvalidToken):
                        restored_accounts.client_for_project(owner, pid)
                    mp.setenv("NEXUS_SECRET_KEY", wrapping_key)
                    assert secrets.compare_digest(restored_accounts.client_for_project(owner, pid), digest)
                    with pytest.raises(NotFound):
                        restored_accounts.client_for_project("user:outsider", pid)
                verifier = EvidenceService(restored, EvidenceTrustEngine(keys))
                for record in records:
                    checked = verifier.reverify(record['id'], project_id=pid)
                    assert (checked['signature'], checked['entry_hash'], checked['verdict']) == (
                        record['signature'], record['entry_hash'], VERIFIED)
                assert verifier.index_integrity(project_id=pid)['intact']
                with restored._tx() as cur:
                    cur.execute("SELECT filename, checksum FROM schema_migrations")
                    recorded = {r["filename"]: r["checksum"] for r in cur.fetchall()}
                assert recorded == {f.name: hashlib.sha256(f.read_text(encoding="utf-8").encode()).hexdigest()
                                    for f in Path(_MIG).glob("*.sql")}
            finally:
                restored.close()
        finally:
            _run("dropdb", "--if-exists", "-h", host, "-p", port, "-U", user,
                 restore_db, env_extra=env)
    finally:
        source.close()
        if os.path.exists(dump_path):
            os.unlink(dump_path)


def test_secret_tables_are_rls_protected():
    """conversation_routes/cloud_sandboxes hold a session key; they must be locked.

    RLS enabled with no client policy, and no PUBLIC grant, so a Supabase
    anon/authenticated role cannot read a sandbox session key through PostgREST.
    """
    _reset_schema()
    store = PostgresStore(DSN)
    try:
        with store._tx() as cur:
            for table in ("conversation_routes", "cloud_sandboxes"):
                cur.execute("SELECT relrowsecurity FROM pg_class WHERE oid=%s::regclass",
                            (table,))
                assert cur.fetchone()["relrowsecurity"], table
                cur.execute(
                    "SELECT count(*) AS n FROM pg_class c, aclexplode(c.relacl) a "
                    "WHERE c.oid=%s::regclass AND a.grantee=0", (table,))
                assert cur.fetchone()["n"] == 0, table
    finally:
        store.close()
