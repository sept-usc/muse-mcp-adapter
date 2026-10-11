"""Every `uses:` in .github/workflows must name a version that exists, pinned to a commit.

Checked over plain git first; the GitHub API (GH_TOKEN or a signed-in `gh`) is the fallback. Pinning to a full commit SHA (with the tag in a comment)
means a moved or hijacked tag cannot change what CI runs.
"""

import os
import pathlib
import re
import subprocess
import sys
import time



_refs: dict[str, str] = {}


def published_refs(owner_repo: str) -> str:
    """Every branch and tag commit of a repository, over plain git (no API, so no token limits)."""
    if owner_repo not in _refs:
        text = ""
        for attempt in range(4):
            done = subprocess.run(
                ["git", "ls-remote", f"https://github.com/{owner_repo}.git"],
                capture_output=True,
                text=True,
                env={"GIT_TERMINAL_PROMPT": "0", "PATH": os.environ["PATH"]},
            )
            if done.returncode == 0:
                text = done.stdout
                break
            time.sleep(2 * (attempt + 1))
        _refs[owner_repo] = text
    return _refs[owner_repo]


def exists(owner_repo: str, ref: str) -> str:
    """"found", "missing" (a definite 404) or "unknown" (the API failed every attempt).

    A network or API hiccup must not be mistaken for a missing action, and must not make a
    required check flaky, so anything other than a 404 is retried.
    """
    if ref in published_refs(owner_repo):
        return "found"  # the commit is the tip of a published branch or tag: a real release
    # Some organisations restrict API access from other organisations' tokens, so the API is only
    # the fallback, for a commit that is not a branch or tag tip.
    last = ""
    for attempt in range(5):
        done = subprocess.run(
            ["gh", "api", f"repos/{owner_repo}/commits/{ref}", "--silent"],
            capture_output=True,
            text=True,
        )
        if done.returncode == 0:
            return "found"
        if "404" in done.stderr or "Not Found" in done.stderr:
            return "missing"
        last = done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "no message"
        time.sleep(2 * (attempt + 1))
    return "unknown: " + last


problems: list[str] = []
for path in sorted(pathlib.Path(".github/workflows").glob("*.yml")):
    for number, line in enumerate(path.read_text().splitlines(), 1):
        match = re.search(r"uses:\s*([\w.-]+/[\w./-]+)@([\w.-]+)", line)
        if not match:
            continue
        repo, ref = match.group(1), match.group(2)
        owner_repo = "/".join(repo.split("/")[:2])
        if not re.fullmatch(r"[0-9a-f]{40}", ref):
            problems.append(f"{path}:{number}: {repo}@{ref} is not pinned to a commit SHA")
            continue
        outcome = exists(owner_repo, ref)
        if outcome == "missing":
            problems.append(f"{path}:{number}: {repo}@{ref} does not exist")
        elif outcome.startswith("unknown"):
            problems.append(f"{path}:{number}: could not check {repo}@{ref} ({outcome})")
if problems:
    print("\n".join(problems))
    sys.exit(1)
print("every action is pinned to a commit that exists")
