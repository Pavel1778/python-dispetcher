"""The Debian package builds, has valid metadata and the expected payload.

This does not require dpkg to be able to *install* into the test environment
(the container's python3 is not a dpkg package); it validates the archive
structure and the launcher behaviour, which is what can be checked portably.
`dpkg-deb --info`/`--contents` are used for real; the launcher is run directly.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "packaging", "debian", "build-deb.sh")

pytestmark = pytest.mark.skipif(
    not shutil.which("dpkg-deb"), reason="dpkg-deb is not installed"
)


@pytest.fixture(scope="module")
def built_deb(tmp_path_factory):
    out = tmp_path_factory.mktemp("deb")
    result = subprocess.run(["bash", SCRIPT, str(out)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    debs = list(out.glob("*.deb"))
    assert debs, "no .deb produced"
    return debs[0]


def test_control_metadata(built_deb):
    info = subprocess.run(["dpkg-deb", "--info", str(built_deb)],
                          capture_output=True, text=True).stdout
    assert "Package: nexus-client" in info
    assert "Version: 2.0.0" in info
    assert "Architecture: all" in info
    assert "Description: NEXUS" in info


def test_payload_contains_launcher_desktop_and_icons(built_deb):
    contents = subprocess.run(["dpkg-deb", "--contents", str(built_deb)],
                              capture_output=True, text=True).stdout
    assert "./usr/bin/nexus-client" in contents
    assert "./usr/share/applications/nexus.desktop" in contents
    assert "192x192/apps/nexus.png" in contents
    assert "512x512/apps/nexus.png" in contents


def test_launcher_refuses_a_non_http_scheme():
    script = os.path.join(ROOT, "packaging", "debian", "nexus-client.py")
    result = subprocess.run([sys.executable, script, "ftp://host"],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "refusing" in result.stderr


def test_launcher_reads_nexus_url_from_env():
    script = os.path.join(ROOT, "packaging", "debian", "nexus-client.py")
    env = dict(os.environ, NEXUS_URL="http://example.invalid:1234")
    result = subprocess.run([sys.executable, script], capture_output=True, text=True, env=env)
    # It will fail to launch a browser in CI, but it must name the env URL.
    assert "example.invalid:1234" in result.stdout


def test_no_secret_is_bundled():
    """The package must not carry a server address or a credential."""
    src = open(os.path.join(ROOT, "packaging", "debian", "nexus-client.py")).read()
    assert "GITHUB_TOKEN" not in src
    assert "OPENHANDS_API_KEY" not in src
    assert "AUTH_" not in src
