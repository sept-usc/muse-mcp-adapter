"""Every test runs with its own empty muse-vm state directory, never the real one."""

from pathlib import Path

import pytest

# What git passes to a hook so that every child command works on the repository being committed.
# A test that runs `git` in a temporary folder must never inherit it: with an absolute GIT_DIR (a
# worktree) a "throwaway" repository was in fact the real one, and a test committed to it.
_GIT_REPOSITORY_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
    "GIT_NAMESPACE",
    "GIT_QUARANTINE_PATH",
)


@pytest.fixture(autouse=True)
def _tests_never_touch_the_real_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _GIT_REPOSITORY_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolated_muse_vm_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_VM_HOME", str(tmp_path / "muse-vm-home"))
