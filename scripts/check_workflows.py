"""Every `uses:` in .github/workflows must name a version that exists, pinned to a commit.

Run with GH_TOKEN or a signed-in `gh`. Pinning to a full commit SHA (with the tag in a comment)
means a moved or hijacked tag cannot change what CI runs.
"""

import pathlib
import re
import subprocess
import sys

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
        found = subprocess.run(
            ["gh", "api", f"repos/{owner_repo}/commits/{ref}", "--silent"], capture_output=True
        )
        if found.returncode != 0:
            problems.append(f"{path}:{number}: {repo}@{ref} does not exist")
if problems:
    print("\n".join(problems))
    sys.exit(1)
print("every action is pinned to a commit that exists")
