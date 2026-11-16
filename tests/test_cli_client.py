"""The NEXUS CLI's bearer client must refuse non-http(s) base URLs.

Reproduction of the finding from the 2026-11-15 security pass:
``Client("file:///etc/passwd", None)`` used to construct silently, and
``_request`` would hand the URL to ``urlopen``, which reads local files
and speaks any scheme, into the CLI's own output. Found by
``ruff --select S`` (S310); fixed by refusing the scheme at construction.

(Restored in the post-consolidation branch; identical to the version from
``arena/b10d2a83-python-dispetcher`` @ ``49747b8``.)
"""

from __future__ import annotations

import pytest

from orchestration import cli


def test_file_scheme_is_refused():
    with pytest.raises(cli.CliError) as excinfo:
        cli.Client("file:///etc/passwd", None)
    assert excinfo.value.code == cli.EXIT["config"]
    assert "http" in str(excinfo.value)


def test_schemeless_url_is_refused():
    with pytest.raises(cli.CliError):
        cli.Client("localhost:3920", None)


def test_ftp_scheme_is_refused():
    with pytest.raises(cli.CliError):
        cli.Client("ftp://nexus.example/api", None)


def test_https_is_accepted():
    client = cli.Client("https://nexus.example/", "tok")
    assert client.base_url == "https://nexus.example"
    assert client.token == "tok"


def test_loopback_http_is_accepted():
    # A local NEXUS server is a supported setup; loopback stays usable.
    client = cli.Client("http://127.0.0.1:3920/", None)
    assert client.base_url == "http://127.0.0.1:3920"
