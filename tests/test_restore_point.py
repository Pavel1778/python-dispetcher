"""Safe restore points: a real git repository, a real bundle, a real restore.

No database, no network, no mocks. Each test builds a throwaway repository under
``tmp_path`` and drives the module (and, where noted, the CLI) against it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestration import cli  # noqa: E402
from orchestration.restore_point import (  # noqa: E402
    RestorePointError,
    create_restore_point,
    list_restore_points,
    repo_root_of,
    restore_restore_point,
    sha256_file,
    verify_restore_point,
)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "work"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("one\n", encoding="utf-8")
    git(r, "add", "a.txt")
    git(r, "commit", "-q", "-m", "first")
    (r / "b.txt").write_text("two\n", encoding="utf-8")
    git(r, "add", "b.txt")
    git(r, "commit", "-q", "-m", "second")
    return r


def test_create_records_head_branch_and_bundle(repo: Path) -> None:
    head = git(repo, "rev-parse", "HEAD").strip()
    record = create_restore_point(repo)

    assert record.head == head
    assert record.branch == "main"
    assert record.dirty is False
    assert record.bundle_path.exists()
    assert record.bundle_size > 0
    assert record.bundle_sha256 == sha256_file(record.bundle_path)
    assert record.metadata.endswith(".json")
    # every ref is in the bundle, not just the checked-out one
    assert "refs/heads/main" in record.refs


def test_create_is_non_destructive(repo: Path) -> None:
    """Creating a restore point must not touch the working tree or HEAD."""
    before_head = git(repo, "rev-parse", "HEAD").strip()
    before_status = git(repo, "status", "--porcelain")
    before_branches = git(repo, "branch", "--format=%(refname)")

    create_restore_point(repo)

    assert git(repo, "rev-parse", "HEAD").strip() == before_head
    assert git(repo, "branch", "--format=%(refname)") == before_branches
    # The only new status entry is the untracked output directory itself; no
    # tracked file is modified. (In the real repository that directory is
    # gitignored, so even that entry does not appear.)
    tracked = [line for line in git(repo, "status", "--porcelain").splitlines()
               if ".restore-points" not in line]
    assert tracked == [line for line in before_status.splitlines()
                       if ".restore-points" not in line]


def test_restore_brings_back_history(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD").strip()
    record = create_restore_point(repo)

    dest = tmp_path / "restored"
    restore_restore_point(record.metadata, dest)

    assert git(dest, "rev-parse", "HEAD").strip() == head
    assert (dest / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert (dest / "b.txt").read_text(encoding="utf-8") == "two\n"


def test_dirty_tree_is_recorded_with_patch(repo: Path) -> None:
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("new\n", encoding="utf-8")

    record = create_restore_point(repo)

    assert record.dirty is True
    assert "untracked.txt" in record.untracked
    assert record.patch_path is not None and record.patch_path.exists()
    assert "changed" in record.patch_path.read_text(encoding="utf-8")


def test_verify_accepts_a_good_point(repo: Path) -> None:
    record = create_restore_point(repo)
    report = verify_restore_point(record.metadata, repo)
    assert report["status"] == "VERIFIED"
    assert report["ok"] is True


def test_verify_rejects_a_tampered_bundle(repo: Path) -> None:
    record = create_restore_point(repo)
    # flip one byte in the bundle
    data = bytearray(record.bundle_path.read_bytes())
    data[-1] ^= 0xFF
    record.bundle_path.write_bytes(bytes(data))

    report = verify_restore_point(record.metadata, repo)
    assert report["status"] == "FAILED"
    assert any("sha256" in r for r in report["reasons"])


def test_verify_reports_a_missing_bundle(repo: Path) -> None:
    record = create_restore_point(repo)
    record.bundle_path.unlink()
    report = verify_restore_point(record.metadata, repo)
    assert report["status"] == "FAILED"
    assert any("bundle" in r for r in report["reasons"])


def test_list_orders_newest_first(repo: Path) -> None:
    import time

    first = create_restore_point(repo, label="first")
    time.sleep(1.1)  # the stamp has one-second resolution
    second = create_restore_point(repo, label="second")

    points = list_restore_points(repo / ".restore-points")
    assert [p.label for p in points[:2]] == ["second", "first"]
    assert {first.head, second.head} == {p.head for p in points}


def test_restore_refuses_an_existing_destination(repo: Path, tmp_path: Path) -> None:
    record = create_restore_point(repo)
    dest = tmp_path / "exists"
    dest.mkdir()
    with pytest.raises(RestorePointError):
        restore_restore_point(record.metadata, dest)


def test_outside_a_repository_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RestorePointError):
        repo_root_of(tmp_path)


def test_custom_output_directory(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "elsewhere"
    record = create_restore_point(repo, out)
    assert Path(record.metadata).parent == out
    assert list_restore_points(out) and len(list_restore_points(out)) == 1


# ----------------------------------------------------------------------- CLI
def _run_cli(argv: list[str], cwd: Path, capsys) -> tuple[int, str]:
    code = cli.main(argv)
    return code, capsys.readouterr().out


def test_cli_create_list_verify(repo: Path, capsys) -> None:
    code, out = _run_cli(["restore-point", "create", "--repo", str(repo), "--json"],
                         repo, capsys)
    assert code == 0
    record = json.loads(out)
    assert record["head"] == git(repo, "rev-parse", "HEAD").strip()
    meta = record["metadata"]

    code, out = _run_cli(["restore-point", "list", "--repo", str(repo), "--json"],
                         repo, capsys)
    assert code == 0
    assert json.loads(out)["count"] == 1

    code, out = _run_cli(["restore-point", "verify", meta, "--repo", str(repo), "--json"],
                         repo, capsys)
    assert code == 0
    assert json.loads(out)["status"] == "VERIFIED"


def test_cli_verify_fails_on_a_bad_point(repo: Path, capsys) -> None:
    code, out = _run_cli(["restore-point", "create", "--repo", str(repo), "--json"],
                         repo, capsys)
    meta = Path(json.loads(out)["metadata"])
    bundle = meta.parent / json.loads(out)["bundle"]
    data = bytearray(bundle.read_bytes())
    data[0] ^= 0xFF
    bundle.write_bytes(bytes(data))

    code, out = _run_cli(["restore-point", "verify", str(meta), "--repo", str(repo), "--json"],
                         repo, capsys)
    assert code == cli.EXIT["conflict"]
    assert json.loads(out)["status"] == "FAILED"
