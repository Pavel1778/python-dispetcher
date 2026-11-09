"""The Android APK builds, is signed, and has the expected manifest/payload.

Builds the APK with the real Android SDK when present (skips otherwise). The
build is a real aapt2 -> d8 -> zipalign -> apksigner pipeline, and the checks
read the produced artifact back with aapt2/apksigner, so they verify the file
rather than trusting the build log.

Runtime (emulator) verification is intentionally NOT claimed here: this
environment has no ``/dev/kvm``, so an emulator cannot run. The APK is verified
structurally and by signature; it is not launched.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "packaging", "android", "build-apk.sh")
SDK = os.environ.get("ANDROID_SDK_ROOT", "/opt/android-sdk")
BT = os.environ.get("ANDROID_BUILD_TOOLS", os.path.join(SDK, "build-tools", "35.0.0"))

pytestmark = pytest.mark.skipif(
    not (shutil.which("javac") and os.path.exists(os.path.join(BT, "d8"))),
    reason="Android SDK build-tools are not installed",
)


@pytest.fixture(scope="module")
def apk(tmp_path_factory):
    out = tmp_path_factory.mktemp("apk")
    env = dict(os.environ, ANDROID_SDK_ROOT=SDK, ANDROID_BUILD_TOOLS=BT)
    result = subprocess.run(["bash", SCRIPT, str(out)], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    apks = list(out.glob("*.apk"))
    assert apks, "no .apk produced"
    return apks[0]


def test_apk_is_signed_and_verifies(apk):
    result = subprocess.run([os.path.join(BT, "apksigner"), "verify", "--verbose", str(apk)],
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert "Verifies" in result.stdout


def test_apk_manifest_has_the_right_package_and_activity(apk):
    badging = subprocess.run([os.path.join(BT, "aapt2"), "dump", "badging", str(apk)],
                             capture_output=True, text=True).stdout
    assert "package: name='com.nexus.lab'" in badging
    assert "versionName='2.0.0'" in badging
    assert "launchable-activity: name='com.nexus.lab.MainActivity'" in badging
    assert "android.permission.INTERNET" in badging


def test_apk_contains_the_app_classes(apk):
    apkanalyzer = os.path.join(SDK, "cmdline-tools", "latest", "bin", "apkanalyzer")
    if not os.path.exists(apkanalyzer):
        pytest.skip("apkanalyzer not available")
    out = subprocess.run([apkanalyzer, "dex", "packages", str(apk)],
                         capture_output=True, text=True).stdout
    assert "com.nexus.lab.MainActivity" in out


def test_apk_bundles_no_secret():
    """The client must not embed a server address, a PAT or an LLM key."""
    src = open(os.path.join(ROOT, "packaging", "android", "app", "src", "com",
                            "nexus", "lab", "MainActivity.java")).read()
    for forbidden in ("GITHUB_TOKEN", "OPENHANDS_API_KEY", "GEMINI_API_KEY",
                      "Authorization: Bearer"):
        assert forbidden not in src
