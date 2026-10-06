"""Run real HTTP servers on ephemeral local ports so tests exercise the real network path."""

import socket
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager

import uvicorn
from starlette.types import ASGIApp


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def run_asgi(app: ASGIApp, port: int | None = None) -> Generator[int]:
    """Serve an ASGI app on 127.0.0.1 in a background thread; yields the port."""
    chosen = port or free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=chosen, log_level="warning", lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("test server did not start")
        time.sleep(0.02)
    try:
        yield chosen
    finally:
        server.should_exit = True
        thread.join(timeout=10)
