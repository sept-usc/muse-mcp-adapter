"""Every test runs with its own empty muse-vm state directory, never the real one."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_muse_vm_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_VM_HOME", str(tmp_path / "muse-vm-home"))
