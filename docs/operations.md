# Running the adapter on a VM

What you need, how to start it, how to look after it, and what to do when something is wrong. Commands are explained in the [deployment guide](deployment.md); this page is the checklist.

## Prerequisites

**On your computer (where you run `muse-vm`)**

| Need | Why |
|---|---|
| Python 3.12 or newer, `git`, and a clone of this repository with `pip install .` run in it | Provides the `muse-vm` and `muse-mcp-adapter` commands |
| `ssh` client | Reaching the VM |
| Google Cloud only: the `gcloud` CLI, signed in (`gcloud auth login`) | Creates the network and VM |
| Google Cloud only: a project with billing, the Compute Engine and IAP APIs enabled, and your account allowed to create instances, firewall rules and use IAP tunnelling | Provisioning and SSH |

You do not need Docker on your computer: the images are built on the VM from your checkout of this repository. The VM needs outbound access to Docker Hub (base images) and PyPI (dependencies, verified against hashes).

**On the VM (Google Cloud creates this for you; for an existing machine check these)**

| Need | Detail |
|---|---|
| Debian or Ubuntu Linux, x86_64 or aarch64 | Docker and automatic updates are installed for you |
| 1 vCPU, 2 GB memory, 20 GB disk | `e2-small` is the default size |
| Key-based SSH and passwordless `sudo` for your user | `up` checks this first and tells you what is missing |
| Caddy ingress only: a public IP and free ports 80 and 443 | Tunnel ingresses need neither |

**For connecting servers**

| Need | Detail |
|---|---|
| The MCP server's public HTTPS address | Private or HTTP endpoints are refused |
| A token (token servers) or a browser (OAuth servers) | Tokens are entered with `muse-vm secret set`, never on a command line |
| Muse's custom connector screen | To paste the connector address and token |

## Start

```
git clone https://github.com/sept-usc/muse-mcp-adapter && cd muse-mcp-adapter
pip install .
muse-vm up --cloud gcp --project my-project    # or: --ssh user@host
muse-vm add-server learn --endpoint https://learn.microsoft.com/api/mcp
```

`up` finishes with a self-check. `add-server` prints the connector address and the token; paste both into Muse. For a tunnel ingress, `up` may print a sign-in link (Tailscale) to open once.

## What runs

| Container | Role | Restarts |
|---|---|---|
| `guard` | Owns the adapter's network; blocks metadata and private ranges | Always |
| `adapter` | The service | Always; a timer also restarts it if its health check fails |
| `caddy` / `tailscale` / `cloudflared` | The one ingress you chose | Always |

On the host: a daily backup timer, a health timer, optional idle shutdown, automatic security updates. Everything lives in `/opt/muse-adapter` (`data`, `config`, `secrets`, `backups`).

## Looking after it

| Task | Command |
|---|---|
| Is it healthy? | `muse-vm status`, then `muse-vm selfcheck` |
| Logs | `muse-vm logs` (adapter), `muse-vm logs caddy` |
| Pause to save money | `muse-vm stop` / `start` (a cloud VM is powered off; disks are kept) |
| New version | `muse-vm upgrade` (address, connections and data stay) |
| Backup | `muse-vm backup --out ./backup.mmavm` (asks for a passphrase) |
| Restore | `muse-vm restore ./backup.mmavm` |
| Leaked token | `muse-vm rotate-token SERVER` |
| Remove everything | `muse-vm down` (add `--keep-data` to keep the data disk) |

Keep a backup somewhere other than the VM. Snapshots (Google Cloud) are taken daily and kept 7 days.

## Troubleshooting

Start with `muse-vm selfcheck`; each line names what failed.

| Symptom | Likely cause | What to do |
|---|---|---|
| `Cannot run commands on … with sudo -n` | Key-based SSH or passwordless sudo is missing | Fix the login, then run `up` again |
| `Ports 80 and 443 are already in use` | Another web server on the machine | Stop it or use `--ingress tailscale-funnel` |
| `up` stops at "waiting for SSH through IAP" | New VM still starting, or IAP access is not granted to your account | Wait a minute; check you have the IAP tunnel and OS Login roles |
| Self-check: `/health` fails | Containers still starting, or the adapter exited | `muse-vm logs`; `muse-vm status` |
| Self-check: HTTPS or certificate fails (Caddy) | DNS not yet pointing at the VM, firewall, or certificate rate limit | Wait and `selfcheck` again; check `muse-vm logs caddy` |
| Tailscale ingress shows a login link | First start needs your sign-in | Open the link once; then `selfcheck` |
| Tailscale address answers intermittently | A Tailscale release with a Funnel fault | The bundle pins a good version; do not override `MMA_TS_IMAGE` without testing |
| Muse says 401 or "token invalid" | Token rotated, wrong connector, or a typo | Re-run `rotate-token`, paste the new one |
| Muse says the server is unavailable | The ingress is down or the VM is stopped | `muse-vm status`; `muse-vm start` |
| A tool call fails with "reconnect" and a link | The provider's OAuth sign-in expired | Open the link and approve again |
| `add-server` fails on a private or HTTP endpoint | Refused by design | Use the server's public HTTPS address (a private server needs `--egress-allow` and a policy decision) |
| A write shows `UNKNOWN` | The call may or may not have happened | Check the downstream system, then settle it with the adapter's `reconcile` command (see the [usage guide](usage.md#1-command-line-tool)); it is never retried automatically |
| Disk full | Logs and backups | Logs rotate at 50 MB; delete old backups you no longer need |
| Lost the passphrase | Backups cannot be recovered | Make a new backup with a new passphrase while the VM is healthy |

If a step of `up` fails, what it created is removed again. Add `--keep-on-failure` to leave it for inspection, and `-v` to see every command.
