"""Every test runs with its own empty muse-vm state directory, never the real one, and with no
git environment from whoever started the run.

Git runs a pre-commit hook with GIT_DIR, GIT_INDEX_FILE and related variables pointing at the
repository being committed to (in a linked worktree, at that worktree's private directory). A test
that makes a throwaway repository in a temporary folder would inherit them and work on the real
repository instead: re-initialising it, giving it a test identity, emptying its index and
committing to its branch. So they are removed before anything runs. See test_git_isolation.py.
"""

import os
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    del config
    for name in [name for name in os.environ if name.startswith("GIT_")]:
        del os.environ[name]


@pytest.fixture(autouse=True)
def _isolated_muse_vm_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_VM_HOME", str(tmp_path / "muse-vm-home"))
