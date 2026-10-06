# pyright: basic
"""Release gate: no MCP capability may be silently lost between server and connector.

Named to run last so it sees every scenario the other suites recorded.
"""

import pytest

from tests.validation.parity import ParityLog, Verdict

pytestmark = pytest.mark.validation


def test_no_capability_is_unsupported(parity_log: ParityLog) -> None:
    if not parity_log.observations:
        pytest.skip("run together with the parity suites to evaluate the gate")
    lost = [o.scenario for o in parity_log.observations if o.verdict is Verdict.UNSUPPORTED]
    assert lost == [], f"capabilities the connector does not carry: {lost}"
