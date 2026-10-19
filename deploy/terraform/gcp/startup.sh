#!/bin/bash
# Runs once on the VM's first boot (Google Cloud runs it as root). It reads its settings from the
# instance metadata that Terraform set, fetches the Tailscale auth key from Secret Manager with
# the VM's own identity, installs muse-vm from pip, and runs the same `muse-vm up --local` a person
# would. Nothing secret is in the metadata.
set -euo pipefail
exec > >(tee -a /var/log/muse-vm-install.log) 2>&1

marker=/var/lib/muse-vm-installed
if [ -f "$marker" ]; then
  echo "already installed"
  exit 0
fi

metadata() {
  curl -fsS -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/$1"
}

name="$(metadata instance/attributes/muse-name)"
source_spec="$(metadata instance/attributes/muse-install-source)"
secret="$(metadata instance/attributes/muse-tailscale-secret)"
egress="$(metadata instance/attributes/muse-egress-allow || true)"
project="$(metadata project/project-id)"

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3-venv python3-pip curl ca-certificates git

token="$(metadata instance/service-accounts/default/token | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"
TS_AUTHKEY="$(curl -fsS -H "Authorization: Bearer $token" \
  "https://secretmanager.googleapis.com/v1/projects/${project}/secrets/${secret}/versions/latest:access" \
  | python3 -c 'import base64,json,sys; print(base64.b64decode(json.load(sys.stdin)["payload"]["data"]).decode().strip())')"
export TS_AUTHKEY

python3 -m venv /opt/muse-vm
/opt/muse-vm/bin/pip install --quiet "$source_spec"
ln -sf /opt/muse-vm/bin/muse-vm /usr/local/bin/muse-vm

args=(up --local --name "$name" --yes)
if [ -n "$egress" ]; then
  args+=(--egress-allow "$egress")
fi
MUSE_VM_NO_BROWSER=1 /usr/local/bin/muse-vm "${args[@]}"

touch "$marker"
echo "muse-vm is installed: connect servers with: sudo muse-vm add-server <name> --name $name"
