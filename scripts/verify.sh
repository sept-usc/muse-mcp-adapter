#!/usr/bin/env bash
# The local gate: everything CI runs, run here first, on Linux with the same Python version.
# A commit is allowed only if every step passes. There are no skips: if Docker is not running,
# the gate fails rather than passing without it.
#
#   scripts/verify.sh            run the whole gate
#   scripts/verify.sh --quick    lint, types and unit tests on this machine only (for iterating;
#                                the commit hook always runs the whole gate)
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON_IMAGE="python:3.12-bookworm"     # what CI's setup-python "3.12" on ubuntu gives us
ACTIONLINT_IMAGE="rhysd/actionlint:1.7.7"
TRIVY_IMAGE="aquasec/trivy:0.58.1"
quick=0; [ "${1:-}" = "--quick" ] && quick=1

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
fail() { printf '\n\033[31mFAILED: %s\033[0m\n' "$1" >&2; exit 1; }

if [ "$quick" = 1 ]; then
  step "lint";  .venv/bin/ruff check src tests || fail "ruff"
  step "types"; .venv/bin/pyright || fail "pyright"
  step "tests"; .venv/bin/pytest -q || fail "pytest"
  echo; echo "quick checks passed (this is NOT the commit gate)"; exit 0
fi

step "preconditions"
docker info >/dev/null 2>&1 || fail "Docker is not running (the gate needs it; start OrbStack or Docker Desktop)"
gh auth status >/dev/null 2>&1 || fail "gh is not signed in (needed to check that every GitHub Action version exists)"
git diff --quiet --exit-code -- . ':!.internal' || echo "note: unstaged changes are included in these checks"

step "no secrets or private details in tracked files"
if git grep -n -I -E 'github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{30,}|tskey-(auth|api)-[A-Za-z0-9]{8,}-[A-Za-z0-9]{12,}|mcpa_(at_|rt_)?[A-Za-z0-9_-]{30,}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY' -- . ':!tests'; then
  fail "something that looks like a credential is tracked"
fi
# Account names, projects and hostnames you never want in the public repo go, one regular
# expression per line, in .internal/private-patterns (not tracked, so it cannot leak them itself).
if [ -f .internal/private-patterns ]; then
  if git grep -n -I -i -E -f .internal/private-patterns -- . ':!tests'; then
    fail "a private account, project or hostname is tracked"
  fi
else
  echo "note: no .internal/private-patterns, so private names were not checked"
fi

step "GitHub workflows: syntax, and every action version exists"
docker run --rm -v "$PWD":/repo -w /repo "$ACTIONLINT_IMAGE" -color || fail "actionlint"
python3 - <<'PY' || fail "an action reference does not resolve"
import re, subprocess, sys, pathlib
bad = []
for path in sorted(pathlib.Path(".github/workflows").glob("*.yml")):
    for number, line in enumerate(path.read_text().splitlines(), 1):
        match = re.search(r"uses:\s*([\w.-]+/[\w./-]+)@([\w.-]+)", line)
        if not match:
            continue
        repo, ref = match.group(1), match.group(2)
        owner_repo = "/".join(repo.split("/")[:2])
        for kind in ("tags", "heads"):
            found = subprocess.run(
                ["gh", "api", f"repos/{owner_repo}/git/ref/{kind}/{ref}", "--silent"],
                capture_output=True,
            )
            if found.returncode == 0:
                break
        else:
            bad.append(f"{path}:{number}: {repo}@{ref} does not exist")
if bad:
    print("\n".join(bad)); sys.exit(1)
print("all action references resolve")
PY

step "lint, types and the full test suite, on Linux with Python 3.12 (as CI does)"
volume=muse-verify-pip
docker volume create "$volume" >/dev/null
COPYFILE_DISABLE=1 tar --exclude='._*' --exclude=.DS_Store --exclude=.git --exclude=.venv --exclude=.internal --exclude=__pycache__ --exclude=.pytest_cache \
    --exclude=.ruff_cache --exclude=reports -cf - . | docker run --rm -i \
  -v "$volume":/root/.cache/pip "$PYTHON_IMAGE" bash -euo pipefail -c '
    mkdir /work && tar -xf - -C /work && cd /work
    git init -q && git config user.email gate@example.com && git config user.name gate
    pip install -q -e ".[dev]"
    echo "-- ruff";    ruff check src tests
    echo "-- pyright"; pyright
    echo "-- pytest";  pytest -q
  ' || fail "lint, types or tests failed on Linux/Python 3.12"

step "this machine's own virtualenv: the same checks (catches a newer Python's differences)"
.venv/bin/ruff check src tests || fail "ruff (local)"
.venv/bin/pyright || fail "pyright (local)"
.venv/bin/pytest -q || fail "pytest (local)"

step "the images build"
docker build -q -t mma-adapter:verify . >/dev/null || fail "adapter image build"
docker build -q -t mma-guard:verify deploy/guard >/dev/null || fail "guard image build"
docker run --rm --entrypoint id mma-adapter:verify -u | grep -qx 10001 || fail "adapter image does not run as uid 10001"
docker run --rm mma-adapter:verify --help >/dev/null || fail "adapter image does not start"

step "no known high or critical vulnerabilities in the images (as CI scans them)"
for image in mma-adapter:verify mma-guard:verify; do
  docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v muse-verify-trivy:/root/.cache \
    "$TRIVY_IMAGE" image --quiet --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 "$image" \
    || fail "vulnerabilities in $image"
done

step "muse-vm and the documentation"
.venv/bin/python -m muse_vm --help >/dev/null || fail "muse-vm does not start"
python3 scripts/check_docs.py || fail "documentation links"
step "every Mermaid diagram renders"
python3 scripts/check_mermaid.py || fail "a Mermaid diagram does not render"

echo; printf '\033[32mAll checks passed. Safe to commit.\033[0m\n'
