"""The Windows executable cross-compiles to a real PE with version metadata.

Cross-compiled with mingw-w64; the checks read the produced PE headers and the
resource section directly, so they verify the artifact rather than the build log.

Runtime verification on Windows is NOT claimed: this environment has no Windows
host, so the .exe is validated structurally and not executed.
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "packaging", "windows", "build-exe.sh")
CC = os.environ.get("MINGW_CC", "x86_64-w64-mingw32-gcc")

pytestmark = pytest.mark.skipif(
    shutil.which(CC) is None, reason="mingw-w64 is not installed"
)


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    out = tmp_path_factory.mktemp("exe")
    result = subprocess.run(["bash", SCRIPT, str(out)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    exes = list(out.glob("*.exe"))
    assert exes, "no .exe produced"
    return exes[0]


def _pe_info(path):
    data = open(path, "rb").read()
    assert data[:2] == b"MZ"
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    assert data[pe:pe + 4] == b"PE\0\0"
    machine = struct.unpack_from("<H", data, pe + 4)[0]
    subsystem = struct.unpack_from("<H", data, pe + 4 + 20 + 68)[0]
    return data, machine, subsystem


def test_exe_is_a_64_bit_windows_pe(exe):
    _, machine, subsystem = _pe_info(exe)
    assert machine == 0x8664  # x86-64
    assert subsystem == 2     # WINDOWS_GUI


def test_exe_has_a_version_resource(exe):
    data, _, _ = _pe_info(exe)
    assert b".rsrc" in data
    for value in ("FileDescription", "ProductName", "2.0.0", "nexus-client.exe"):
        assert value.encode("utf-16-le") in data, f"{value} missing from the version resource"


def test_launcher_accepts_only_http_urls():
    """The launcher source must gate on http/https, like the other clients."""
    src = open(os.path.join(ROOT, "packaging", "windows", "nexus-client.c")).read()
    assert "http://" in src and "https://" in src
    assert "is_http" in src
    # It must not carry a credential or a server secret.
    for forbidden in ("GITHUB_TOKEN", "OPENHANDS_API_KEY", "AUTH_SESSION_SECRET"):
        assert forbidden not in src
