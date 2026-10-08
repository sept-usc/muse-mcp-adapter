#!/bin/sh
# Prepare a Debian or Ubuntu host: Docker with the compose plugin, bounded container logs, and
# automatic security updates. Safe to run again. Used as the cloud VM's startup script and run
# over SSH by `muse-vm up --ssh`.
set -eu

if ! command -v apt-get >/dev/null 2>&1; then
  echo "muse-vm bootstrap: this needs apt (Debian or Ubuntu). Install Docker Engine with the compose plugin and run again." >&2
  exit 3
fi
export DEBIAN_FRONTEND=noninteractive

if ! docker compose version >/dev/null 2>&1; then
  apt-get update -q
  apt-get install -y -q docker.io
  # Ubuntu ships the plugin as docker-compose-v2, Debian 13 as docker-compose.
  apt-get install -y -q docker-compose-v2 || apt-get install -y -q docker-compose
fi

# Security updates install themselves; a long-lived VM that never patches is the usual way in.
if ! dpkg -s unattended-upgrades >/dev/null 2>&1; then
  apt-get install -y -q unattended-upgrades
fi
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF

# Rotate container logs for every container, whatever its compose file says.
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
docker compose version

mkdir -p /var/lib/muse-adapter && touch /var/lib/muse-adapter/bootstrap-done
