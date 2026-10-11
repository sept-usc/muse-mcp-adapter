#!/usr/bin/env bash
# Apply the branch ruleset in .github/rulesets/main.json to this repository's default branch:
# changes reach `main` only through a pull request, and only when the `gate` check passes.
# Needs admin rights on the repository, and a plan that supports rulesets (public repositories do).
set -euo pipefail
cd "$(dirname "$0")/.."
repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
existing="$(gh api "repos/$repo/rulesets" --jq '.[] | select(.name=="main") | .id' || true)"
if [ -n "$existing" ]; then
  gh api -X PUT "repos/$repo/rulesets/$existing" --input .github/rulesets/main.json --silent
  echo "updated the 'main' ruleset on $repo"
else
  gh api -X POST "repos/$repo/rulesets" --input .github/rulesets/main.json --silent
  echo "created the 'main' ruleset on $repo"
fi
