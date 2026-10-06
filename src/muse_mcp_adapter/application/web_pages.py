"""Small, safe HTML and redirect responses for the browser-facing flows."""

from __future__ import annotations

import html

from starlette.responses import HTMLResponse, RedirectResponse, Response

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
    "frame-ancestors 'none'; base-uri 'none'",
}


def page(title: str, message: str, status: int = 200, *, extra: str = "") -> Response:
    """A minimal page. All text is escaped; ``extra`` must be trusted, pre-escaped markup."""
    body = (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>{html.escape(title)}</title>"
        "<body style='font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>{extra}"
    )
    response = HTMLResponse(body, status_code=status)
    response.headers.update(_SECURITY_HEADERS)
    return response


def redirect(url: str) -> Response:
    response = RedirectResponse(url, status_code=302)
    response.headers.update(_SECURITY_HEADERS)
    return response
