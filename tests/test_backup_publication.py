"""Exercise nexus.sh backup on a private filesystem; no DB or host credentials needed."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "nexus.sh"
STAMP = "20260102T030405Z"
BASENAME = f"nexus-{STAMP}.dump"


@pytest.fixture
def operator(tmp_path):
    script = tmp_path / "scripts" / SCRIPT.name
    script.parent.mkdir()
    shutil.copy2(SCRIPT, script)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    backups = tmp_path / "local backups"
    backups.mkdir()
    env_file = tmp_path / "operator.env"
    env_file.write_text("SUPABASE_DB_URL=dbname=unused\n")

    def shim(name, body):
        command = bin_dir / name
        command.write_text("#!/bin/sh\nset -eu\n" + body)
        command.chmod(0o755)

    shim("date", f"echo {STAMP}\n")
    shim("pg_dump", '''while [ "$1" != -f ]; do shift; done
printf '%s' "$PAYLOAD" > "$2"
if [ "$FAIL_AT" = pg_dump ]; then exit 7; fi
''')
    # Refuse the last rename (after the checksum has been published).
    shim("mv", '''case "$FAIL_AT:$*" in publish:*.dump) exit 9;; esac
exec /usr/bin/mv "$@"
''')
    shim("sha256sum", '''if [ -n "$PROBE" ]; then
    find "$NEXUS_BACKUP_DIR" -maxdepth 1 -name 'nexus-*.dump' -printf '%f\\n' > "$PROBE"
fi
if [ "$FAIL_AT" = checksum ]; then printf 'partial digest'; exit 8; fi
exec /usr/bin/sha256sum "$@"
''')

    def backup(payload="snapshot", *, fail_at="", probe=""):
        env = {"PATH": f"{bin_dir}:{os.defpath}", "HOME": str(tmp_path),
               "NEXUS_ENV_FILE": str(env_file), "NEXUS_BACKUP_DIR": str(backups),
               "PAYLOAD": payload, "FAIL_AT": fail_at, "PROBE": str(probe)}
        return subprocess.run([script, "backup"], env=env, cwd=tmp_path,
                              capture_output=True, text=True, timeout=15)

    return backups, backup


def test_overlapping_backups_in_one_second_keep_each_payload_and_manifest(operator):
    backups, backup = operator
    payloads = [f"snapshot-{i}" for i in range(4)]
    with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        for result in pool.map(backup, payloads):
            assert result.returncode == 0, result.stderr
    dumps = list(backups.glob("nexus-*.dump"))
    assert sorted(dump.read_text() for dump in dumps) == payloads
    for dump in dumps:
        sidecar = backups / (dump.name + ".sha256")
        subprocess.run(["sha256sum", "--check", sidecar.name], cwd=backups, check=True)
        assert dump.stat().st_mode & 0o077 == 0
        assert sidecar.stat().st_mode & 0o077 == 0


def test_new_dump_is_not_visible_while_its_checksum_is_being_written(operator, tmp_path):
    backups, backup = operator
    seen = tmp_path / "visible-to-reader"
    result = backup(probe=seen)
    assert result.returncode == 0, result.stderr
    assert seen.read_text() == ""
    assert (backups / BASENAME).is_file()
    assert (backups / (BASENAME + ".sha256")).is_file()


@pytest.mark.parametrize("fail_at", ["pg_dump", "checksum", "publish"])
def test_failed_backup_preserves_the_previous_pair_and_publishes_nothing(operator, fail_at):
    backups, backup = operator
    first = backup("last good copy")
    assert first.returncode == 0, first.stderr
    before = {p.name: p.read_bytes() for p in backups.iterdir()}
    failed = backup("incomplete replacement", fail_at=fail_at)
    assert failed.returncode != 0
    assert {p.name: p.read_bytes() for p in backups.iterdir()} == before


def test_an_existing_unpaired_sidecar_is_not_overwritten(operator):
    backups, backup = operator
    sidecar = backups / (BASENAME + ".sha256")
    sidecar.write_text("reserved")
    result = backup()
    assert result.returncode == 0, result.stderr
    assert not (backups / BASENAME).exists()
    assert sidecar.read_text() == "reserved"
    new, = backups.glob("nexus-*.dump")
    subprocess.run(["sha256sum", "--check", new.name + ".sha256"], cwd=backups, check=True)


def test_retention_rotates_only_complete_named_pairs_and_keeps_the_new_dump(operator):
    backups, backup = operator
    prior = []
    for i in range(14):
        dump = backups / f"nexus-20250101T0000{i:02d}Z.dump"
        data = f"copy-{i}".encode()
        dump.write_bytes(data)
        (backups / (dump.name + ".sha256")).write_text(f"{hashlib.sha256(data).hexdigest()}  {dump.name}\n")
        # Even an old archive with a future mtime must not evict today's result.
        os.utime(dump, (2100000000 + i, 2100000000 + i))
        prior.append(dump.name)
    untouched = ["nexus-monthly.dump", "nexus-monthly.dump.sha256", "nexus-20240101T000000Z.dump"]
    for name in untouched:
        (backups / name).write_text("keep")
    result = backup()
    assert result.returncode == 0, result.stderr
    retained = [BASENAME, *prior[1:]]
    expected = set(untouched) | set(retained) | {name + ".sha256" for name in retained}
    assert {p.name for p in backups.iterdir()} == expected
    assert all((backups / name).read_text() == "keep" for name in untouched)
