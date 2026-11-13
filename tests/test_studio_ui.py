"""The studio front end, checked as files.

No browser and no database: these are the properties that can be read off the
sources. They exist because the interface has no build step and no framework to
catch a dangling reference, so the checks a compiler would make are made here:

* every control in the markup has a handler, and every handler has a control;
* every element id and icon the script asks for exists;
* the pages carry no inline code, which the Content-Security-Policy would block;
* every file in ``orchestration/studio`` is either served or deliberately not,
  and every one of them is shipped in the package;
* text colours keep a readable contrast in both themes.

The behaviour itself is covered by ``tests/test_browser_e2e.py`` (a real browser)
and ``tests/studio_render_test.js`` (the Markdown renderer and highlighter).
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration import web_ui  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STUDIO = ROOT / "orchestration" / "studio"
SCRIPTS = ("boot.js", "login.js", "markdown.js", "highlight.js", "app.js")


def read(name: str) -> str:
    return (STUDIO / name).read_text(encoding="utf-8")


# ------------------------------------------------------------- wiring
def _actions_in_markup() -> set[str]:
    used = set(re.findall(r'data-action="([\w-]+)"', read("index.html")))
    for name in ("app.js", "markdown.js"):
        used |= set(re.findall(r'data-action="([\w-]+)"', read(name)))
    # explain() names an action for the button under an error message.
    used |= set(re.findall(r"action: '([\w-]+)'", read("app.js")))
    return used


def _handlers() -> set[str]:
    source = read("app.js")
    start = source.index("var ACTIONS = {")
    end = source.index("\n  };", start)
    return set(re.findall(r"^    '([\w-]+)':", source[start:end], flags=re.M))


def test_every_control_has_a_handler_and_every_handler_a_control():
    used, handlers = _actions_in_markup(), _handlers()
    assert len(handlers) > 30, "the ACTIONS table was not found"
    assert used - handlers == set(), "controls that do nothing"
    assert handlers - used == set(), "handlers nothing can trigger"


def test_the_script_only_asks_for_elements_that_exist():
    page, script = read("index.html"), read("app.js")
    defined = set(re.findall(r'\bid="([\w-]+)"', page)) | set(re.findall(r'\bid="([\w-]+)"', script))
    wanted = set(re.findall(r"\$\('#([\w-]+)'", script))
    assert len(wanted) > 40
    assert wanted - defined == set()


def test_every_icon_referenced_is_in_the_sprite():
    page, script = read("index.html"), read("app.js")
    sprite = set(re.findall(r'<symbol id="i-([\w-]+)"', page))
    wanted = set(re.findall(r'href="#i-([\w-]+)"', page))
    wanted |= set(re.findall(r"icon\('([\w-]+)'", script))
    wanted |= set(re.findall(r"\bicon: '([\w-]+)'", script))
    wanted |= set(re.findall(r"href=\"#i-([\w-]+)\"", script))
    # [id, icon, label] rows: settings tabs and the theme switch.
    wanted |= set(re.findall(r"\['[\w-]+', '([\w-]+)', '", script))
    assert len(wanted) > 30
    assert wanted - sprite == set()


def test_element_ids_are_unique_in_the_page():
    ids = re.findall(r'\bid="([\w-]+)"', read("index.html"))
    assert sorted(ids) == sorted(set(ids))


# ------------------------------------------------- what the policy forbids
@pytest.mark.parametrize("name", ["index.html", "login.html"])
def test_pages_have_no_inline_code(name):
    """``script-src 'self'; style-src 'self'`` blocks all of these in the browser."""
    page = read(name)
    for script in re.findall(r"<script\b[^>]*>(.*?)</script>", page, flags=re.S):
        assert script.strip() == "", "an inline script would not run"
    assert re.findall(r"<script\b(?![^>]*\bsrc=)[^>]*>", page) == []
    assert "<style" not in page
    assert re.findall(r"\sstyle\s*=", page) == []
    assert re.findall(r"\son[a-z]+\s*=", page) == [], "inline event handlers do not run"
    assert "javascript:" not in page.lower()
    # Everything the page loads comes from this origin.
    for url in re.findall(r'\b(?:src|href)="([^"#][^"]*)"', page):
        assert url.startswith("/"), url
        assert not url.startswith("//"), url


@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_write_no_style_attribute_and_never_evaluate_text(name):
    script = read(name)
    # Markup built in a string is subject to the same policy as the page.
    assert re.findall(r"\sstyle\s*=\s*\\?[\"']", script) == []
    assert re.findall(r"\son[a-z]+\s*=\s*\\?\"", script) == []
    for banned in ("eval(", "new Function", "document.write", "insertAdjacentHTML",
                   "outerHTML", "setTimeout('", 'setTimeout("', "setInterval('"):
        assert banned not in script, banned
    # Nothing is fetched from, or sent to, another origin.
    assert re.findall(r"""['"]https?://(?!github\.com/|app\.all-hands\.dev)[^'"]*['"]""", script) == []


def test_nothing_in_the_front_end_looks_like_a_credential():
    for path in STUDIO.rglob("*"):
        if path.suffix not in (".js", ".css", ".html", ".svg"):
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}", text), path.name
        assert not re.search(r"Bearer\s+[A-Za-z0-9._-]{16,}", text), path.name


# ------------------------------------------------------------- node
@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_parse(name):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    subprocess.run(["node", "--check", str(STUDIO / name)], check=True, timeout=60)


def test_markdown_renderer_and_highlighter():
    """Untrusted text never becomes markup; see tests/studio_render_test.js."""
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    done = subprocess.run(["node", str(ROOT / "tests" / "studio_render_test.js")],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert "studio render tests passed" in done.stdout


# ---------------------------------------------------- files and packaging
def test_every_file_in_the_studio_directory_is_accounted_for():
    on_disk = {p.relative_to(STUDIO).as_posix() for p in STUDIO.rglob("*") if p.is_file()}
    served = {name for name, _type, _cache in web_ui._FILES.values()}
    pages = {"index.html", "login.html"}            # rendered, never served as files
    licences = {name for name in on_disk if name.startswith("fonts/OFL-")}
    assert served <= on_disk, "a listed asset is missing from the directory"
    assert on_disk - served - pages - licences == set(), "a file nobody serves"
    # The fonts are redistributed under the OFL, which requires its text to ship.
    assert len(licences) == 2
    for name in licences:
        assert "SIL OPEN FONT LICENSE" in read(name).upper()


def test_served_assets_are_what_is_on_disk():
    for url, (name, content_type, _cache) in web_ui._FILES.items():
        body, served_type, cache = web_ui.STATIC[url]
        assert body == (STUDIO / name).read_bytes(), url
        assert served_type == content_type
        assert cache.startswith("public, max-age=")
    assert "__ASSET_VERSION__" not in web_ui.APP_HTML
    assert "__ASSET_VERSION__" not in web_ui.login_page("")


def test_package_data_ships_every_studio_file():
    """A wheel without these files would import and then fail on the first page."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = config["tool"]["setuptools"]["package-data"]["orchestration"]
    package = ROOT / "orchestration"
    for path in STUDIO.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(package).as_posix()
        # setuptools globs do not cross a directory separator.
        matched = any(fnmatch.fnmatchcase(relative, pattern) and
                      relative.count("/") == pattern.count("/") for pattern in patterns)
        assert matched, f"{relative} is not covered by package-data {patterns}"


def test_stylesheet_only_uses_defined_tokens_and_served_files():
    css = read("app.css")
    defined = set(re.findall(r"(--[\w-]+)\s*:", css))
    used = set(re.findall(r"var\((--[\w-]+)", css))
    assert used - defined == set(), "a custom property that is never defined"
    for url in re.findall(r"url\(\"?([^)\"]+)\"?\)", css):
        assert url in web_ui.STATIC, f"{url} is referenced but not served"
    # The fonts the page preloads are the ones the stylesheet declares.
    for url in re.findall(r'rel="preload" href="([^"]+)"', read("index.html")):
        assert url in css and url in web_ui.STATIC


def test_login_page_escapes_the_error_text():
    page = web_ui.login_page('<script>alert(1)</script>" onload="x')
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;&quot; onload=&quot;x" in page
    # No error means an empty alert region, which the stylesheet hides.
    assert '<p class="auth-error" id="loginerr" role="alert"></p>' in web_ui.login_page("")
    assert ".auth-error:empty { display: none; }" in read("app.css")


# -------------------------------------------------------------- contrast
def _tokens(css: str, selector: str) -> dict[str, str]:
    start = css.index(selector + " {")
    return dict(re.findall(r"--([\w-]+):\s*([^;]+);", css[start:css.index("\n}", start)]))


def _colour(value: str) -> tuple[tuple[float, float, float], float]:
    value = value.strip()
    if value.startswith("#"):
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5)), 1.0
    parts = [float(x) for x in re.fullmatch(r"rgba?\(([^)]+)\)", value).group(1).split(",")]
    return tuple(parts[:3]), parts[3] if len(parts) > 3 else 1.0


def _over(front, back):
    (colour, alpha), (under, _) = front, back
    return tuple(c * alpha + u * (1 - alpha) for c, u in zip(colour, under)), 1.0


def _luminance(colour) -> float:
    def channel(value: float) -> float:
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    red, green, blue = colour[0]
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def _contrast(first, second) -> float:
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_text_colours_are_readable(theme):
    """WCAG AA: 4.5:1 for text, 3:1 for a graphic, on every surface they sit on."""
    css = read("app.css")
    tokens = _tokens(css, ":root")
    if theme == "light":
        tokens = {**tokens, **_tokens(css, ':root[data-theme="light"]')}

    def colour(name):
        return _colour(tokens[name])

    pairs = []
    for text in ("text", "text-2", "text-3", "accent-strong", "ok", "warn", "bad"):
        for surface in ("bg", "bg-side", "surface", "surface-2"):
            pairs.append((text, surface, _contrast(colour(text), colour(surface))))
    for token in "ksncftav":                       # syntax colours, on code
        for surface in ("bg", "bg-side"):
            pairs.append((f"t-{token}", surface, _contrast(colour(f"t-{token}"), colour(surface))))
    for tone in ("ok", "warn", "bad"):             # a status pill: tone on its own tint
        tint = _over(colour(f"{tone}-soft"), colour("surface"))
        pairs.append((tone, f"{tone}-soft", _contrast(colour(tone), tint)))
    pairs.append(("accent-strong", "accent-soft", _contrast(
        colour("accent-strong"), _over(colour("accent-soft"), colour("surface-2")))))
    for fill in ("accent-fill", "accent-fill-hover"):   # the label of a filled button
        pairs.append(("on-accent", fill, _contrast(colour("on-accent"), colour(fill))))
    too_low = [(a, b, round(ratio, 2)) for a, b, ratio in pairs if ratio < 4.5]
    assert too_low == []
    # The mark on the gradient (send arrow, avatar letter) at both of its ends.
    for end in ("accent", "accent-2"):
        assert _contrast(colour("on-gradient"), colour(end)) >= 4.5, end
