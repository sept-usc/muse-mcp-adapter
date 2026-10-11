#!/usr/bin/env bash
# Fail if anything that looks like a credential is tracked. Shared by the local gate and CI.
set -euo pipefail
cd "$(dirname "$0")/.."
if git grep -n -I -E 'github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{30,}|tskey-(auth|api)-[A-Za-z0-9]{8,}-[A-Za-z0-9]{12,}|mcpa_(at_|rt_)?[A-Za-z0-9_-]{30,}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY' -- . ':!tests'; then
  echo "something that looks like a credential is tracked" >&2
  exit 1
fi
# Names that must never be in the public repo (accounts, projects, hostnames) go, one regular
# expression per line, in .internal/private-patterns. That file is not tracked, so it cannot
# leak them itself; CI has no such file and only runs the credential check above.
if [ -f .internal/private-patterns ]; then
  if git grep -n -I -i -E -f .internal/private-patterns -- . ':!tests'; then
    echo "a private account, project or hostname is tracked" >&2
    exit 1
  fi
else
  echo "note: no .internal/private-patterns, so private names were not checked"
fi
echo "no credentials or private names are tracked"
