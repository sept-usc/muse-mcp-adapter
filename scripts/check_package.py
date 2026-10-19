"""Prove that someone who only `pip install`s the release can deploy it.

Builds the wheel, checks what is in it, installs it into a clean environment away from this
checkout, confirms muse-vm finds the build context that travels inside it, and builds both
container images from exactly that context.
"""

import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REQUIRED = [
    "muse_vm/context/Dockerfile",
    "muse_vm/context/requirements.lock",
    "muse_vm/context/guard/Dockerfile",
    "muse_vm/context/guard/guard.sh",
    "muse_vm/bundle/compose.yml",
    "muse_vm/bundle/bootstrap.sh",
    "muse_mcp_adapter/provider_packs/linear.json",
]
FORBIDDEN = ("tests/", ".internal/", "docs/", ".github/", "scripts/")


def run(*argv: str, cwd: str | None = None) -> str:
    done = subprocess.run(argv, capture_output=True, text=True, cwd=cwd)
    if done.returncode != 0:
        print(f"$ {' '.join(argv)}\n{done.stdout}{done.stderr}")
        sys.exit(1)
    return done.stdout


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as work_dir:
        work = Path(work_dir)
        run(sys.executable, "-m", "pip", "wheel", str(root), "--no-deps", "-q", "-w", str(work / "dist"))
        wheel = next((work / "dist").glob("*.whl"))
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            entry_points = next(
                archive.read(n).decode() for n in names if n.endswith("entry_points.txt")
            )
        missing = [item for item in REQUIRED if item not in names]
        stray = [n for n in names if n.startswith(FORBIDDEN)]
        if missing or stray:
            print(f"the wheel is missing {missing} or holds files it should not: {stray[:5]}")
            sys.exit(1)
        if "muse-vm" not in entry_points or "muse-mcp-adapter" not in entry_points:
            print("the wheel does not install both commands")
            sys.exit(1)

        venv = work / "venv"
        run(sys.executable, "-m", "venv", str(venv))
        python = str(venv / "bin" / "python")
        run(python, "-m", "pip", "install", "-q", str(wheel))
        empty = work / "elsewhere"  # no checkout here, so only the installed release can be found
        empty.mkdir()
        context = work / "context"
        probe = f"""
import shutil, sys
from pathlib import Path
from muse_vm import bundle
source = bundle.find_build_source()
assert source is not None and source.kind == "release", source
for path, name in source.files:
    target = Path({str(context)!r}) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    (shutil.copytree if path.is_dir() else shutil.copy)(path, target)
print(source.revision)
"""
        revision = run(python, "-c", probe, cwd=str(empty)).strip()
        run(str(venv / "bin" / "muse-vm"), "--help", cwd=str(empty))
        run("docker", "build", "-q", "-t", "mma-release:check", str(context))
        run("docker", "build", "-q", "-t", "mma-release-guard:check", str(context / "guard"))
        started = run("docker", "run", "--rm", "mma-release:check", "--help")
        assert "usage" in started.lower()
    print(f"the release wheel installs, finds its build context ({revision}) and builds both images")


main()
