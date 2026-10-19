# Set up the Muse MCP Adapter on Google Cloud

<walkthrough-tutorial-duration duration="20"></walkthrough-tutorial-duration>

## What this does

This creates a small, locked-down virtual machine in your Google Cloud project, runs the Muse MCP
Adapter on it, and connects the MCP servers you choose to Muse. Nothing is installed on your own
computer.

It costs about $13 a month while running, plus a little for the disk and network address
translation, and you can delete everything at the end with one command. You will click up to two
Tailscale links along the way; you need a free [Tailscale](https://tailscale.com) account.

Click **Start** to begin.

## Choose a project

<walkthrough-project-setup billing="true"></walkthrough-project-setup>

The adapter is created in the project you pick here. It uses a network of its own, a VM with no
service account and no public address, and SSH only through Identity-Aware Proxy, so it does not
touch anything else in the project.

## Run the guided setup

Copy this command into Cloud Shell and press Enter:

```bash
scripts/cloudshell-setup.sh --cloud gcp --project <walkthrough-project-id/>
```

It installs `muse-vm`, checks that everything is in place, and asks you which MCP servers Muse
should use. Building takes 10 to 20 minutes.

**The Tailscale links.** Tailscale gives the adapter its public address. `muse-vm` prints a
sign-in link: open it, sign in and approve the new device. If your Tailscale network has not used
Funnel before it prints one more link: approve that too. Then it carries on by itself.

**Tokens.** If you choose a server that needs a token (GitHub, for example), you are asked for it
at a hidden prompt. It is stored only on the VM, in a folder only the adapter can read.

## Connect Muse

When it finishes, `muse-vm` prints a connector address and a token for each server. In a new Muse
side chat, say:

```text
Create a connector called GitHub from the API spec at <the connector address>
```

Click **Connect**, accept Muse's consent dialog, and paste the token. Then ask Muse to use it.

## Clean up

To stop paying for it, pause or remove the VM:

```bash
muse-vm stop      # pause: the VM is powered off and its disk kept
muse-vm start     # resume
muse-vm down      # remove everything it created
```

<walkthrough-conclusion-trophy></walkthrough-conclusion-trophy>

You are done. More: [the README](../README.md) and [troubleshooting](../README.md#troubleshooting).
