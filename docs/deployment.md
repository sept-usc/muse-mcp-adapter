# Deployment reference

The [README](../README.md#step-by-step-setup) walks through installing the adapter on a VM. This page is the reference behind it: what is installed on the machine, every `muse-vm` command, and how the images are built.

## What is installed on the machine

Everything the adapter needs lives in `/opt/muse-adapter`; a few host settings are added only when they are missing.

| What | Where | Notes |
|---|---|---|
| Compose project: `guard`, `adapter`, `tailscale` | `/opt/muse-adapter/compose.yml` | Three containers, no published ports |
| Adapter configuration | `config/serve.json` | Names where secrets are; holds none |
| Database, key file, token store, artifacts | `data/` (mode 700, owned by the container user) | The adapter's state |
| Secrets for MCP servers | `secrets/` (mode 700, one file per secret, mode 400) | Mounted read-only into the container |
| Backups the adapter makes of itself | `backups/` | Daily, owner-only |
| Tailscale device state | `ts-state/` | The device's own credentials |
| Compose variables | `.env` (mode 600) | Image names and settings; no secrets once the device has joined |
| systemd units | `muse-adapter.service`, `muse-adapter-backup.timer`, `muse-adapter-heal.timer`, optional `muse-adapter-idle.timer` | Start at boot, back up, restart an unhealthy adapter, optionally stop when idle |
| Docker, with the compose plugin | The distribution's `docker.io` | **Installed only if missing.** An existing Docker is never restarted or reconfigured |
| Automatic security updates | `unattended-upgrades` | **Enabled only if the machine has none.** An existing update policy is never overwritten |
| Docker log rotation | `/etc/docker/daemon.json` | Only on a machine where Docker was just installed. The adapter's containers rotate their own logs in either case |

`muse-vm down` removes `/opt/muse-adapter`, the units and the images it built. It does not remove Docker or the update settings.

## How the containers are hardened

- **Adapter:** runs as an unprivileged user (uid 10001), read-only root filesystem, all Linux capabilities dropped, `no-new-privileges`, memory, CPU and process limits, bounded logs, a health check.
- **Egress guard:** a small container that owns the adapter's network namespace and installs firewall rules before the adapter starts. The adapter cannot reach the cloud metadata server, private address ranges, carrier-grade NAT or link-local addresses, even if the adapter were compromised. `--egress-allow CIDR` adds a deliberate exception for an MCP server you run on a private network; the default is none.
- **Tailscale:** pinned to a version, by digest, that is known to publish Funnel reliably.
- **Images:** base images pinned by digest, Python packages installed from a hash-locked file, so the build is repeatable and a tampered package is refused.

## Commands

| Command | Purpose |
|---|---|
| `up --cloud gcp --project P` | Create a Google Cloud VM and install the adapter. Options: `--zone`, `--machine-type`, `--budget-usd N`, `--idle-stop-hours N`, `--no-snapshots` |
| `up --ssh USER@HOST` | Install on a machine you have. Option: `--ssh-opt` for extra ssh options (a key, a port) |
| `up` common options | `--name` (default `muse`), `--egress-allow CIDR`, `--tz`, `--keep-on-failure`, `--allow-dirty`, `-v` (print every command), `--yes` |
| `add-server ID --endpoint URL` | Register, discover, draft a policy, publish and link a server; prints the connector address, token and connection code. `--credential none\|env_token\|oauth`, `--secret-name`, `--allow TOOL`, `--policy FILE`, `--no-reads` |
| `expose SERVER --allow TOOL --deny TOOL` | Publish a new version that changes which tools Muse can use; existing settings are kept |
| `servers`, `rotate-token SERVER`, `unlink SERVER` | See connections, replace a token, stop Muse using a server |
| `secret set NAME`, `secret list` | Store or list the secrets servers need (values are never shown) |
| `list`, `status`, `logs [SERVICE]`, `selfcheck` | Look at deployments and their health |
| `start`, `stop` | Start or stop the adapter (on Google Cloud, the VM) |
| `upgrade` | Rebuild from your updated checkout and restart; the address, connections and data stay |
| `backup --out FILE`, `restore FILE` | Encrypted backup and restore. Set `MUSE_VM_BACKUP_PASSPHRASE` to avoid the prompt |
| `down [--keep-data]` | Sign the device out of Tailscale and remove everything that was created |

Deployment details are kept on your computer under `~/.config/muse-vm/` (set `MUSE_VM_HOME` to move it). They hold no secrets.

## How the images are built

The images are always built on the target machine from your checkout of this repository, so you run exactly the code you reviewed and nothing is pulled from a registry we operate. The build uses the repository `Dockerfile` with hash-locked dependencies (`requirements.lock`) and base images pinned by digest, and records the commit it was built from in the image. `up` and `upgrade` refuse a checkout with uncommitted changes (`--allow-dirty` overrides).

To get a new version: `git pull`, review what changed, then `muse-vm upgrade`.

If your organisation requires images from its own registry, build and scan them yourself from the same `Dockerfile` and `deploy/guard/Dockerfile`, push them to your registry, and run `muse-vm up --image REF --guard-image REF`. The machine must be able to pull from it.
