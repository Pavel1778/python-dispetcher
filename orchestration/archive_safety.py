"""Safe reading of uploaded archives.

Extracting an archive is the single most dangerous operation a file upload can
trigger, so this module is deliberately paranoid and has no dependencies beyond
the standard library. It refuses, before any byte is written anywhere:

* **Zip Slip / path traversal.** A member named ``../../etc/passwd`` or
  ``/etc/passwd`` or ``C:\\Windows\\...`` would, in a naive ``extractall``, be
  written outside the destination. Every name is normalized and rejected unless
  it is a plain relative path made of ordinary segments.
* **Symlinks and special files.** A symlink whose target escapes the root turns
  any later write through it into an arbitrary-file write; tar device nodes,
  FIFOs and hardlinks are refused outright.
* **Archive bombs.** A tiny archive can expand to gigabytes (a "zip bomb") or
  contain an enormous number of members ("inode exhaustion"). Both the declared
  and the *actually read* sizes are capped, and a compression-ratio ceiling
  catches the classic nested-def late bomb.
* **Nested bombs.** Members whose own name is an archive are not extracted
  recursively; a caller that wants that must ask for it explicitly and pay the
  same limits again.

The module never writes to disk. It yields validated ``(relative_path, bytes)``
pairs and leaves storage to the caller, which keeps the trust boundary in one
place: nothing downstream has to re-check a name.
"""

from __future__ import annotations

import io
import posixpath
import tarfile
import zipfile
from dataclasses import dataclass, field

#: Extensions we are willing to open as archives. Deliberately short.
ZIP_EXTS = (".zip",)
TAR_EXTS = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
GZIP_EXTS = (".gz",)
BZIP2_EXTS = (".bz2",)
XZ_EXTS = (".xz",)
ARCHIVE_EXTS = ZIP_EXTS + TAR_EXTS + GZIP_EXTS + BZIP2_EXTS + XZ_EXTS

#: Characters that never appear in a legitimate relative path we accept.
_FORBIDDEN_NAME_CHARS = set("\x00\r\n")


class ArchiveUnsafe(ValueError):
    """The archive cannot be safely extracted. Carries a human-readable reason."""


@dataclass(frozen=True)
class ArchiveLimits:
    """Hard ceilings for one archive. Every value is a refusal threshold."""

    max_entries: int = 2000
    max_total_uncompressed: int = 256 * 1024 * 1024  # 256 MiB
    max_entry_size: int = 64 * 1024 * 1024  # 64 MiB
    max_compression_ratio: float = 200.0
    #: Only applies once the archive is larger than this, so a tiny file with a
    #: huge ratio (a few bytes -> a few hundred bytes) is not rejected.
    ratio_floor_bytes: int = 1 * 1024 * 1024  # 1 MiB
    max_depth: int = 16
    max_name_length: int = 255


@dataclass
class ArchiveMember:
    """One validated member, ready to store."""

    path: str
    data: bytes
    is_dir: bool = False

    def as_dict(self) -> dict:
        return {"path": self.path, "size": len(self.data), "is_dir": self.is_dir}


@dataclass
class ArchiveReport:
    members: list[ArchiveMember] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    total_uncompressed: int = 0

    def as_dict(self) -> dict:
        return {
            "members": [m.as_dict() for m in self.members],
            "skipped": self.skipped,
            "count": len(self.members),
            "total_uncompressed": self.total_uncompressed,
        }


def normalize_member_name(name: str) -> str:
    """Return a safe relative path for *name*, or raise :class:`ArchiveUnsafe`.

    Rejects, in order: non-strings, NUL/CR/LF, absolute POSIX paths, Windows
    drive letters and UNC paths, ``..`` segments anywhere, and names that
    normalize to nothing. Backslashes are treated as separators first, because a
    Windows-built zip uses them and ``zipfile`` does not translate them.
    """
    if not isinstance(name, str):
        raise ArchiveUnsafe("archive member name is not text")
    if any(ch in _FORBIDDEN_NAME_CHARS for ch in name):
        raise ArchiveUnsafe(f"archive member name has control characters: {name!r}")
    # Windows clients store ``\`` as the separator; zipfile hands it through
    # untouched. Normalize before any other check so ``..\..\x`` cannot slip by.
    candidate = name.replace("\\", "/").strip()
    if not candidate:
        raise ArchiveUnsafe("archive contains an empty member name")
    if candidate.startswith("/"):
        raise ArchiveUnsafe(f"archive member is an absolute path: {name!r}")
    # ``C:/...`` or ``C:...`` - a drive-relative or drive-absolute path.
    if len(candidate) >= 2 and candidate[1] == ":":
        raise ArchiveUnsafe(f"archive member has a drive letter: {name!r}")
    if candidate.startswith("//"):
        raise ArchiveUnsafe(f"archive member is a UNC path: {name!r}")

    parts = [p for p in candidate.split("/") if p not in ("", ".")]
    if not parts:
        raise ArchiveUnsafe(f"archive member normalizes to nothing: {name!r}")
    if any(part == ".." for part in parts):
        raise ArchiveUnsafe(f"archive member escapes the destination: {name!r}")
    # A trailing slash marks a directory in some zips; keep it as a plain path.
    return posixpath.join(*parts)


def _check_depth_and_length(rel: str, limits: ArchiveLimits) -> None:
    if len(rel) > limits.max_name_length:
        raise ArchiveUnsafe(f"archive member name is too long: {rel[:60]}...")
    if rel.count("/") + 1 > limits.max_depth:
        raise ArchiveUnsafe(f"archive member is nested too deeply: {rel}")


def _ratio_ok(total_uncompressed: int, compressed: int, limits: ArchiveLimits) -> bool:
    if total_uncompressed <= limits.ratio_floor_bytes:
        return True
    if compressed <= 0:
        # No compressed size to compare against; only the absolute cap applies.
        return True
    return total_uncompressed / compressed <= limits.max_compression_ratio


def open_zip(data: bytes, limits: ArchiveLimits | None = None) -> ArchiveReport:
    """Read a zip from *data*, returning only validated members.

    Directories are recorded (so a caller can recreate the tree) but carry no
    bytes. Symlink members (Unix mode ``S_IFLNK``) are refused: a symlink is the
    classic way to make a later write land outside the destination.
    """
    limits = limits or ArchiveLimits()
    report = ArchiveReport()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ArchiveUnsafe(f"not a valid zip archive: {e}") from e

    with zf:
        infos = zf.infolist()
        if len(infos) > limits.max_entries:
            raise ArchiveUnsafe(
                f"archive has {len(infos)} entries; limit is {limits.max_entries}"
            )
        declared_total = 0
        validated: list[tuple[zipfile.ZipInfo, str]] = []
        for info in infos:
            rel = normalize_member_name(info.filename)
            _check_depth_and_length(rel, limits)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:  # S_IFLNK
                raise ArchiveUnsafe(f"archive contains a symlink: {info.filename!r}")
            if info.file_size > limits.max_entry_size:
                raise ArchiveUnsafe(
                    f"archive member {info.filename!r} is larger than "
                    f"{limits.max_entry_size} bytes"
                )
            declared_total += info.file_size
            if declared_total > limits.max_total_uncompressed:
                raise ArchiveUnsafe(
                    "archive expands past the total size limit "
                    f"({limits.max_total_uncompressed} bytes)"
                )
            validated.append((info, rel))
        if not _ratio_ok(declared_total, len(data), limits):
            raise ArchiveUnsafe("archive compression ratio is suspiciously high")

        for info, rel in validated:
            if info.is_dir():
                report.members.append(ArchiveMember(rel, b"", is_dir=True))
                continue
            # Read with a running cap: ``file_size`` is attacker-controlled and
            # can lie, so the *actually read* bytes are what count.
            with zf.open(info) as fh:
                chunk = fh.read(limits.max_entry_size + 1)
            if len(chunk) > limits.max_entry_size:
                raise ArchiveUnsafe(
                    f"archive member {info.filename!r} exceeded the per-file limit "
                    "while decompressing"
                )
            report.total_uncompressed += len(chunk)
            if report.total_uncompressed > limits.max_total_uncompressed:
                raise ArchiveUnsafe("archive exceeded the total size limit while decompressing")
            report.members.append(ArchiveMember(rel, chunk))
    return report


def _tar_link_is_safe(rel_target: str, member_path: str) -> bool:
    """Whether a tar link target stays inside the extraction root.

    A relative target is resolved against the member's own directory; the
    result must not climb above the root. Absolute targets are always unsafe.
    """
    target = rel_target.replace("\\", "/").strip()
    if target.startswith("/") or (len(target) >= 2 and target[1] == ":"):
        return False
    base = posixpath.dirname(member_path)
    resolved = posixpath.normpath(posixpath.join(base, target))
    return resolved != ".." and not resolved.startswith("../")


def open_tar(data: bytes, limits: ArchiveLimits | None = None) -> ArchiveReport:
    """Read a tar (optionally compressed) from *data*.

    Only regular files and directories are accepted. Symlinks, hardlinks,
    character/block devices and FIFOs are refused, because none of them is ever
    needed to hand a user's project to an agent and each one is an escape route.
    """
    limits = limits or ArchiveLimits()
    report = ArchiveReport()
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.TarError as e:
        raise ArchiveUnsafe(f"not a valid tar archive: {e}") from e

    with tf:
        members = tf.getmembers()
        if len(members) > limits.max_entries:
            raise ArchiveUnsafe(
                f"archive has {len(members)} entries; limit is {limits.max_entries}"
            )
        declared_total = 0
        validated: list[tuple[tarfile.TarInfo, str]] = []
        for member in members:
            rel = normalize_member_name(member.name)
            _check_depth_and_length(rel, limits)
            if member.issym() or member.islnk():
                # Refuse outright rather than resolve: even a "safe" link is a
                # second path to a file and complicates every later check.
                raise ArchiveUnsafe(f"archive contains a link: {member.name!r}")
            if member.ischr() or member.isblk() or member.isfifo():
                raise ArchiveUnsafe(f"archive contains a special file: {member.name!r}")
            if not (member.isfile() or member.isdir()):
                raise ArchiveUnsafe(f"archive contains an unsupported entry: {member.name!r}")
            if member.size > limits.max_entry_size:
                raise ArchiveUnsafe(
                    f"archive member {member.name!r} is larger than "
                    f"{limits.max_entry_size} bytes"
                )
            declared_total += member.size
            if declared_total > limits.max_total_uncompressed:
                raise ArchiveUnsafe(
                    "archive expands past the total size limit "
                    f"({limits.max_total_uncompressed} bytes)"
                )
            validated.append((member, rel))
        if not _ratio_ok(declared_total, len(data), limits):
            raise ArchiveUnsafe("archive compression ratio is suspiciously high")

        for member, rel in validated:
            if member.isdir():
                report.members.append(ArchiveMember(rel, b"", is_dir=True))
                continue
            extracted = tf.extractfile(member)
            if extracted is None:
                report.skipped.append(rel)
                continue
            chunk = extracted.read(limits.max_entry_size + 1)
            if len(chunk) > limits.max_entry_size:
                raise ArchiveUnsafe(
                    f"archive member {member.name!r} exceeded the per-file limit "
                    "while decompressing"
                )
            report.total_uncompressed += len(chunk)
            if report.total_uncompressed > limits.max_total_uncompressed:
                raise ArchiveUnsafe("archive exceeded the total size limit while decompressing")
            report.members.append(ArchiveMember(rel, chunk))
    return report


def _decompress_single(data: bytes, limits: ArchiveLimits, codec: str) -> ArchiveReport:
    """Decompress a single-file stream (gzip/bzip2/xz) under a running cap."""
    import bz2
    import gzip
    import lzma

    report = ArchiveReport()
    opener = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}[codec]
    try:
        with opener(io.BytesIO(data), "rb") as fh:
            out = fh.read(limits.max_total_uncompressed + 1)
    except (OSError, EOFError, lzma.LZMAError) as e:
        raise ArchiveUnsafe(f"not a valid {codec} stream: {e}") from e
    if len(out) > limits.max_total_uncompressed:
        raise ArchiveUnsafe(
            f"{codec} stream expands past the total size limit "
            f"({limits.max_total_uncompressed} bytes)"
        )
    report.total_uncompressed = len(out)
    report.members.append(ArchiveMember("content", out))
    return report


def open_archive(data: bytes, filename: str,
                 limits: ArchiveLimits | None = None) -> ArchiveReport:
    """Dispatch to the right reader based on *filename*'s extension.

    Unknown extensions are refused rather than guessed at: a caller that wants
    to read a zip must name it ``.zip``.
    """
    limits = limits or ArchiveLimits()
    name = (filename or "").lower()
    if name.endswith(ZIP_EXTS):
        return open_zip(data, limits)
    if name.endswith(TAR_EXTS):
        return open_tar(data, limits)
    if name.endswith(GZIP_EXTS):
        return _decompress_single(data, limits, "gz")
    if name.endswith(BZIP2_EXTS):
        return _decompress_single(data, limits, "bz2")
    if name.endswith(XZ_EXTS):
        return _decompress_single(data, limits, "xz")
    raise ArchiveUnsafe(f"unsupported archive type: {filename!r}")


def is_archive_name(filename: str) -> bool:
    return (filename or "").lower().endswith(ARCHIVE_EXTS)
