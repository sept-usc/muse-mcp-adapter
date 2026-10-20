# Troubleshooting


Start with the commands that explain most problems:

```bash
.venv/bin/muse-vm doctor        # before creating anything: what is missing, and how to fix it
.venv/bin/muse-vm selfcheck     # after: each line says what passed or failed
.venv/bin/muse-vm logs          # the adapter's log; also: logs tailscale, logs guard
```

Add `-v` to any `muse-vm` command to print every command it runs. If `up` fails it removes what it created; add `--keep-on-failure` to keep the machine for inspection.

## While creating the deployment (`muse-vm setup` or `muse-vm up`)

| What you see | Why | What to do |
|---|---|---|
| `gcloud credentials … have expired` | `gcloud` still lists your account but its stored sign-in has expired | `gcloud auth login`, then run again |
| `Python 3.12 or newer`, `ssh` or `git` is marked ✗ | A tool is missing on your computer | Install it; `muse-vm doctor` says which |
| `your tailnet does not allow HTTPS and Funnel yet` | Your Tailscale network has not used Funnel before | Open the approval link it prints (it also opens in your browser) and approve. It keeps waiting, so you can do it while it runs. If it printed steps instead of a link: enable HTTPS certificates under **DNS** in the Tailscale console, and add `"nodeAttrs": [{"target": ["autogroup:member"], "attr": ["funnel"]}]` to the access controls |
| It prints a Tailscale sign-in link and then stops after 30 minutes | Nobody opened the link in that time. The link itself stays valid while it waits | Run the same command again: it prints a new link. For an unattended run, use an auth key (`TS_AUTHKEY`) |
| `this tailnet requires device approval` | Your tailnet approves new devices | Approve the device under **Machines** in the Tailscale console |
| `That does not look like a Tailscale auth key` | The key was pasted with extra text, or is not a `tskey-` key | Generate a new one and save it without spaces or prefixes |
| The address does not resolve, or `up` waits a long time at the self-check | A brand-new Funnel name can take up to ten minutes to reach public DNS | Wait. `muse-vm selfcheck` checks again, and uses public DNS so a computer that is itself in your tailnet is not misled |
| The address ends in `-1` or `-2` | An older device with the same name is still in your tailnet | Harmless. Delete the stale offline device under **Machines**. `muse-vm down` signs the device out, but Tailscale keeps its entry until you delete it, so reinstalling under the same name gets a suffix. Delete the entry after `down` to keep the name |
| `Cannot run commands on … with sudo -n` | SSH key login or passwordless `sudo` is missing on the machine | Fix the login (`ssh user@host sudo -n true` must work), then run `up` again |
| Google Cloud: it waits for SSH for several minutes | A new VM is still booting and installing Docker, or IAP access is not granted | Give it up to 10 minutes. It retries dropped connections itself. If it never connects, check that your account has the IAP tunnel and OS Login roles |
| `A network named mma-… already exists` | A previous run left something behind | `muse-vm down --name <name>`, or choose another `--name` |
| `setup` says the deployment already exists | You ran it again | That is fine: it keeps the deployment and only adds the servers you ask for |
| The image build fails or hangs | The machine cannot reach Docker Hub or PyPI | Allow outbound HTTPS from the machine, or build the images elsewhere and use `--image` and `--guard-image` ([reference](deployment.md#how-the-images-are-built)) |
| `The checkout … has uncommitted changes` | You edited files that are deployed | Commit or stash them, so the deployment matches a commit |
| The self-check fails on `/health` but the checks inside the machine pass | The public address is not reachable from here yet, or Tailscale is disconnected | `muse-vm logs tailscale`; check the device is online in the Tailscale console |

## While connecting a server (`muse-vm add-server`)

| What you see | Why | What to do |
|---|---|---|
| `The server offers no such tool` | A tool name in `--allow` or `--deny` is misspelled | The message lists the tools the server offers |
| It refuses a private or `http://` address | By design: the adapter only calls public HTTPS servers | Use the server's public HTTPS address. A server on a private network needs `--egress-allow` and a deliberate policy decision |
| The server returns an authentication error | The token is wrong, expired, or lacks permission | Run the same `add-server` (or `muse-vm setup`) again with a correct token: it replaces the stored one and finishes the half-done connection. For GitHub, check the token's repository access and permissions |
| `… is already connected` | The server already has a connection | Nothing to do. `muse-vm servers` lists it; `rotate-token` issues a new token if you lost it |
| An OAuth server shows "reconnect" | The sign-in expired | Open the link it prints and approve again |

## While connecting or using Muse

| What you see | Why | What to do |
|---|---|---|
| Muse says it cannot create the connector from that address | Muse accepts only ordinary public hostnames, not ones that encode an IP (`…sslip.io`) | Use the `*.ts.net` address `muse-vm up` printed |
| Muse says 401 or "token invalid" | The token was rotated, belongs to a different connector, or has a typo | `muse-vm rotate-token SERVER` and paste the new token |
| Muse says the server is unavailable | The machine is stopped, or Tailscale is disconnected | `muse-vm status`; `muse-vm start` |
| Muse cannot use a tool you expected | It is not published, or is a write | `muse-vm expose SERVER --allow TOOL` |
| A write shows `UNKNOWN` | The call may or may not have happened | Check the other system, then settle it with the adapter's `reconcile` command ([usage guide](usage.md#1-command-line-tool)). It is never retried automatically |
| Muse reports success but nothing changed | An identical write within 30 seconds of an earlier one is treated as a retry and replays the first result | The response says `idempotentReplay`. Ask again after a moment, or send an `Idempotency-Key` |

## While it runs

| What you see | Why | What to do |
|---|---|---|
| The adapter stopped | The machine rebooted, or a container crashed | Both recover by themselves, usually within a minute. `muse-vm status` shows the state |
| The disk is filling up | Logs and backups | Container logs rotate at 50 MB; delete old backups you no longer need |
| You lost the backup passphrase | Backups are encrypted with it and cannot be recovered | Make a new backup with a new passphrase while the machine is healthy |
| After a restore, a server stops working | Server tokens are not part of a backup | `muse-vm secret set NAME` again |

## Still stuck?

Collect `muse-vm status`, `muse-vm selfcheck` and `muse-vm logs` (they contain no secrets), then see [Running on a VM](operations.md) for more, or open an issue with that output.
