"""Open a link in the user's browser when they are sitting at the terminal."""

from __future__ import annotations

import os
import sys
import webbrowser


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("MUSE_VM_NO_BROWSER")


def open_link(url: str) -> bool:
    """Open `url` in the default browser; False when that is not possible or not wanted."""
    if not url.startswith("https://") or not interactive():
        return False
    try:
        return bool(webbrowser.open(url, new=2))
    except webbrowser.Error:
        return False
