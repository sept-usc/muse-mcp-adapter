#!/usr/bin/env bash
# The installer must work where Python is older than 3.12 (the hard case, which has to
# fetch a Python) and where it is new enough. Run in clean containers, from this checkout.
set -euo pipefail
cd "$(dirname "$0")/.."
for image in python:3.11-bookworm python:3.12-bookworm; do
  echo "-- $image"
  out="$(COPYFILE_DISABLE=1 tar --exclude='._*' --exclude=.git --exclude=.venv --exclude=.internal \
      --exclude=__pycache__ -cf - . | docker run --rm -i "$image" bash -euo pipefail -c '
    mkdir /work && tar --warning=no-unknown-keyword -xf - -C /work && cd /work
    useradd -m -s /bin/bash shelluser && chown -R shelluser /work
    su shelluser -c "MUSE_VM_SOURCE=/work bash scripts/install.sh --help"
  ')"
  echo "$out" | grep -q "usage: muse-vm setup" || { echo "$out"; echo "the installer did not reach muse-vm setup on $image" >&2; exit 1; }
done
# A stock Ubuntu: no python3 at all, a user with passwordless sudo: it must install what it needs.
echo "-- ubuntu:24.04 (no python, user with sudo)"
out="$(COPYFILE_DISABLE=1 tar --exclude='._*' --exclude=.git --exclude=.venv --exclude=.internal \
    --exclude=__pycache__ -cf - . | docker run --rm -i ubuntu:24.04 bash -euo pipefail -c '
  apt-get update -q >/dev/null && apt-get install -y -q sudo curl ca-certificates >/dev/null
  mkdir /work && tar --warning=no-unknown-keyword -xf - -C /work
  useradd -m -s /bin/bash shelluser && echo "shelluser ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/shelluser
  chown -R shelluser /work
  su shelluser -c "MUSE_VM_SOURCE=/work bash /work/scripts/install.sh --help"
' 2>&1)"
echo "$out" | grep -q "usage: muse-vm setup" || { echo "$out" | tail -15; echo "the installer did not reach muse-vm setup on a stock ubuntu" >&2; exit 1; }
echo "the installer works with an old and a new Python, and on a stock Ubuntu"
