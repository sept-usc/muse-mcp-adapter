# Automation, Terraform and doing it by hand

## Settings file and automation

Everything `setup` asks can also be written down once in `muse-vm.toml` in the folder you run it from (or passed with `--config`). Every key is optional. A person at a terminal is asked for what is missing; a script gets an error naming it. Unknown keys are rejected, so a typo never silently does nothing.

```toml
name = "prod"                    # deployment name (default: muse)
cloud = "gcp"                    # or: ssh = "deploy@203.0.113.7", or: local = true
project = "my-project"           # default: gcloud's
zone = "us-central1-a"           # default: gcloud's, else us-central1-a
machine_type = "e2-small"
budget_usd = 20                  # a monthly budget alert on the project
idle_stop_hours = 8              # power the VM off after this long without use
egress_allow = ["10.0.0.0/24"]   # private ranges the adapter may reach (default: none)
tailscale_authkey_file = "~/ts-key"   # a single-use auth key, to skip the sign-in link
yes = true                       # accept that this creates billable resources

[[servers]]
preset = "github"
allow = ["issue_write"]          # tools beyond the read-only ones
token_file = "~/github-token"    # one line; read, never printed

[[servers]]
preset = "learn"

[[servers]]
id = "tracker"                   # a server of your own
endpoint = "https://tracker.example.com/mcp"
credential = "env_token"         # none, env_token or oauth
secret_name = "TRACKER_TOKEN"
```

```bash
.venv/bin/muse-vm setup                       # reads ./muse-vm.toml, asks only for what is missing
.venv/bin/muse-vm setup --non-interactive     # never asks: fails and says what is missing
.venv/bin/muse-vm setup --cloud gcp --project my-project --server github --yes
```

A saved file holds no secrets: only the paths of files that do. Flags override the file. For a fully unattended run, give a Tailscale auth key (`tailscale_authkey_file`, or `TS_AUTHKEY`) so there is no link to click, `yes = true` for the cost, and a `token_file` for every server that needs a token. OAuth servers (Linear, Atlassian) always need you in a browser once.


## With Terraform

For teams that manage infrastructure as code, `deploy/terraform/gcp` creates the same locked-down setup on Google Cloud declaratively: a network of its own, a shielded VM with no public address, SSH only through Identity-Aware Proxy, Cloud NAT for outbound traffic, daily snapshots, and a first-boot install that runs the same `muse-vm up --local` a person would.

```bash
# 1. A single-use, untagged Tailscale auth key (Settings, Keys), kept in Secret Manager: it never enters Terraform state
printf '%s' "tskey-auth-..." | gcloud secrets create my-tailscale-key --data-file=- --project my-project
```

```hcl
# 2. main.tf
module "muse_adapter" {
  source = "github.com/sept-usc/muse-mcp-adapter//deploy/terraform/gcp?ref=<a tag or commit>"

  project                   = "my-project"
  tailscale_auth_key_secret = "my-tailscale-key"
  install_source            = "git+https://github.com/sept-usc/muse-mcp-adapter@<the same tag or commit>"
}
```

```bash
terraform init && terraform apply     # then follow the first-boot install (10 to 20 minutes):
terraform output watch_the_install    # prints the command; it ends with the adapter's address
terraform output connect_a_server     # connect a server from your computer, over IAP
```

The VM reads the key from Secret Manager once, with an identity that can read that one secret and nothing else; the adapter container cannot reach that identity (the egress guard blocks the metadata server). Other inputs: `name`, `region`, `zone`, `machine_type`, `disk_gb`, `egress_allow`, `snapshots`, and `tailscale_auth_key` if you would rather have Terraform store the key (then it is in the Terraform state). Pin `ref` and `install_source` to a reviewed release or commit. Day-two commands (`add-server`, `expose`, `upgrade`, `backup`) run on the VM, for example `gcloud compute ssh mma-muse --tunnel-through-iap -- -t sudo muse-vm add-server github`; remove everything with `terraform destroy`.


## Doing it by hand

`setup` is a front end to these commands, which stay available for scripts and for finer control. All options are listed in the [deployment reference](deployment.md#commands).

```bash
.venv/bin/muse-vm up --cloud gcp --project my-project        # or: --ssh deploy@203.0.113.7
.venv/bin/muse-vm add-server github                          # a well-known server needs nothing else
.venv/bin/muse-vm add-server tracker --endpoint https://tracker.example.com/mcp --credential env_token
.venv/bin/muse-vm expose github --allow issue_write
```

`up` also takes `--local` to install on the machine you are on, and many more options (`--zone`, `--machine-type`, `--budget-usd`, `--idle-stop-hours`, `--egress-allow`, `--ssh-opt`, `--image`, `--keep-on-failure`, `--no-browser`) and, if you prefer not to click a Tailscale sign-in link, a Tailscale auth key in `TS_AUTHKEY`: a single-use, untagged key that expires in a day, generated under **Settings, Keys** in the Tailscale console. The key is removed from the machine as soon as the device has joined.
