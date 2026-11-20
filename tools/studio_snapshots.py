"""Capture the existing Studio on a disposable real backend, never a deployed site.

    tools/with_disposable_postgres.sh python tools/studio_snapshots.py OUT [--compare REF]

Comparison decodes PNGs (Pillow, optional). It fails on even one different
pixel or a missing/extra screen; always use the same Chromium and fonts.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version as package_version
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def capture(browser, out: Path, *, phone: bool) -> None:
    from tests.test_browser_e2e import _connect_repository, _login, _open, _send, _send_tap, _stack

    mobile = {"viewport": {"width": 390, "height": 844}, "is_mobile": True,
              "has_touch": True, "device_scale_factor": 2} if phone else {}
    ctx, page, watch = _open(browser, timezone_id="UTC", **mobile)

    def shot(name: str) -> None:
        page.evaluate("() => document.fonts.ready.then(() => true)")
        page.mouse.move(0, 0)
        assert page.evaluate("() => document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=out / f"{'phone' if phone else 'desktop'}-{name}.png",
                        scale="css", caret="hide", animations="disabled")

    try:
        with pytest.MonkeyPatch.context() as mp, _stack(mp, lab_inline=False) as stack:
            page.goto(stack.base + "/login")
            expect(page.locator("#loginbtn")).to_be_visible()
            if not phone:
                shot("login")
            _login(page, stack.base)
            expect(page.locator(".hero h1")).to_have_text("Что будем строить?")
            shot("home")
            stack.answers("Откройте **корзину** в дереве файлов.\n\n```python\nadd(2, 3)\n```")
            send = _send_tap if phone else _send
            send(page, "Как устроена корзина?")
            expect(page.locator(".msg.assistant .code-lang")).to_have_text("python")
            # Freeze only fixture data, not the layout: masking the displayed
            # time still shifts the copy button as different digits change width.
            with stack.app.store._tx() as cur:
                cur.execute("UPDATE nexus_conversation_messages SET created_at = "
                            "timestamptz '2026-01-01 12:34:00+00' + "
                            "CASE WHEN role='user' THEN interval '0s' ELSE interval '1s' END")
            page.reload()
            expect(page.locator(".msg-meta > span[title]")).to_have_text("12:34")
            expect(page.locator(".sb-link")).to_have_count(1)
            shot("chat")
            if phone:
                page.tap("#open-sidebar")
                expect(page.locator(".sb-link")).to_be_in_viewport()
                shot("history")
            else:
                _connect_repository(page, analyze=False)
                page.click("#ws-toggle")
                page.click('.tree-row.file[data-path="src/cart.py"]')
                expect(page.locator(".codeview .cl")).to_have_count(4)
                shot("files")
                page.click("#tab-agent")
                expect(page.locator("#ws-body .empty h3")).to_have_text("Агент ещё не запускался")
                shot("agent")
                page.click("#user-menu")
                page.click('.menu-item[data-action="open-settings"]')
                expect(page.locator(".kv.is-status")).to_be_visible()
                shot("settings")
            assert watch.problems == [], watch.problems
    finally:
        ctx.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out", type=Path, help="new directory; never overwrites a reference")
    ap.add_argument("--compare", type=Path)
    args = ap.parse_args()
    # _stack resets its database. Require exactly the private socket cluster that
    # the wrapper created, not a TEST_* variable that could point anywhere.
    from psycopg.conninfo import conninfo_to_dict
    socket = Path.home().parent
    params = conninfo_to_dict(os.environ.get("TEST_SUPABASE_DB_URL", ""))
    if not (socket.parent == Path('/tmp') and socket.name.startswith('nexus-audit.')
            and params == dict(user='nexus_audit', dbname='nexus_audit', host=str(socket), port='5432')
            and (socket / 'data/postmaster.pid').is_file()):
        ap.error("run under tools/with_disposable_postgres.sh; refusing to reset a supplied database")
    args.out.mkdir(parents=True, exist_ok=False)
    options = {"args": ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]}
    if exe := os.getenv("E2E_CHROMIUM_EXECUTABLE"):
        options["executable_path"] = exe
    if libs := os.getenv("E2E_CHROMIUM_LD_LIBRARY_PATH"):
        options["env"] = dict(os.environ, LD_LIBRARY_PATH=libs)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**options)
        try:
            for phone in (False, True):
                capture(browser, args.out, phone=phone)
            version = browser.version
        finally:
            browser.close()
    paths = sorted(args.out.glob("*.png"))
    studio = ROOT / "orchestration" / "studio"
    manifest = {"chromium": version, "playwright": package_version('playwright'),
                "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "assets": {p.relative_to(studio).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in sorted(studio.rglob('*')) if p.is_file()},
                "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if args.compare:
        from PIL import Image, ImageChops
        if {p.name for p in paths} != {p.name for p in args.compare.glob('*.png')}:
            sys.exit("different screen inventory")
        failed = False
        for p in paths:
            with Image.open(p) as actual, Image.open(args.compare / p.name) as expected:
                if actual.size != expected.size:
                    print(f"{p.name}: size changed")
                    failed = True
                    continue
                box = ImageChops.difference(actual.convert('RGB'), expected.convert('RGB')).getbbox()
                print(f"{p.name}: {box or 'identical'}")
                failed |= box is not None
        return int(failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
