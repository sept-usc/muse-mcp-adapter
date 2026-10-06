"""Shared state of an Atlas server: the data, the ledger of side effects, and injected faults."""

from __future__ import annotations

import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Fault:
    """Misbehaviour to inject into one tool."""

    fail_first: int = 0  # raise an error for the first N calls
    delay_seconds: float = 0.0  # sleep before doing anything
    drop_before_effect: bool = False  # kill the HTTP response before the tool runs
    drop_after_effect: bool = False  # run the tool, then kill the HTTP response
    error_message: str = "Injected failure"
    drop_times: int = 1  # how many calls to drop (then behave normally)


@dataclass(frozen=True)
class Effect:
    sequence: int
    tool: str
    detail: dict[str, Any]
    at: float


@dataclass
class Issue:
    id: str
    title: str
    body: str = ""
    status: str = "open"
    labels: list[str] = field(default_factory=lambda: [])
    comments: list[str] = field(default_factory=lambda: [])


class AtlasState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.issues: dict[str, Issue] = {
                "ISSUE-1": Issue("ISSUE-1", "Login fails on Safari", labels=["bug", "web"]),
                "ISSUE-2": Issue("ISSUE-2", "Export is slow", status="closed", labels=["perf"]),
                "ISSUE-3": Issue("ISSUE-3", "Dark mode request", labels=["feature"]),
            }
            self.effects: list[Effect] = []
            self.calls: Counter[str] = Counter()
            self.faults: dict[str, Fault] = {}
            self.flaky: Counter[str] = Counter()
            self.drops: Counter[str] = Counter()
            self.profile = "v1"
            self.clock = 0

    # --- ledger ------------------------------------------------------------------------

    def effect(self, tool: str, **detail: Any) -> Effect:
        with self._lock:
            item = Effect(len(self.effects) + 1, tool, detail, time.time())
            self.effects.append(item)
            return item

    def ledger(self) -> list[dict[str, Any]]:
        with self._lock:
            return [asdict(item) for item in self.effects]

    def count_effects(self, tool: str) -> int:
        with self._lock:
            return sum(1 for item in self.effects if item.tool == tool)

    def next_issue_id(self) -> str:
        with self._lock:
            number = max((int(key.split("-")[1]) for key in self.issues), default=0) + 1
            return f"ISSUE-{number}"

    def tick(self) -> int:
        with self._lock:
            self.clock += 1
            return self.clock

    # --- faults ------------------------------------------------------------------------

    def set_faults(self, faults: dict[str, dict[str, Any]]) -> None:
        with self._lock:
            self.faults = {tool: Fault(**values) for tool, values in faults.items()}
            self.drops.clear()

    def should_fail(self, tool: str) -> Fault | None:
        """The fault to apply to this call as an error, or None."""
        with self._lock:
            self.calls[tool] += 1
            fault = self.faults.get(tool)
            if fault is not None and self.calls[tool] <= fault.fail_first:
                return fault
            return None

    def delay_for(self, tool: str) -> float:
        with self._lock:
            fault = self.faults.get(tool)
            return fault.delay_seconds if fault else 0.0

    def should_drop(self, tool: str, *, after_effect: bool) -> bool:
        with self._lock:
            fault = self.faults.get(tool)
            if fault is None:
                return False
            wanted = fault.drop_after_effect if after_effect else fault.drop_before_effect
            key = f"{tool}:{'after' if after_effect else 'before'}"
            if wanted and self.drops[key] < fault.drop_times:
                self.drops[key] += 1
                return True
            return False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "profile": self.profile,
                "calls": dict(self.calls),
                "effects": len(self.effects),
                "issues": {key: asdict(issue) for key, issue in self.issues.items()},
                "faults": {tool: asdict(fault) for tool, fault in self.faults.items()},
            }
