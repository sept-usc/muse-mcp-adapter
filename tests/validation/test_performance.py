# pyright: basic
"""What the connector costs: latency added over calling the MCP server directly."""

import statistics
import time
from pathlib import Path

import pytest

from tests.validation.env import AtlasHandle, ConnectorHandle

pytestmark = [pytest.mark.validation, pytest.mark.performance]
REPORT = Path(__file__).parents[2] / "reports" / "performance.md"
SAMPLES = 25
# The connector opens an MCP session per call, as the direct client does; the budget covers
# validation, policy, persistence and serialisation on top of that.
P95_OVERHEAD_BUDGET_MS = 250


def _timed(call, count: int = SAMPLES) -> list[float]:  # type: ignore[no-untyped-def]
    call()  # warm up
    samples = []
    for _ in range(count):
        start = time.perf_counter()
        call()
        samples.append((time.perf_counter() - start) * 1000)
    return sorted(samples)


def _percentile(samples: list[float], fraction: float) -> float:
    return samples[min(len(samples) - 1, int(len(samples) * fraction))]


def test_the_connector_adds_a_bounded_delay(world: tuple[AtlasHandle, ConnectorHandle]) -> None:
    atlas, conn = world
    direct = atlas.direct()
    rows = []
    for name, arguments in (
        ("get_issue (read)", {"issue_id": "ISSUE-1"}),
        ("handbook (large text)", {}),
    ):
        tool = name.split()[0]
        direct_ms = _timed(lambda tool=tool, arguments=arguments: direct.call(tool, arguments))
        via_ms = _timed(lambda tool=tool, arguments=arguments: conn.call(tool, arguments))
        rows.append((name, direct_ms, via_ms))

    counter = iter(range(10_000))
    write_direct = _timed(lambda: direct.call("create_issue", {"title": "perf"}))
    write_via = _timed(
        lambda: conn.call(
            "create_issue", {"title": "perf"}, **{"Idempotency-Key": f"p{next(counter)}"}
        )
    )
    rows.append(("create_issue (write)", write_direct, write_via))

    lines = [
        "# Connector overhead",
        "",
        f"{SAMPLES} calls each, in milliseconds; overhead is connector time minus direct time.",
        "",
        "| Call | Direct p50 | Connector p50 | Overhead p50 "
        "| Direct p95 | Connector p95 | Overhead p95 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    worst = 0.0
    for name, d, v in rows:
        p50 = (statistics.median(d), statistics.median(v))
        p95 = (_percentile(d, 0.95), _percentile(v, 0.95))
        worst = max(worst, p95[1] - p95[0])
        lines.append(
            f"| {name} | {p50[0]:.1f} | {p50[1]:.1f} | {p50[1] - p50[0]:.1f} "
            f"| {p95[0]:.1f} | {p95[1]:.1f} | {p95[1] - p95[0]:.1f} |"
        )
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n")
    assert worst < P95_OVERHEAD_BUDGET_MS, f"p95 overhead {worst:.0f} ms exceeds the budget"
