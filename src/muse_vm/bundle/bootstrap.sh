#!/bin/sh
# Prepare a Debian or Ubuntu host for the adapter, changing as little as possible.
#
#   - Docker with the compose plugin: installed only when it is missing.
#   - Automatic security updates: enabled only when the host has no unattended-upgrades yet, and an
#     existing configuration is never overwritten.
#   - Container log rotation in /etc/docker/daemon.json, and the Docker restart that applies it:
#     only on a host where this script just installed Docker, so there is nothing to disturb. The
#     adapter's own containers rotate their logs through their compose file either way.
#
# Safe to run again. Used as the cloud VM's startup script and run over SSH by `muse-vm up --ssh`.
set -eu

if ! command -v apt-get >/dev/null 2>&1; then
  echo "muse-vm bootstrap: this needs apt (Debian or Ubuntu). Install Docker Engine with the compose plugin and run again." >&2
  exit 3
fi
export DEBIAN_FRONTEND=noninteractive

installed_docker=no
if ! docker compose version >/dev/null 2>&1; then
  apt-get update -q
  apt-get install -y -q docker.io
  # Ubuntu ships the plugin as docker-compose-v2, Debian 13 as docker-compose.
  apt-get install -y -q docker-compose-v2 || apt-get install -y -q docker-compose
  installed_docker=yes
fi

# Security updates install themselves; a long-lived VM that never patches is the usual way in.
if ! dpkg -s unattended-upgrades >/dev/null 2>&1; then
  apt-get install -y -q unattended-upgrades
  if [ ! -e /etc/apt/apt.conf.d/20auto-upgrades ]; then
    cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF
  fi
fi

if [ "$installed_docker" = yes ]; then
  mkdir -p /etc/docker
  python3 - <<'PY'
import json, os
path = "/etc/docker/daemon.json"
current = {}
if os.path.exists(path):
    try:
        current = json.load(open(path))
    except ValueError:
        current = {}
current.setdefault("log-driver", "json-file")
options = current.setdefault("log-opts", {})
options.setdefault("max-size", "10m")
options.setdefault("max-file", "5")
current.setdefault("live-restore", True)
json.dump(current, open(path, "w"), indent=2)
PY
  systemctl enable docker >/dev/null 2>&1 || true
  systemctl restart docker
fi
docker compose version

mkdir -p /var/lib/muse-adapter && touch /var/lib/muse-adapter/bootstrap-done
