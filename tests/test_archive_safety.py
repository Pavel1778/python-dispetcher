"""Archive safety and multipart parsing: no database, no network.

These are the security-critical, dependency-free pieces, so they are tested
directly and exhaustively. A regression here is a path-traversal or a
denial-of-service, which is exactly why every rejection is asserted by name.
"""

from __future__ import annotations

import io
import os
import sys
import tarfile
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.archive_safety import (  # noqa: E402
    ArchiveLimits,
    ArchiveUnsafe,
    is_archive_name,
    normalize_member_name,
    open_archive,
    open_tar,
    open_zip,
)
from orchestration.multipart import MultipartError, parse_multipart  # noqa: E402


def make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def make_tar(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# ------------------------------------------------------------ name validation
@pytest.mark.parametrize("bad", [
    "../etc/passwd", "a/../../b", "..\\..\\x", "/etc/passwd", "C:\\windows\\x",
    "C:/windows/x", "//server/share", "", "   ", "a\x00b", "line\nbreak",
    "./..", "foo/..", "..",
])
def test_traversal_and_absolute_names_are_rejected(bad):
    with pytest.raises(ArchiveUnsafe):
        normalize_member_name(bad)


@pytest.mark.parametrize("good,expected", [
    ("a.txt", "a.txt"),
    ("src/a.txt", "src/a.txt"),
    ("./src//a.txt", "src/a.txt"),
    ("src\\win\\a.txt", "src/win/a.txt"),
    ("dir/", "dir"),
])
def test_safe_names_are_normalized(good, expected):
    assert normalize_member_name(good) == expected


# ------------------------------------------------------------------ zip: happy
def test_zip_roundtrip_keeps_files_and_dirs():
    data = make_zip({"src/a.txt": b"hello", "src/sub/b.txt": b"world"})
    report = open_zip(data)
    paths = {m.path: m.data for m in report.members}
    assert paths["src/a.txt"] == b"hello"
    assert paths["src/sub/b.txt"] == b"world"
    assert report.total_uncompressed == 10


def test_zip_traversal_member_is_refused_before_any_read():
    data = make_zip({"ok.txt": b"x", "../../escape.txt": b"y"})
    with pytest.raises(ArchiveUnsafe, match="escapes the destination"):
        open_zip(data)


def test_zip_symlink_member_is_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        info = zipfile.ZipInfo("link")
        # S_IFLNK in the high 16 bits of external_attr marks a symlink.
        info.external_attr = (0o120777 << 16)
        z.writestr(info, "/etc/passwd")
    with pytest.raises(ArchiveUnsafe, match="symlink"):
        open_zip(buf.getvalue())


def test_zip_with_too_many_entries_is_refused():
    entries = {f"f{i}.txt": b"x" for i in range(50)}
    with pytest.raises(ArchiveUnsafe, match="entries"):
        open_zip(make_zip(entries), ArchiveLimits(max_entries=10))


def test_zip_bomb_by_ratio_is_refused():
    # 8 MiB of zeros compresses to a few kilobytes: ratio far above the ceiling.
    data = make_zip({"bomb.bin": b"\x00" * (8 * 1024 * 1024)})
    with pytest.raises(ArchiveUnsafe, match="ratio"):
        open_zip(data)


def test_zip_entry_larger_than_the_per_file_limit_is_refused():
    data = make_zip({"big.bin": b"a" * 5000})
    with pytest.raises(ArchiveUnsafe, match="larger than"):
        open_zip(data, ArchiveLimits(max_entry_size=1000))


def test_zip_total_uncompressed_limit_is_refused():
    data = make_zip({f"f{i}.bin": b"a" * 1000 for i in range(10)})
    with pytest.raises(ArchiveUnsafe, match="total size limit"):
        open_zip(data, ArchiveLimits(max_total_uncompressed=2500,
                                     max_entry_size=100000,
                                     max_compression_ratio=10_000,
                                     ratio_floor_bytes=10**9))


def test_not_a_zip_is_refused():
    with pytest.raises(ArchiveUnsafe, match="not a valid zip"):
        open_zip(b"this is not a zip")


# ------------------------------------------------------------------ tar: happy
def test_tar_roundtrip():
    report = open_tar(make_tar({"a.txt": b"hi", "dir/b.txt": b"there"}))
    paths = {m.path: m.data for m in report.members}
    assert paths["a.txt"] == b"hi"
    assert paths["dir/b.txt"] == b"there"


def test_tar_symlink_member_is_refused():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../etc/passwd"
        t.addfile(info)
    with pytest.raises(ArchiveUnsafe, match="link"):
        open_tar(buf.getvalue())


def test_tar_device_member_is_refused():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        info = tarfile.TarInfo("dev")
        info.type = tarfile.CHRTYPE
        t.addfile(info)
    with pytest.raises(ArchiveUnsafe, match="special file"):
        open_tar(buf.getvalue())


def test_tar_traversal_is_refused():
    with pytest.raises(ArchiveUnsafe, match="escapes the destination"):
        open_tar(make_tar({"../evil": b"x"}))


# ----------------------------------------------------------- dispatch + gzip
def test_dispatch_by_extension_and_unknown_refused():
    assert open_archive(make_zip({"a": b"b"}), "x.zip").members[0].path == "a"
    with pytest.raises(ArchiveUnsafe, match="unsupported archive type"):
        open_archive(b"data", "x.rar")


def test_gzip_single_stream_decompresses_and_is_capped():
    import gzip

    data = gzip.compress(b"hello world" * 10)
    report = open_archive(data, "x.gz")
    assert report.members[0].data == b"hello world" * 10
    with pytest.raises(ArchiveUnsafe, match="total size limit"):
        open_archive(data, "x.gz", ArchiveLimits(max_total_uncompressed=10))


def test_is_archive_name():
    assert is_archive_name("a.zip") and is_archive_name("b.TAR.GZ")
    assert not is_archive_name("notes.txt")


# ------------------------------------------------------------------ multipart
def _mp(boundary: str, parts: list[tuple[str, str | None, str | None, bytes]]) -> bytes:
    out = b""
    for name, filename, ctype, data in parts:
        out += f"--{boundary}\r\n".encode()
        disp = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disp += f'; filename="{filename}"'
        out += disp.encode() + b"\r\n"
        if ctype:
            out += f"Content-Type: {ctype}\r\n".encode()
        out += b"\r\n" + data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return out


def test_multipart_parses_a_file_part():
    body = _mp("BND", [("file", "a.txt", "text/plain", b"HELLO")])
    parts = parse_multipart(body, "BND")
    assert parts["file"].filename == "a.txt"
    assert parts["file"].data == b"HELLO"
    assert parts["file"].content_type == "text/plain"
    assert parts["file"].is_file


def test_multipart_parses_a_plain_field():
    body = _mp("BND", [("duration_ms", None, None, b"1234")])
    parts = parse_multipart(body, "BND")
    assert parts["duration_ms"].data == b"1234"
    assert not parts["duration_ms"].is_file


def test_multipart_requires_a_boundary():
    with pytest.raises(MultipartError, match="boundary"):
        parse_multipart(b"whatever", "")


def test_multipart_missing_boundary_in_body_is_refused():
    with pytest.raises(MultipartError, match="does not contain its boundary"):
        parse_multipart(b"no boundary here", "BND")
