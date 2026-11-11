"""The NEXUS Studio front end: static files, read once at import.

The interface is plain HTML, CSS and JavaScript under ``orchestration/studio``.
There is no build step, no template engine and no third-party script: the
browser gets exactly the files in that directory.

This module is the only place that reads them. It exposes

* the two pages (:data:`APP_HTML` and :func:`login_page`), and
* :data:`STATIC`, an explicit allowlist that maps a public URL to the bytes,
  content type and cache policy to answer with.

The allowlist is the security property. The server looks a request path up in
this mapping and nothing else, so no URL can be turned into "read this file from
the package directory". Adding an asset means adding a line here.

Nothing in these files may contain a secret: they are served before login.
"""
from __future__ import annotations

import hashlib
import html
from pathlib import Path

_DIR = Path(__file__).resolve().parent / "studio"

_CSS = "text/css; charset=utf-8"
_JS = "application/javascript; charset=utf-8"
_SVG = "image/svg+xml"
_WOFF2 = "font/woff2"

#: Stylesheets and scripts change with every release and are requested with a
#: content-hash query (see ``ASSET_VERSION``), so a short lifetime is enough.
_SHORT = "public, max-age=300"
#: Font files are immutable in practice; a week keeps repeat visits off the wire.
_FONTS = "public, max-age=604800"

#: Public URL -> (file under ``studio/``, content type, Cache-Control).
_FILES: dict[str, tuple[str, str, str]] = {
    "/static/app.css": ("app.css", _CSS, _SHORT),
    "/static/boot.js": ("boot.js", _JS, _SHORT),
    "/static/markdown.js": ("markdown.js", _JS, _SHORT),
    "/static/highlight.js": ("highlight.js", _JS, _SHORT),
    "/static/app.js": ("app.js", _JS, _SHORT),
    "/static/fonts/inter-latin.woff2": ("fonts/inter-latin.woff2", _WOFF2, _FONTS),
    "/static/fonts/inter-cyrillic.woff2": ("fonts/inter-cyrillic.woff2", _WOFF2, _FONTS),
    "/static/fonts/jetbrains-mono-latin.woff2":
        ("fonts/jetbrains-mono-latin.woff2", _WOFF2, _FONTS),
    "/static/fonts/jetbrains-mono-cyrillic.woff2":
        ("fonts/jetbrains-mono-cyrillic.woff2", _WOFF2, _FONTS),
    "/favicon.svg": ("favicon.svg", _SVG, _SHORT),
    # Browsers probe /favicon.ico on their own; answer with the same mark so the
    # probe is not a 404. SVG is accepted there by every browser this UI targets.
    "/favicon.ico": ("favicon.svg", _SVG, _SHORT),
}


def _load() -> dict[str, tuple[bytes, str, str]]:
    return {url: ((_DIR / name).read_bytes(), content_type, cache)
            for url, (name, content_type, cache) in _FILES.items()}


#: Public URL -> (body, content type, Cache-Control). Read once; see the module
#: docstring for why this is a closed mapping rather than a directory lookup.
STATIC: dict[str, tuple[bytes, str, str]] = _load()

#: A short hash of every asset. Pages reference assets as ``?v=<hash>`` so a new
#: release is fetched immediately instead of after the cache lifetime.
ASSET_VERSION: str = hashlib.sha256(
    b"".join(STATIC[url][0] for url in sorted(STATIC))).hexdigest()[:12]


def _page(name: str) -> str:
    return (_DIR / name).read_text(encoding="utf-8").replace("__ASSET_VERSION__", ASSET_VERSION)


#: The single application page.
APP_HTML: str = _page("index.html")

_LOGIN_HTML: str = _page("login.html")

#: Kept under the old name for callers that only need the icon text.
FAVICON_SVG: str = STATIC["/favicon.svg"][0].decode("utf-8")


def login_page(error: str = "") -> str:
    """The login page, with *error* shown in its alert region.

    The text is HTML-escaped here. Every message passed today is written by the
    server, but one of them embeds configuration diagnostics, and escaping at
    the single point of insertion means no future caller can introduce markup.
    """
    return _LOGIN_HTML.replace("__ERROR__", html.escape(error, quote=True))
