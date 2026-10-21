"""The suite must not be able to reach the repository it is being run from."""

import os
import subprocess
from pathlib import Path


def test_the_hooks_repository_variables_are_not_visible_to_tests() -> None:
    assert not [
        name for name in os.environ if name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")
    ]


def test_a_throwaway_repository_in_a_temp_folder_is_its_own_repository(tmp_path: Path) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com"}
    env["GIT_COMMITTER_NAME"], env["GIT_COMMITTER_EMAIL"] = "t", "t@example.com"

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True, env=env
        )
        return done.stdout.strip()

    git("init", "-q")
    (tmp_path / "file").write_text("x")
    git("add", "-A")
    git("commit", "-qm", "x")
    assert Path(git("rev-parse", "--show-toplevel")).resolve() == tmp_path.resolve()
