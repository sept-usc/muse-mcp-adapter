# pyright: basic
"""When Muse sends no Idempotency-Key, an identical write is a retry only while it could be one."""

from types import SimpleNamespace
from typing import Any

import pytest

from muse_mcp_adapter.application.runtime import AdapterRuntime, Principal
from muse_mcp_adapter.domain.errors import RequestValidationError

NOW = "2026-10-10T12:00:00+00:00"
PRINCIPAL = Principal(connection_id="c1", identity_id="i1", server_id="s1", slug="slug")


def _runtime(earlier: dict[int, tuple[str, str]], window: int = 30) -> AdapterRuntime:
    """A runtime whose store holds the given earlier operations, by generation: (status, time)."""

    class Store:
        def find_operation_by_idempotency(self, **kwargs: Any) -> Any:
            generation = int(str(kwargs["idempotency_key"]).rsplit("-", 1)[1])
            if generation not in earlier:
                return None
            status, updated_at = earlier[generation]
            return SimpleNamespace(status=status, updated_at=updated_at)

    runtime = object.__new__(AdapterRuntime)
    runtime._repository = Store()  # type: ignore[assignment]
    runtime._clock = lambda: NOW
    runtime._window = window
    return runtime


def _key(runtime: AdapterRuntime) -> str:
    return runtime._derive_key(PRINCIPAL, "create_issue", {"title": "x"})  # type: ignore[reportPrivateUsage]


def _generation(key: str) -> int:
    return int(key.rsplit("-", 1)[1])


def test_a_first_request_starts_the_first_generation() -> None:
    assert _generation(_key(_runtime({}))) == 0


def test_an_identical_request_right_after_completion_is_a_replay() -> None:
    assert _generation(_key(_runtime({0: ("COMPLETED", "2026-10-10T11:59:55+00:00")}))) == 0


def test_an_identical_request_after_the_window_is_a_new_action() -> None:
    assert _generation(_key(_runtime({0: ("COMPLETED", "2026-10-10T11:58:00+00:00")}))) == 1


def test_a_request_still_in_flight_or_of_unknown_outcome_is_never_run_again() -> None:
    old = "2026-10-10T08:00:00+00:00"
    for status in ("PENDING", "RUNNING", "UNKNOWN", "INPUT_REQUIRED"):
        assert _generation(_key(_runtime({0: (status, old)}))) == 0, status


def test_a_failed_request_is_safe_to_run_again() -> None:
    assert _generation(_key(_runtime({0: ("FAILED", "2026-10-10T11:59:59+00:00")}))) == 1


def test_generations_chain_and_the_latest_decides() -> None:
    chain = {
        0: ("COMPLETED", "2026-10-10T11:00:00+00:00"),
        1: ("COMPLETED", "2026-10-10T11:59:50+00:00"),
    }
    assert _generation(_key(_runtime(chain))) == 1  # the latest finished 10s ago: a replay
    chain[1] = ("COMPLETED", "2026-10-10T11:30:00+00:00")
    assert _generation(_key(_runtime(chain))) == 2


def test_the_key_depends_on_the_request_and_the_connection() -> None:
    runtime = _runtime({})
    other = runtime._derive_key(PRINCIPAL, "create_issue", {"title": "y"})  # type: ignore[reportPrivateUsage]
    assert other.rsplit("-", 1)[0] != _key(runtime).rsplit("-", 1)[0]
    stranger = Principal(connection_id="c2", identity_id="i1", server_id="s1", slug="slug")
    assert (
        runtime._derive_key(stranger, "create_issue", {"title": "x"}).rsplit("-", 1)[0]  # type: ignore[reportPrivateUsage]
        != _key(runtime).rsplit("-", 1)[0]
    )


def test_a_runaway_number_of_generations_is_refused_instead_of_looping_forever() -> None:
    class Endless:
        def find_operation_by_idempotency(self, **kwargs: Any) -> Any:
            return SimpleNamespace(status="FAILED", updated_at=NOW)

    runtime = _runtime({})
    runtime._repository = Endless()  # type: ignore[assignment]
    with pytest.raises(RequestValidationError):
        _key(runtime)
