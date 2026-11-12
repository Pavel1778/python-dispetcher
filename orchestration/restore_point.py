"""Safe restore points: a self-contained, verifiable snapshot taken before work.

The first safety rule of an NEXUS run is to create a restore point before
touching anything. A restore point here is a **git bundle** of the whole
repository (every ref), written next to a metadata record that names the exact
HEAD, branch and working-tree state it captured, plus a SHA-256 of the bundle.
It is written under ``.restore-points/`` (gitignored) and is never pushed.

Properties that matter, and that the tests exercise:

* **Self-contained.** ``git bundle create --all`` packs every ref, so the whole
  history is recoverable from the one file with no network and no dependency on
  the origin.
* **Non-destructive.** Creating one only reads the repository and writes new
  files into the output directory. It never resets, checks out, stashes or
  deletes anything, so it cannot overwrite a user's work.
* **Verifiable.** ``verify`` re-checks the bundle's SHA-256 and runs
  ``git bundle verify``; ``restore`` clones the bundle into a scratch directory
  to prove the history really comes back.
* **Honest about the working tree.** A bundle cannot carry uncommitted changes.
  When the tree is dirty the patch of tracked changes and the list of untracked
  paths are recorded alongside, and the metadata says so. Untracked file
  *contents* are deliberately not copied: they may hold secrets and can be
  large. ``dirty`` and ``untracked`` in the record say what was and was not
  captured.

The module never uses a shell; every git call is an argument list.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

#: Directory (relative to the repository root) restore points are written to.
DEFAULT_DIR = ".restore-points"

#: Metadata file suffix.
META_SUFFIX = ".json"

#: Bundle file suffix.
BUNDLE_SUFFIX = ".bundle"


class RestorePointError(RuntimeError):
    """A restore point could not be created, read or verified."""


@dataclass(frozen=True)
class RestorePoint:
    """The durable record of one restore point."""

    created_at: str
    repo_root: str
    head: str
    branch: str | None
    refs: tuple[str, ...]
    dirty: bool
    untracked: tuple[str, ...]
    git_version: str
    bundle: str
    bundle_sha256: str
    bundle_size: int
    label: str | None = None
    patch: str | None = None
    metadata: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> RestorePoint:
        return cls(
            created_at=data["created_at"],
            repo_root=data["repo_root"],
            head=data["head"],
            branch=data.get("branch"),
            refs=tuple(data.get("refs") or ()),
            dirty=bool(data.get("dirty")),
            untracked=tuple(data.get("untracked") or ()),
            git_version=data.get("git_version", ""),
            bundle=data["bundle"],
            bundle_sha256=data["bundle_sha256"],
            bundle_size=int(data.get("bundle_size", 0)),
            label=data.get("label"),
            patch=data.get("patch"),
            metadata=data.get("metadata", ""),
            notes=tuple(data.get("notes") or ()),
        )

    @property
    def bundle_path(self) -> Path:
        return Path(self.metadata).parent / self.bundle

    @property
    def patch_path(self) -> Path | None:
        if not self.patch:
            return None
        return Path(self.metadata).parent / self.patch


def _git(repo: Path | str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RestorePointError(f"git {' '.join(args)} failed: {detail}")
    return proc


def repo_root_of(path: Path | str) -> Path:
    """Return the repository root containing ``path``."""
    proc = _git(path, "rev-parse", "--show-toplevel")
    root = proc.stdout.strip()
    if not root:
        raise RestorePointError(f"{path} is not inside a git repository")
    return Path(root)


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 1000):
        candidate = path.with_name(f"{stem}-{i}{suffix}")
        if not candidate.exists():
            return candidate
    raise RestorePointError(f"cannot find a free name next to {path}")


def create_restore_point(
    repo: Path | str = ".",
    out_dir: Path | str | None = None,
    *,
    label: str | None = None,
    now: datetime | None = None,
) -> RestorePoint:
    """Create a restore point for ``repo`` and return its record.

    Nothing in the repository is modified: the bundle and its metadata are new
    files under ``out_dir`` (default ``<repo_root>/.restore-points``).
    """
    root = repo_root_of(repo)
    out = Path(out_dir) if out_dir is not None else root / DEFAULT_DIR
    out.mkdir(parents=True, exist_ok=True)

    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    branch_raw = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    branch = None if branch_raw in ("", "HEAD") else branch_raw
    refs = tuple(
        line for line in _git(root, "for-each-ref", "--format=%(refname)").stdout.splitlines()
        if line.strip()
    )
    status = _git(root, "status", "--porcelain").stdout
    untracked = tuple(
        line for line in _git(root, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
        if line.strip()
    )
    dirty = bool(status.strip())
    git_version = _git(root, "--version").stdout.strip()

    moment = now or datetime.now(timezone.utc)
    stamp = moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"nexus-restore-{stamp}-{head[:12]}"

    bundle_path = _unique(out / f"{base}{BUNDLE_SUFFIX}")
    # Bundle every ref so the whole history is in the one file. Write to a
    # temporary name first so an interrupted run never leaves a half-written
    # file under the final name.
    tmp_bundle = bundle_path.with_suffix(bundle_path.suffix + ".partial")
    _git(root, "bundle", "create", str(tmp_bundle), "--all")
    tmp_bundle.replace(bundle_path)

    patch_path: Path | None = None
    notes: list[str] = []
    if dirty:
        diff = _git(root, "diff", "--binary", "HEAD").stdout
        if diff.strip():
            patch_path = _unique(out / f"{base}.patch")
            patch_path.write_text(diff, encoding="utf-8")
            notes.append("tracked changes captured in the patch file")
        else:
            notes.append("working tree is dirty but git diff HEAD is empty")
        if untracked:
            notes.append(
                f"{len(untracked)} untracked path(s) listed by name only; "
                "their contents are not copied"
            )

    metadata_path = _unique(out / f"{base}{META_SUFFIX}")
    record = RestorePoint(
        created_at=moment.astimezone(timezone.utc).isoformat(),
        repo_root=str(root),
        head=head,
        branch=branch,
        refs=refs,
        dirty=dirty,
        untracked=untracked,
        git_version=git_version,
        bundle=bundle_path.name,
        bundle_sha256=sha256_file(bundle_path),
        bundle_size=bundle_path.stat().st_size,
        label=label,
        patch=patch_path.name if patch_path else None,
        metadata=str(metadata_path),
        notes=tuple(notes),
    )
    metadata_path.write_text(
        json.dumps(record.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return record


def list_restore_points(out_dir: Path | str) -> list[RestorePoint]:
    """Read every metadata record under ``out_dir``, newest first."""
    directory = Path(out_dir)
    if not directory.is_dir():
        return []
    points: list[RestorePoint] = []
    for meta in directory.glob(f"*{META_SUFFIX}"):
        try:
            points.append(RestorePoint.from_dict(json.loads(meta.read_text(encoding="utf-8"))))
        except (OSError, ValueError, KeyError):
            continue
    points.sort(key=lambda p: p.created_at, reverse=True)
    return points


def _metadata_for(path: Path) -> Path:
    if path.suffix == META_SUFFIX:
        return path
    if path.suffix == BUNDLE_SUFFIX:
        candidate = path.with_suffix(META_SUFFIX)
        if candidate.exists():
            return candidate
    raise RestorePointError(f"no metadata record for {path}")


def verify_restore_point(path: Path | str, repo: Path | str | None = None) -> dict:
    """Verify one restore point. Returns a report with ``status`` VERIFIED/FAILED.

    Checks: the metadata parses, the bundle exists, its SHA-256 still matches the
    recorded value, ``git bundle verify`` accepts it, and the recorded HEAD is
    present in the bundle.
    """
    checks: dict[str, dict] = {}
    reasons: list[str] = []

    def fail(name: str, detail: str) -> None:
        checks[name] = {"ok": False, "detail": detail}
        reasons.append(f"{name}: {detail}")

    def ok(name: str, detail: str = "") -> None:
        checks[name] = {"ok": True, "detail": detail}

    try:
        meta_path = _metadata_for(Path(path))
        record = RestorePoint.from_dict(json.loads(meta_path.read_text(encoding="utf-8")))
        ok("metadata", str(meta_path))
    except (RestorePointError, OSError, ValueError, KeyError) as exc:
        fail("metadata", str(exc))
        return {"status": "FAILED", "ok": False, "checks": checks, "reasons": reasons}

    bundle = Path(record.metadata).parent / record.bundle
    if not bundle.exists():
        fail("bundle", f"missing: {bundle}")
        return {"status": "FAILED", "ok": False, "checks": checks, "reasons": reasons}
    ok("bundle", str(bundle))

    actual = sha256_file(bundle)
    if actual != record.bundle_sha256:
        fail("sha256", f"expected {record.bundle_sha256}, got {actual}")
    else:
        ok("sha256", actual)

    where = repo or (record.repo_root if Path(record.repo_root).is_dir() else Path.cwd())
    proc = _git(where, "bundle", "verify", str(bundle), check=False)
    if proc.returncode != 0:
        fail("git_bundle_verify", (proc.stderr or proc.stdout).strip())
    else:
        ok("git_bundle_verify", (proc.stdout or "").strip().splitlines()[0] if proc.stdout else "")

    heads = _git(where, "bundle", "list-heads", str(bundle), check=False)
    if record.head and record.head not in heads.stdout:
        fail("head_present", f"recorded HEAD {record.head} not among the bundle's refs")
    elif record.head:
        ok("head_present", record.head)

    status = "VERIFIED" if not reasons else "FAILED"
    return {"status": status, "ok": not reasons, "checks": checks, "reasons": reasons,
            "restore_point": record.to_dict()}


def restore_restore_point(path: Path | str, dest: Path | str) -> Path:
    """Clone a restore point's bundle into ``dest`` to prove it restores.

    ``dest`` must not exist. Returns the created directory.
    """
    meta_path = _metadata_for(Path(path))
    record = RestorePoint.from_dict(json.loads(meta_path.read_text(encoding="utf-8")))
    bundle = Path(record.metadata).parent / record.bundle
    if not bundle.exists():
        raise RestorePointError(f"missing bundle: {bundle}")
    target = Path(dest)
    if target.exists():
        raise RestorePointError(f"destination already exists: {target}")
    proc = subprocess.run(
        ["git", "clone", str(bundle), str(target)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RestorePointError(f"restore failed: {(proc.stderr or proc.stdout).strip()}")
    return target
