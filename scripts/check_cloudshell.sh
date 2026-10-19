#!/usr/bin/env bash
# The Cloud Shell helper must work where Python is older than 3.12 (the hard case, which has to
# fetch a Python) and where it is new enough. Run in clean containers, from this checkout.
set -euo pipefail
cd "$(dirname "$0")/.."
for image in python:3.11-bookworm python:3.12-bookworm; do
  echo "-- $image"
  out="$(COPYFILE_DISABLE=1 tar --exclude='._*' --exclude=.git --exclude=.venv --exclude=.internal \
      --exclude=__pycache__ -cf - . | docker run --rm -i "$image" bash -euo pipefail -c '
    mkdir /work && tar --warning=no-unknown-keyword -xf - -C /work && cd /work
    useradd -m -s /bin/bash shelluser && chown -R shelluser /work
    su shelluser -c "MUSE_VM_SOURCE=/work bash scripts/cloudshell-setup.sh --help"
  ')"
  echo "$out" | grep -q "usage: muse-vm setup" || { echo "$out"; echo "the helper did not reach muse-vm setup on $image" >&2; exit 1; }
done
echo "the Cloud Shell helper works with an old and a new Python"
