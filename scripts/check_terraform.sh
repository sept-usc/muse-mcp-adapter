#!/usr/bin/env bash
# The Terraform module must be formatted, valid, and plan to the resources we expect, and must
# refuse a missing or ambiguous Tailscale key. Run in a pinned container; no credentials are used
# (a dummy token and no refresh, so nothing is contacted).
set -euo pipefail
cd "$(dirname "$0")/.."
image="hashicorp/terraform:1.9.8"
run() {
  docker run --rm -v "$PWD/deploy/terraform/gcp":/module -w /module \
    -e GOOGLE_OAUTH_ACCESS_TOKEN=dummy -e TF_IN_AUTOMATION=1 -e TF_DATA_DIR=/tmp/tfdata \
    --entrypoint sh "$image" -c "$1"
}
run 'terraform fmt -check -recursive && terraform init -backend=false -input=false >/dev/null && terraform validate'
plan="$(run 'terraform init -backend=false -input=false >/dev/null && terraform plan -refresh=false -input=false -no-color -var project=demo -var tailscale_auth_key_secret=my-key')"
echo "$plan" | grep -q "Plan: 13 to add" || { echo "$plan"; echo "the plan is not the 13 resources expected" >&2; exit 1; }
for resource in google_compute_instance.vm google_compute_router_nat.nat google_compute_firewall.iap_ssh google_secret_manager_secret_iam_member.reader; do
  echo "$plan" | grep -q "# $resource will be created" || { echo "missing $resource" >&2; exit 1; }
done
# a firewall rule that opens anything but SSH from IAP would show up here
echo "$plan" | grep -q '"0.0.0.0/0"' && { echo "the plan opens a port to the world" >&2; exit 1; }
if run 'terraform init -backend=false -input=false >/dev/null && terraform plan -refresh=false -input=false -no-color -var project=demo' >/dev/null 2>&1; then
  echo "a plan without a Tailscale key should have been refused" >&2; exit 1
fi
if run 'terraform init -backend=false -input=false >/dev/null && terraform plan -refresh=false -input=false -no-color -var project=demo -var tailscale_auth_key_secret=a -var tailscale_auth_key=b' >/dev/null 2>&1; then
  echo "a plan with two Tailscale keys should have been refused" >&2; exit 1
fi
echo "terraform: formatted, valid, plans 13 resources, refuses a missing or ambiguous key"
