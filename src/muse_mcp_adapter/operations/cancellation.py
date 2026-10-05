from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager


class CancellationRegistry:
    """Connects an administrative cancel request to an executing client call."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, threading.Event] = {}

    @contextmanager
    def track(self, operation_id: str) -> Generator[threading.Event]:
        event = threading.Event()
        with self._lock:
            self._active[operation_id] = event
        try:
            yield event
        finally:
            with self._lock:
                self._active.pop(operation_id, None)

    def request(self, operation_id: str) -> bool:
        with self._lock:
            event = self._active.get(operation_id)
            if event is None:
                return False
            event.set()
            return True
