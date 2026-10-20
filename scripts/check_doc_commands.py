"""Every `muse-vm` command shown in the documentation must parse against the real command line.

A flag that was renamed or removed, or a command that never existed, fails here instead of failing
for a user who copied it. Examples with placeholders (<name>, ...) are checked with the placeholder
replaced by a plain word.
"""

import re
import shlex
import sys
from pathlib import Path

from muse_vm.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
files = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", *sorted((ROOT / "docs").glob("*.md"))]
failures: list[str] = []
checked = 0
parser = build_parser()


def shell_blocks(text: str) -> list[str]:
    """The contents of bash/sh/console code blocks, found by tracking the fences line by line."""
    blocks: list[str] = []
    current: list[str] | None = None
    keep = False
    for line in text.splitlines():
        if line.startswith("```"):
            if current is None:
                current, keep = [], line[3:].strip() in ("", "bash", "sh", "console")
            else:
                if keep:
                    blocks.append("\n".join(current) + "\n")
                current = None
        elif current is not None:
            current.append(line)
    return blocks


for path in files:
    text = path.read_text(encoding="utf-8")
    for block in shell_blocks(text):
        joined = re.sub(r"\\\n\s*", " ", block)
        for raw in joined.splitlines():
            line = raw.strip().lstrip("$ ").split("  #")[0].split(" # ")[0].strip()
            match = re.search(r"(?:^|\s)(?:\S*/)?muse-vm\s+(.*)$", line)
            if not match or line.startswith("#"):
                continue
            arguments = match.group(1).strip()
            arguments = re.sub(r"<[^>]+>", "placeholder", arguments)
            arguments = arguments.replace("…", "").replace("...", "")
            try:
                words = shlex.split(arguments)
            except ValueError:
                continue
            if not words or words[0].startswith("-") and words[0] not in ("--help", "--version"):
                continue
            checked += 1
            try:
                parser.parse_args(words)
            except SystemExit as stop:
                if stop.code not in (0, None):
                    failures.append(f"{path.name}: muse-vm {arguments}")
if failures:
    print("commands in the documentation that the command line does not accept:")
    print("\n".join(f"  {item}" for item in failures))
    sys.exit(1)
print(f"{checked} muse-vm commands in the documentation all parse")
