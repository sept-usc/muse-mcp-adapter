"""A test run inside the commit hook must leave the repository it is committing to alone.

Git runs a pre-commit hook with GIT_DIR, GIT_INDEX_FILE and related variables pointing at the
repository being committed to (in a linked worktree, at that worktree's private directory). Every
`git` a test starts inherits them, so a throwaway repository made in a temporary folder was in fact
the real one: it was re-initialised (the main checkout became "bare"), given a test identity that
later commits then carried, had its index emptied, and received a commit named "x" on the branch.
The gate runs in the hook, so this is the one place the whole suite must be safe.

These tests recreate the hook's environment against a disposable repository and check that
nothing in it moves.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REVISION_TEST = (
    "tests/unit/test_vm_bundle.py::test_the_source_revision_names_the_commit_and_notices_edits"
)
THIS_FILE = "tests/unit/test_git_isolation.py"


def _clean() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=v@example.com", "-c", "user.name=victim", *args],
        check=True,
        capture_output=True,
        text=True,
        env=_clean(),
    )
    return done.stdout


def _snapshot(repo: Path) -> dict[str, str]:
    """Everything a stray git command in the hook's environment was seen to change."""
    return {
        "head": _git(repo, "rev-parse", "HEAD"),
        "branches": _git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
        "history": _git(repo, "log", "--format=%H %an %s"),
        "index": _git(repo, "ls-files", "-s"),
        "config": (repo / ".git" / "config").read_text(),
    }


def test_no_git_variable_reaches_a_test() -> None:
    assert [name for name in os.environ if name.startswith("GIT_")] == []


def test_a_run_in_the_hooks_environment_leaves_the_repository_alone(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    _git(victim, "init", "-q")
    (victim / "kept.txt").write_text("kept\n")
    _git(victim, "add", "-A")
    _git(victim, "commit", "-qm", "the only commit")
    before = _snapshot(victim)
    gitdir = victim / ".git"

    hostile = {
        **os.environ,
        "GIT_DIR": str(gitdir),
        "GIT_WORK_TREE": str(victim),
        "GIT_INDEX_FILE": str(gitdir / "index"),
    }
    ran = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            REVISION_TEST,
            f"{THIS_FILE}::test_no_git_variable_reaches_a_test",
        ],
        cwd=ROOT,
        env=hostile,
        capture_output=True,
        text=True,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "2 passed" in ran.stdout
    assert _snapshot(victim) == before
    assert (victim / "kept.txt").read_text() == "kept\n"
    assert sorted(path.name for path in victim.iterdir()) == [".git", "kept.txt"]
