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


_STYLE = (
    "body{font-family:system-ui,sans-serif;margin:0;background:#f6f6f4;color:#1b1b1b}"
    "main{max-width:44rem;margin:0 auto;padding:1.5rem 1rem 4rem}"
    "h1{font-size:1.4rem;margin:.2rem 0 1rem}h2{font-size:1.05rem;margin:1.6rem 0 .5rem}"
    ".bar{background:#1b1b1b;color:#fff;padding:.6rem 1rem;font-size:.85rem}"
    ".box{background:#fff;border:1px solid #d8d8d4;border-radius:8px;padding:.8rem 1rem;"
    "margin:.6rem 0}.quote{border-left:4px solid #b9b9b2;background:#fff}"
    ".warn{border-color:#c77d00;background:#fff7e6}.bad{border-color:#b3261e;background:#fdecea}"
    ".ok{border-color:#2e7d32;background:#eef7ee}"
    "table{width:100%;border-collapse:collapse;font-size:.92rem}"
    "td,th{text-align:left;padding:.4rem .3rem;border-bottom:1px solid #e3e3df;"
    "vertical-align:top}.tag{font-size:.75rem;padding:.1rem .4rem;border-radius:4px;"
    "background:#e8e8e4}.r{background:#dff0e0}.w{background:#fde9c8}.s{background:#f8d4d1}"
    "button{font:inherit;padding:.55rem 1rem;border-radius:6px;border:1px solid #1b1b1b;"
    "background:#fff;cursor:pointer}button.go{background:#1b1b1b;color:#fff}"
    "input[type=text],input[type=password]{font:inherit;padding:.5rem;width:100%;"
    "box-sizing:border-box}code{background:#ececE8;padding:.1rem .3rem;border-radius:4px;"
    "word-break:break-all}.muted{color:#5b5b55;font-size:.88rem}"
    "details{margin:.3rem 0}summary{cursor:pointer}"
)


def document(title: str, body: str, status: int = 200) -> Response:
    """A full page. ``body`` must be trusted, already-escaped markup; the title is escaped."""
    text = (
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style>"
        f"<body>{body}"
    )
    response = HTMLResponse(text, status_code=status)
    response.headers.update(_SECURITY_HEADERS)
    return response


def redirect(url: str) -> Response:
    response = RedirectResponse(url, status_code=302)
    response.headers.update(_SECURITY_HEADERS)
    return response
