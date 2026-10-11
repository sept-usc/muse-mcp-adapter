"""Every `uses:` in .github/workflows must name a version that exists, pinned to a commit.

Run with GH_TOKEN or a signed-in `gh`. Pinning to a full commit SHA (with the tag in a comment)
means a moved or hijacked tag cannot change what CI runs.
"""

import pathlib
import re
import subprocess
import sys
import time



def exists(owner_repo: str, ref: str) -> str:
    """"found", "missing" (a definite 404) or "unknown" (the API failed every attempt).

    A network or API hiccup must not be mistaken for a missing action, and must not make a
    required check flaky, so anything other than a 404 is retried.
    """
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
