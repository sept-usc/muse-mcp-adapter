from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Callable, Generator
from contextlib import contextmanager

from ..domain.errors import RateLimitExceededError


class FixedWindowRateLimiter:
    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValueError("Rate limit and window must be positive")
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[float, int]] = {}

    def acquire(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            started, count = self._windows.get(key, (now, 0))
            if now - started >= self._window:
                started, count = now, 0
            if count >= self._limit:
                raise RateLimitExceededError(f"Rate limit exceeded for {key}")
            self._windows[key] = (started, count + 1)


class RequestConcurrencyLimiter:
    def __init__(self, *, per_key_limit: int) -> None:
        if per_key_limit < 1:
            raise ValueError("Concurrency limit must be positive")
        self._limit = per_key_limit
        self._lock = threading.Lock()
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}

    @contextmanager
    def slot(self, key: str, *, timeout_seconds: float = 0) -> Generator[None]:
        with self._lock:
            semaphore = self._semaphores.setdefault(
                key, threading.BoundedSemaphore(self._limit)
            )
        if not semaphore.acquire(timeout=timeout_seconds):
            raise RateLimitExceededError(f"Concurrency limit exceeded for {key}")
        try:
            yield
        finally:
            semaphore.release()


class AdapterMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[str] = Counter()

    def increment(self, name: str, *, value: int = 1) -> None:
        if value < 0:
            raise ValueError("Metric increment cannot be negative")
        with self._lock:
            self._counters[name] += value

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)
