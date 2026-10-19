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
git diff --quiet --exit-code -- . ':!.internal' || echo "note: unstaged changes are included in these checks"

step "no credentials or private names in tracked files"
scripts/check_secrets.sh || fail "a credential or private name is tracked"

step "GitHub workflows: syntax, and every action is pinned to a commit that exists"
docker run --rm -v "$PWD":/repo -w /repo "$ACTIONLINT_IMAGE" -color || fail "actionlint"
python3 scripts/check_workflows.py || fail "an action reference is not pinned or does not exist"

step "shell scripts are clean"
scripts/check_shell.sh || fail "shellcheck"

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

step "the release wheel installs on its own and builds the images from what it carries"
python3 scripts/check_package.py || fail "the release wheel does not work on its own"

step "the installer works with an old and a new Python"
scripts/check_install.sh || fail "the installer"

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
