#!/usr/bin/env bash
# Every shell script in the repository must pass shellcheck (run in a container, pinned).
set -euo pipefail
cd "$(dirname "$0")/.."
files="$(git ls-files '*.sh' .githooks/pre-commit)"
# shellcheck disable=SC2086  # the list is newline-separated file names without spaces
docker run --rm -v "$PWD":/repo -w /repo koalaman/shellcheck:v0.10.0 -x $files
echo "shellcheck: $(echo "$files" | wc -l | tr -d ' ') scripts are clean"
