"""Every relative link and heading anchor in README.md and docs/ must resolve."""

import os
import re
import sys


def slug(heading: str) -> str:
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower().replace("`", ""))
    return text.replace(" ", "-")


def anchors(path: str) -> set[str]:
    found: set[str] = set()
    in_code = False
    for line in open(path, encoding="utf-8"):
        if line.startswith("```"):
            in_code = not in_code
        match = None if in_code else re.match(r"^#{1,6}\s+(.*)$", line)
        if match:
            found.add(slug(match.group(1)))
    return found


files = [n for n in ("README.md", "CONTRIBUTING.md", "SECURITY.md") if os.path.exists(n)] + [f"docs/{name}" for name in sorted(os.listdir("docs")) if name.endswith(".md")]
problems: list[str] = []
for file in files:
    text = open(file, encoding="utf-8").read()
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    for match in re.finditer(r"\]\(([^)#\s]*)(#[^)\s]*)?\)", text):
        path, fragment = match.group(1), (match.group(2) or "")[1:]
        if path.startswith(("http://", "https://", "mailto:")):
            continue
        target = os.path.normpath(os.path.join(os.path.dirname(file), path)) if path else file
        if not os.path.exists(target):
            problems.append(f"{file}: missing file {path}")
        elif fragment and target.endswith(".md") and fragment not in anchors(target):
            problems.append(f"{file}: no heading #{fragment} in {target}")
if problems:
    print("\n".join(problems))
    sys.exit(1)
print(f"{len(files)} documents: all links and anchors resolve")
