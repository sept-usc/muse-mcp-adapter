# Running the adapter on a VM

How it runs, how to look after it, and what to do when something is wrong. The [README](../README.md#install-it-on-a-vm) covers installation and the [deployment reference](deployment.md) lists every command.

## What runs

| Container | Role | Restarts |
|---|---|---|
| `guard` | Owns the adapter's network; blocks the cloud metadata server and private ranges | Always |
| `adapter` | The service | Always; a timer also restarts it if its health check fails |
| `tailscale` | Publishes the adapter at its public HTTPS address (Funnel) | Always |

On the host: a daily backup timer, a health timer, optional idle shutdown, and automatic security updates. Everything lives in `/opt/muse-adapter`. After a reboot everything starts again without help, usually within a minute.

## Looking after it

| Task | Command |
|---|---|
| Is it healthy? | `muse-vm status`, then `muse-vm selfcheck` |
| Logs | `muse-vm logs` (adapter), `muse-vm logs tailscale`, `muse-vm logs guard` |
| Pause to save money | `muse-vm stop` / `start` (a cloud VM is powered off; disks are kept) |
| New version | `git pull`, review, then `muse-vm upgrade` |
| Backup | `muse-vm backup --out ./backup.mmavm` (asks for a passphrase of at least 12 characters) |
| Restore | `muse-vm restore ./backup.mmavm` |
| Leaked token | `muse-vm rotate-token SERVER` |
| Remove everything | `muse-vm down` (add `--keep-data` to keep the data disk) |

Keep a backup somewhere other than the machine. A backup holds the adapter's key and tokens, so it is encrypted with your passphrase; losing the passphrase means losing the backup. Server tokens stored with `muse-vm secret set` are **not** in a backup: set them again after a restore. On Google Cloud, disk snapshots are taken daily and kept for 7 days.

## Troubleshooting

Start with `muse-vm selfcheck`; each line names what failed. `-v` on any command prints every command it runs.

| Symptom | Likely cause | What to do |
|---|---|---|
| `up` says the tailnet does not allow HTTPS and Funnel | The one-time Tailscale settings are missing | Follow the steps it prints: enable HTTPS certificates, and add `"nodeAttrs": [{"target": ["autogroup:member"], "attr": ["funnel"]}]` to the access controls |
| `up` prints a Tailscale sign-in link and then times out | The link expired before it was opened (links last a few minutes) | Use an auth key (`TS_AUTHKEY`), or open the newest link as soon as it is printed |
| `up` says the device needs approval | Your tailnet requires device approval | Approve it under Machines in the Tailscale console |
| The address does not resolve, or the self-check waits a long time | A first-time Funnel name can take up to ten minutes to reach public DNS | Wait; `muse-vm selfcheck` checks again. It asks a public resolver too, so a computer that is itself in your tailnet is not misled |
| The address ends in `-1` or `-2` | An older device with the same name is still in your tailnet | Delete the stale offline device under Machines. `muse-vm down` signs devices out, so this happens only after an interrupted run |
| `Cannot run commands on … with sudo -n` | Key-based SSH or passwordless sudo is missing | Fix the login, then run `up` again |
| `up` stops waiting for SSH on Google Cloud | A new VM is still booting, or IAP access is not granted to your account | The tool retries dropped connections; check that you have the IAP tunnel and OS Login roles |
| The image build fails | The machine cannot reach Docker Hub or PyPI | Allow outbound access from the machine, or pre-build the images and use `--image` and `--guard-image` |
| `The checkout … has uncommitted changes` | You have local edits to what is deployed | Commit or stash them, so the deployment matches a commit |
| Self-check: `/health` fails but the inside checks pass | The public address is not reachable from here yet, or Tailscale is down | `muse-vm logs tailscale`; check the Tailscale console shows the device online |
| Muse says 401 or "token invalid" | Token rotated, wrong connector, or a typo | `muse-vm rotate-token SERVER`, paste the new one |
| Muse says the server is unavailable | The VM is stopped or Tailscale is disconnected | `muse-vm status`; `muse-vm start` |
| A tool call fails with "reconnect" and a link | The provider's OAuth sign-in expired | Open the link and approve again |
| `add-server` fails on a private or HTTP endpoint | Refused by design | Use the server's public HTTPS address (a private server needs `--egress-allow` and a policy decision) |
| A write shows `UNKNOWN` | The call may or may not have happened | Check the downstream system, then settle it with the adapter's `reconcile` command (see the [usage guide](usage.md#1-command-line-tool)); it is never retried automatically |
| Muse reports success but nothing changed | An identical write within 30 seconds of an earlier one is treated as a retry and replays the first result | The response is flagged `idempotentReplay`; ask again after a moment, or send an `Idempotency-Key` |
| Disk full | Logs and backups | Container logs rotate at 50 MB; delete old backups you no longer need |
| Lost the passphrase | Backups cannot be recovered | Make a new backup with a new passphrase while the machine is healthy |

If a step of `up` fails, what it created is removed again. Add `--keep-on-failure` to leave it for inspection.
