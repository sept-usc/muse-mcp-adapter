"""Render every Mermaid diagram in README.md and docs/ with the real mermaid-cli.

A diagram with a syntax error renders as an error box on GitHub, so it must never be committed.
"""

import os
import re
import subprocess
import sys
import tempfile

IMAGE = "minlag/mermaid-cli:11.4.2"

files = [n for n in ("README.md", "CONTRIBUTING.md", "SECURITY.md") if os.path.exists(n)] + [f"docs/{n}" for n in sorted(os.listdir("docs")) if n.endswith(".md")]
blocks: list[tuple[str, int, str]] = []
for file in files:
    text = open(file, encoding="utf-8").read()
    for match in re.finditer(r"```mermaid\n(.*?)\n```", text, flags=re.S):
        blocks.append((file, text[: match.start()].count("\n") + 1, match.group(1)))

if not blocks:
    print("no Mermaid diagrams found")
    sys.exit(0)

failures = 0
with tempfile.TemporaryDirectory() as work:
    os.chmod(work, 0o777)
    with open(os.path.join(work, "puppeteer.json"), "w") as handle:
        handle.write('{"executablePath": "/usr/bin/chromium-browser", "args": ["--no-sandbox"]}')
    for number, (file, line, source) in enumerate(blocks, 1):
        name = f"d{number}"
        with open(os.path.join(work, f"{name}.mmd"), "w", encoding="utf-8") as handle:
            handle.write(source)
        done = subprocess.run(
            ["docker", "run", "--rm", "--platform", "linux/amd64", "-v", f"{work}:/data", IMAGE,
             "-p", "/data/puppeteer.json", "-i", f"/data/{name}.mmd", "-o", f"/data/{name}.svg", "-q"],
            capture_output=True, text=True,
        )
        if done.returncode != 0 or not os.path.exists(os.path.join(work, f"{name}.svg")):
            failures += 1
            print(f"{file}:{line}: the diagram does not render\n{done.stderr.strip() or done.stdout.strip()}")
print(f"{len(blocks)} Mermaid diagrams checked, {failures} failed")
sys.exit(1 if failures else 0)
