"""A minimal, dependency-free ``multipart/form-data`` parser.

``cgi.FieldStorage`` was removed from the standard library in Python 3.13, and
this project is otherwise standard-library only, so uploads are parsed here. The
parser is deliberately small and strict:

* it never buffers more than the request body the caller already read;
* it rejects a missing/empty boundary rather than guessing;
* it treats every part name and filename as untrusted text and does not attempt
  to decode a filename using a legacy ``filename*=`` charset (a known
  smuggling vector) - only the plain ``filename="..."`` form is honoured.

The result is a mapping of field name to :class:`Part`, so a caller can require
exactly the fields it expects and reject the rest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADER_SEP = b"\r\n\r\n"
_DISPOSITION_NAME = re.compile(r'name="([^"]*)"')
_DISPOSITION_FILENAME = re.compile(r'filename="([^"]*)"')


class MultipartError(ValueError):
    """The body is not a well-formed multipart/form-data payload."""


@dataclass
class Part:
    name: str
    data: bytes
    filename: str | None = None
    content_type: str | None = None

    @property
    def is_file(self) -> bool:
        return self.filename is not None


def _decode_header_value(raw: bytes) -> str:
    """Decode a part header value as UTF-8, falling back to latin-1.

    Browsers put the raw UTF-8 bytes of a filename into the plain
    ``filename="..."`` form, so decoding with latin-1 alone turns a Cyrillic
    name into mojibake. Decoding as UTF-8 first recovers the real text; a value
    that is not valid UTF-8 (a legacy client) still decodes, byte-for-byte, as
    latin-1 rather than raising.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _parse_headers(raw: bytes) -> tuple[str | None, str | None, str | None]:
    """Return ``(name, filename, content_type)`` from a part's header block."""
    name = filename = content_type = None
    for line in raw.split(b"\r\n"):
        if b":" not in line:
            continue
        key, _, value = line.partition(b":")
        key_l = key.strip().lower()
        value_s = _decode_header_value(value.strip())
        if key_l == b"content-disposition":
            m = _DISPOSITION_NAME.search(value_s)
            if m:
                name = m.group(1)
            f = _DISPOSITION_FILENAME.search(value_s)
            if f:
                filename = f.group(1)
        elif key_l == b"content-type":
            content_type = value_s
    return name, filename, content_type


def parse_multipart(body: bytes, boundary: str) -> dict[str, Part]:
    """Parse *body* into a ``{name: Part}`` mapping.

    Later parts with a duplicate name overwrite earlier ones; the caller that
    cares about that can be built on top, but for the upload endpoints here a
    single value per field is the contract.
    """
    if not boundary:
        raise MultipartError("multipart boundary is missing")
    if len(boundary) > 200:
        raise MultipartError("multipart boundary is too long")
    delimiter = b"--" + boundary.encode("latin-1")
    if delimiter not in body:
        raise MultipartError("multipart body does not contain its boundary")

    parts: dict[str, Part] = {}
    # Split on the delimiter; the first chunk is the preamble and the last is
    # the epilogue after the closing "--boundary--".
    for chunk in body.split(delimiter)[1:]:
        if chunk[:2] == b"--":  # closing delimiter
            break
        chunk = chunk.lstrip(b"\r\n")
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        if not chunk:
            continue
        if _HEADER_SEP not in chunk:
            raise MultipartError("multipart part has no header separator")
        header_raw, _, content = chunk.partition(_HEADER_SEP)
        name, filename, content_type = _parse_headers(header_raw)
        if not name:
            raise MultipartError("multipart part has no field name")
        parts[name] = Part(name=name, data=content, filename=filename,
                           content_type=content_type)
    if not parts:
        raise MultipartError("multipart body has no parts")
    return parts
