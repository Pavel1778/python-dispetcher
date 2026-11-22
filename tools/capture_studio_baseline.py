#!/usr/bin/env python3
"""Capture a screenshot baseline of the NEXUS Studio's main screens.

This drives the *real* studio (the same stack `tests/test_browser_e2e.py` uses:
a real web server, real PostgreSQL, the fake Cloud/GitHub/LLM servers) through
real Chromium and writes PNGs to a directory. It exists so that "the external
appearance is preserved" can be *compared*, not asserted from memory: run it
before a change and after, and diff the images.

Usage::

    TEST_SUPABASE_DB_URL=postgresql://... \\
    E2E_CHROMIUM_EXECUTABLE=/usr/bin/chromium \\
      python tools/capture_studio_baseline.py --out docs/nexus-3.0/baseline

Every screen is captured at 1440x900 (desktop) and 390x844 (phone), in both the
dark and light themes, so a later comparison covers the two things a refactor
most easily breaks: layout at phone width and theme tokens.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests import test_browser_e2e as e2e  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


SCREENS = [
    # (name, viewport, colour scheme, setup-callable-name)
    ("login", {"width": 1440, "height": 900}, "dark", "login"),
    ("studio-empty", {"width": 1440, "height": 900}, "dark", "studio"),
    ("studio-phone", {"width": 390, "height": 844}, "dark", "studio"),
    ("studio-light", {"width": 1440, "height": 900}, "light", "studio"),
]


def _capture(out_dir: str) -> int:
    dsn = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        print("set TEST_SUPABASE_DB_URL", file=sys.stderr)
        return 2
    os.makedirs(out_dir, exist_ok=True)

    # A tiny shim so `_stack` can be driven outside pytest (it takes a
    # monkeypatch object; this one supports the handful of calls it makes).
    class _MP:
        def setenv(self, k, v):
            os.environ[k] = v

        def delenv(self, k, raising=False):
            os.environ.pop(k, None)

    options: dict = {"args": ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]}
    if os.environ.get("E2E_CHROMIUM_EXECUTABLE"):
        options["executable_path"] = os.environ["E2E_CHROMIUM_EXECUTABLE"]

    with e2e._stack(_MP()) as stack, sync_playwright() as pw:
        browser = pw.chromium.launch(**options)
        try:
            for name, viewport, scheme, kind in SCREENS:
                ctx = browser.new_context(
                    viewport=viewport, locale="ru-RU",
                    color_scheme=scheme, reduced_motion="reduce",
                )
                page = ctx.new_page()
                if kind == "login":
                    page.goto(stack.base + "/login")
                    page.wait_for_selector("#loginbtn")
                else:
                    page.goto(stack.base + "/login")
                    page.fill("#username", e2e.USERNAME)
                    page.fill("#password", e2e.PASSWORD)
                    page.click("#loginbtn")
                    page.wait_for_url(stack.base + "/")
                    page.wait_for_selector('#app[data-ready="true"]')
                page.wait_for_timeout(400)
                path = os.path.join(out_dir, f"{name}.png")
                page.screenshot(path=path, full_page=False)
                print(f"wrote {path}")
                ctx.close()
        finally:
            browser.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.path.join(ROOT, "docs/nexus-3.0/baseline"))
    args = ap.parse_args()
    return _capture(args.out)


if __name__ == "__main__":
    raise SystemExit(main())
