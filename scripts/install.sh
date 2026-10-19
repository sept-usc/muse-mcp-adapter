#!/usr/bin/env bash
# Install muse-vm and start the guided setup, on a machine that may have an old or no suitable
# Python. Two cases: Google Cloud Shell (to create a VM from there), and the Linux VM the adapter
# should run on (the setup then offers "this machine"). Everything it installs goes in your home
# directory; nothing needs root until the setup installs the adapter itself.
#
#   bash install.sh [muse-vm setup options]      e.g.  --cloud gcp --project my-project
#   bash install.sh --local                      install on this machine, without asking
#
# MUSE_VM_SOURCE overrides what is installed (a path or a pip address); the default is the
# project's repository.
set -euo pipefail

SOURCE="${MUSE_VM_SOURCE:-git+https://github.com/sept-usc/muse-mcp-adapter}"
HOME_DIR="${MUSE_VM_HOME_DIR:-$HOME/.muse-vm-install}"
BIN="$HOME/.local/bin"
mkdir -p "$BIN" "$HOME_DIR"
export PATH="$BIN:$PATH"

python_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; }

install_with_venv() {
  "$1" -m venv "$HOME_DIR/venv"
  "$HOME_DIR/venv/bin/pip" install --quiet --upgrade pip
  "$HOME_DIR/venv/bin/pip" install --quiet "$SOURCE"
  ln -sf "$HOME_DIR/venv/bin/muse-vm" "$BIN/muse-vm"
}

install_with_uv() {
  # No Python 3.12 here: uv fetches one. uv itself comes from PyPI, never from a downloaded script.
  python3 -m venv "$HOME_DIR/uv-venv"
  "$HOME_DIR/uv-venv/bin/pip" install --quiet uv
  UV_TOOL_BIN_DIR="$BIN" "$HOME_DIR/uv-venv/bin/uv" tool install --python 3.12 --force "$SOURCE"
}

if command -v muse-vm >/dev/null 2>&1; then
  echo "muse-vm is already installed: $(command -v muse-vm)"
else
  echo "Installing muse-vm…"
  chosen=""
  for candidate in python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then
      chosen="$candidate"
      break
    fi
  done
  if [ -n "$chosen" ]; then
    install_with_venv "$chosen"
  else
    echo "This machine's Python is older than 3.12: fetching a suitable one with uv."
    install_with_uv
  fi
fi

exec muse-vm setup "$@"
