# Deploying on a VM

`muse-vm` puts the adapter on a Linux machine and looks after it from your own computer. The adapter is installed inside the VM, runs in a locked-down container, and is reachable by Muse over HTTPS. Nothing else on the machine is exposed.

```
pip install muse-mcp-adapter        # provides the muse-vm command
muse-vm up --cloud gcp --project my-project
muse-vm add-server learn --endpoint https://learn.microsoft.com/api/mcp
```

`add-server` prints a connector URL and a token. Paste both into Muse.

## Where it can run

| Target | Command | Notes |
|---|---|---|
| Google Cloud VM | `muse-vm up --cloud gcp --project P` | Creates a dedicated network, the VM, and the rules around it. Needs the `gcloud` CLI, signed in. |
| Any Linux machine | `muse-vm up --ssh user@host` | Debian/Ubuntu, x86_64 or aarch64, key-based SSH and passwordless `sudo`. |

## How Muse reaches it

| Ingress | What it needs | Inbound ports |
|---|---|---|
| `caddy` | A public IP. Gets a certificate automatically for `<ip>.sslip.io`, or for `--domain`. | 80, 443 |
| `tailscale-funnel` | A Tailscale account. | none |
| `cloudflare-tunnel` | A Cloudflare tunnel token and hostname (`--domain`). | none |

On Google Cloud, tunnel ingress creates a VM with no public IP at all. If you do not pick one, a public address means `caddy` and anything else means `tailscale-funnel`.

## What `up` sets up

- Docker, automatic security updates, and log rotation on the host.
- The adapter container: pinned by digest, non-root, read-only filesystem, all capabilities dropped, memory/CPU/process limits.
- An **egress guard**: the adapter cannot reach the cloud metadata server, private ranges, or link-local addresses. Add exceptions with `--egress-allow`.
- A health check, a timer that restarts anything unhealthy, a daily backup, and optional idle shutdown (`--idle-stop-hours`).
- On Google Cloud: its own network (the default network is not used), SSH only through Identity-Aware Proxy, a shielded VM with no service account, OS Login, daily disk snapshots (kept 7 days), and an optional budget alert (`--budget-usd`).

It then runs a self-check against the live address (health, security headers, rejection of requests without a valid token, the egress guard, the container's user and filesystem) and prints the result. `muse-vm selfcheck` repeats it any time.

If anything fails part way, what was created is removed again, unless `--keep-on-failure`.

## Connecting servers

```
muse-vm add-server linear --endpoint https://mcp.linear.app/mcp --credential env_token
muse-vm secret set LINEAR_TOKEN
```

`add-server` registers the server, discovers its tools, drafts a policy (read-only tools approved, everything else held back — use `--allow TOOL` or `--policy FILE` to change that), publishes it, links it to Muse, and prints the connector URL and token. For servers that sign in with OAuth, use `--credential oauth` and follow the sign-in link it prints.

Secrets are read from files in a root-only directory on the VM, never from the command line, the environment, or the image.

| Command | Purpose |
|---|---|
| `servers` | Connected servers and their connections |
| `rotate-token SERVER` | Issue a new token (the old one stops working) |
| `unlink SERVER` | Stop Muse using a server |
| `secret set NAME` / `secret list` | Manage secrets (values are never shown) |

## Day to day

| Command | Purpose |
|---|---|
| `list`, `status`, `logs` | See deployments, health, and container logs |
| `stop`, `start` | Stop the adapter (on a cloud VM, the VM itself, so billing pauses) |
| `upgrade` | Move to this version; address, connections, and data stay |
| `backup --to FILE` | Download an encrypted backup (passphrase of at least 12 characters) |
| `restore --from FILE` | Restore onto the deployment; current data is moved aside, not deleted |
| `down` | Remove the deployment and everything it created |

Deployment details are kept on your computer under `~/.config/muse-vm/` (no secrets in them).

## Using Terraform instead

`deploy/terraform/gcp` creates the same network, VM, and snapshot schedule declaratively. Run `muse-vm up --ssh ...` against the VM afterwards to install the adapter.

## Building from source

`--build` builds the images on the VM from your checkout instead of pulling them. The adapter image is built from the repository `Dockerfile` with hash-locked dependencies (`requirements.lock`).
