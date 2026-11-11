"""Start the browser the E2E tests will use, once, and say which one it is.

CI runs this before deciding whether Chromium's system libraries have to be
installed: on the hosted runner images they are already there, and installing
them anyway makes every run depend on the package mirrors.

It launches Chromium exactly the way ``tests/test_browser_e2e.py`` does (the
same flags, the same ``E2E_CHROMIUM_*`` overrides), loads a page and exits 0.
Any failure exits non-zero with the first line of the reason. The lines starting
with ``::`` become annotations on the GitHub job, which is where the outcome can
still be read when the log cannot.
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        print(f"playwright is not installed: {error}")
        return 2
    options: dict = {"args": ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]}
    if os.environ.get("E2E_CHROMIUM_EXECUTABLE"):
        options["executable_path"] = os.environ["E2E_CHROMIUM_EXECUTABLE"]
    if os.environ.get("E2E_CHROMIUM_LD_LIBRARY_PATH"):
        options["env"] = {**os.environ,
                          "LD_LIBRARY_PATH": os.environ["E2E_CHROMIUM_LD_LIBRARY_PATH"]}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(**options)
            page = browser.new_page()
            page.set_content("<p id='probe'>ok</p>")
            text = page.inner_text("#probe")
            version = browser.version
            browser.close()
    except Exception as error:  # noqa: BLE001 - whatever stopped it is the answer
        print(f"::warning title=Chromium::did not start: {str(error).splitlines()[0]}")
        return 1
    if text != "ok":
        print(f"::warning title=Chromium::started but rendered {text!r}")
        return 1
    source = options.get("executable_path", "Playwright's own build")
    print(f"::notice title=Chromium::{version} ({source})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
