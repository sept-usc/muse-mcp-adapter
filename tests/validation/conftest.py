# pyright: basic
"""Shared fixtures for the validation suite."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Protocol

import pytest

from tests.validation.env import AtlasHandle, ConnectorHandle, atlas_server, connector
from tests.validation.parity import Observation, ParityLog, Verdict

REPORT = Path(__file__).parents[2] / "reports" / "mcp-parity.md"


@pytest.fixture(scope="session")
def parity_log() -> Generator[ParityLog]:
    log = ParityLog()
    yield log
    if log.observations:
        log.write(REPORT)


class Recorder(Protocol):
    def __call__(
        self,
        scenario: str,
        area: str,
        verdict: Verdict,
        direct: object,
        via: object,
        note: str = "",
        standard: str | None = None,
    ) -> None: ...


@pytest.fixture
def record(parity_log: ParityLog) -> Recorder:
    def add(
        scenario: str,
        area: str,
        verdict: Verdict,
        direct: object,
        via: object,
        note: str = "",
        standard: str | None = None,
    ) -> None:
        parity_log.add(
            Observation(scenario, area, verdict, _short(direct), _short(via), note, standard)
        )

    return add


def _short(value: object) -> str:
    text = str(value).replace("\n", " ")
    return text if len(text) <= 90 else text[:87] + "..."


@pytest.fixture(scope="module")
def atlas() -> Generator[AtlasHandle]:
    with atlas_server() as handle:
        yield handle


@pytest.fixture(scope="module")
def conn(
    atlas: AtlasHandle, tmp_path_factory: pytest.TempPathFactory
) -> Generator[ConnectorHandle]:
    with connector(tmp_path_factory.mktemp("parity"), atlas) as handle:
        yield handle
