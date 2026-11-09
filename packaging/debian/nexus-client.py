#!/usr/bin/env python3
"""Open the NEXUS dashboard in the user's browser.

The Debian package ships a thin desktop client: it opens the configured NEXUS
backend URL in the default browser. It stores no credentials itself -- the web
UI's own login (session cookie) is what authenticates, exactly as on the web.

The URL is read from ``$NEXUS_URL`` or ``~/.config/nexus/client.conf`` so the
package contains no server address and no secret.
"""
from __future__ import annotations

import configparser
import os
import pathlib
import sys
import urllib.parse
import webbrowser

DEFAULT = "http://127.0.0.1:8000"


def backend_url() -> str:
    env = os.environ.get("NEXUS_URL", "").strip()
    if env:
        return env
    conf = pathlib.Path.home() / ".config" / "nexus" / "client.conf"
    if conf.exists():
        parser = configparser.ConfigParser()
        parser.read(conf)
        url = parser.get("nexus", "url", fallback="").strip()
        if url:
            return url
    return DEFAULT


def main(argv: list[str]) -> int:
    url = argv[1] if len(argv) > 1 else backend_url()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        print(f"refusing to open a non-http(s) URL: {url}", file=sys.stderr)
        return 2
    print(f"Opening NEXUS at {url}")
    if not webbrowser.open(url):
        print(f"Could not launch a browser; open {url} manually.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
