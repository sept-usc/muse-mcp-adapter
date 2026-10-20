# Muse MCP Adapter

**Let Muse use any MCP server, safely.** The adapter sits between Muse and the MCP servers you rely on. It shows Muse only what you approve, keeps your credentials to itself, and makes sure a write runs once or says it may not have.

```mermaid
flowchart LR
    Muse["<b>Muse</b><br/>your personal agent asks,<br/>and confirms writes with you"]
    Owner["<b>You, the owner</b><br/>register servers,<br/>choose what Muse may use"]

    subgraph Adapter["Muse MCP Adapter"]
        direction TB
        subgraph C1["Connection 1"]
            direction LR
            U1["<b>Own URL and token</b><br/>Muse reaches only<br/>this connection"] --> P1["<b>Own policy</b><br/>only the operations<br/>you approved"] --> K1["<b>Own credential</b><br/>none, a token, or<br/>your own sign-in"]
        end
        subgraph C2["Connection 2"]
            direction LR
            U2["<b>Own URL and token</b><br/>Muse reaches only<br/>this connection"] --> P2["<b>Own policy</b><br/>only the operations<br/>you approved"] --> K2["<b>Own credential</b><br/>none, a token, or<br/>your own sign-in"]
        end
        subgraph C3["Connection 3"]
            direction LR
            U3["<b>Own URL and token</b><br/>Muse reaches only<br/>this connection"] --> P3["<b>Own policy</b><br/>only the operations<br/>you approved"] --> K3["<b>Own credential</b><br/>none, a token, or<br/>your own sign-in"]
        end
        Shared["<b>Shared by every connection</b><br/>validation, size and time limits, operation log, encrypted token store"]
    end

    S1["<b>Microsoft Learn</b><br/>MCP server"]
    S2["<b>Linear</b><br/>MCP server"]
    S3["<b>Internal server</b><br/>MCP server"]

    Muse -- "REST over HTTPS" --> U1
    Muse --> U2
    Muse --> U3
    Owner -. "administers" .-> Shared
    K1 -- MCP --> S1
    K2 -- MCP --> S2
    K3 -- MCP --> S3

    classDef policy stroke:#4f8fd6,stroke-width:2px;
    class P1,P2,P3 policy;
```

## Contents

**Understand it**
[What is it?](#what-is-it) · [What can you use it for?](#what-can-you-use-it-for) · [Why use it?](#why-use-it) · [How it works](#how-it-works) · [Key concepts](#key-concepts)

**Get started**
[Quick start](#quick-start) · [Step-by-step setup](#step-by-step-setup) · [Settings file and automation](#settings-file-and-automation) · [Doing it by hand](#doing-it-by-hand) · [Troubleshooting](#troubleshooting)

**Use it**
[Day to day](#day-to-day) · [Stay in control: your policy](#stay-in-control-your-policy) · [What Muse sees](#what-muse-sees)

**Reference**
[OpenAPI description](#how-the-adapter-exposes-an-openapi-description) · [Authentication](#how-authentication-works) · [Run it by hand](#run-it-by-hand-development) · [Provider packs](#reference-provider-packs) · [Scope](#scope) · [Documentation](#documentation) · [Development](#development) · [License](#license)

## What is it?

[MCP](https://modelcontextprotocol.io) (Model Context Protocol) is how applications such as issue trackers, wikis, documentation sites, and databases publish tools, data, and prompt templates. Muse is a personal AI agent that adds services through Custom Connectors, which it reads from an OpenAPI description. Muse does not speak MCP, so MCP servers are out of its reach.

The adapter bridges the two:

- **Toward Muse** it is an ordinary REST API with an OpenAPI description. Muse adds it in chat like any custom connector.
- **Toward the MCP server** it is a careful MCP client, so the server sees a normal MCP conversation.
- **In between** it applies your rules: what is exposed, what counts as a read or a write, and the limits on each call.

For Muse it behaves almost like a transparent wrapper: using a server through the adapter is as good as talking to the MCP server directly, and the few differences are deliberate and visible.

## What can you use it for?

- **Look things up from Muse.** Connect a documentation or knowledge server, such as Microsoft Learn, and Muse can search official documentation and read whole pages while you chat. Ask "How do I raise the function timeout for Azure Functions on the Consumption plan?" and Muse searches, then answers from the real documentation.
- **Work with your tools through chat.** Connect an issue tracker, a wiki, or a database server, such as Linear or Atlassian, so Muse can read records and, with your confirmation, change them. Every write is marked, confirmed with you first, and runs once.
- **Share only part of a server.** A server may offer dozens of tools. Publish just the read-only ones, or just the three you need, and the rest do not exist for Muse.
- **Bring your own MCP servers to Muse.** Any MCP server, including one you wrote, becomes a Muse connector. You do not hand-write an API description or build a separate connector for each tool.
- **Keep credentials out of Muse.** Muse holds one revocable token per connector. The keys and sign-ins for the MCP servers stay inside the adapter.
- **Stay in control as servers change.** When a server adds, removes, or alters a tool, you see the difference and decide whether Muse gets it.
- **See what Muse did.** Every call is recorded with its operation, outcome, and duration, and a write's state is tracked until it is settled.

## Why use it?

MCP servers change on their own: tools appear, schemas shift, and a tool marked "read-only" may not be. Connecting Muse to that directly would mean handing Muse your credentials and letting upstream changes alter what it can do without anyone noticing.

- **You decide what Muse can reach.** Nothing is exposed until you review it and publish it. New or changed upstream tools never go live on their own.
- **Permissions are yours, not the server's.** You classify each operation as read, write, or sensitive write. What the server claims about itself is only a hint.
- **Writes are guarded.** Muse confirms with you before a write. The adapter runs it once, and if the outcome is unclear it never retries blindly.
- **Credentials stay with the adapter.** Muse gets one token per connection. The credentials for the MCP server never reach Muse.
- **Changes are caught.** The adapter compares each live server with what you published and reports anything added, removed, or changed.

The trade-off is one more service to run. For a single simple, stable MCP server you may not need it. It pays off when you want several servers behind one reviewed, consistent boundary.

## How it works

```mermaid
flowchart LR
    A["<b>1 Create the deployment</b><br/>muse-vm setup<br/>builds and starts the adapter,<br/>gives it a public address"] --> B["<b>2 Connect a server</b><br/>muse-vm add-server<br/>registers it and discovers<br/>what it offers"]
    B --> C["<b>3 Policy</b><br/>read-only tools are approved,<br/>everything else waits<br/>for you"]
    C --> D["<b>4 Publish</b><br/>the approved catalog goes live;<br/>you get a connector URL<br/>and a token"]
    D --> E["<b>5 Add to Muse</b><br/>tell Muse the URL,<br/>paste the token"]
    D -. "when the server changes:<br/>discover again, review, expose" .-> C

    classDef you stroke:#4f8fd6,stroke-width:2px;
    class C you;
```

1. **You run the adapter on a small Linux machine you control.** One guided command, `muse-vm setup`, builds it from this repository and starts it. The machine gets a public HTTPS address through Tailscale Funnel, and nothing on it accepts inbound connections.
2. **You connect an MCP server.** `muse-vm add-server` registers the server, looks at what it offers, and drafts a policy that exposes only its read-only tools. You decide whether to expose more.
3. **You add the connector to Muse.** You give Muse a connector address and a token. Muse reads the API description and can now use the tools you approved.
4. **Every call is checked.** When Muse calls a tool, the adapter confirms who is calling, that the operation is on your list, and that the input is valid. It then calls the MCP server with its own credential, checks the answer, records the call, and replies.

```mermaid
sequenceDiagram
    autonumber
    participant M as Muse
    participant A as Adapter
    participant S as MCP server
    M->>A: call an operation, with the connection's token
    Note over A: Who is calling? The token identifies one connection
    Note over A: Is it allowed? The operation is in your approved policy
    Note over A: Is it valid? The request matches the published schema
    Note over A: Run before? A repeated write is never run twice
    A->>S: call the server over MCP, with this connection's credential
    S-->>A: the answer
    Note over A: Check the answer: type, size, schema. Content is only data
    Note over A: Record: audit entry and operation state
    A-->>M: the result, or a clear reason it was refused
```

You run the adapter in your own environment, on your own machine and with your own accounts. There is no shared service in the middle, and no one else holds your credentials.

## Key concepts

| Term | What it means |
| --- | --- |
| **Server** | An MCP server you register by its address, such as Microsoft Learn or Linear. |
| **Policy** | Your list of what Muse may use. Anything not on it does not exist for Muse. |
| **Catalog** | The approved, versioned result of your policy. This is what Muse sees, and you can roll back to an earlier version. |
| **Connection** | One link between Muse and **one** MCP server: its own connector URL, token, policy, and credential. Add a second server and you get a second connection. It is a record inside the adapter, not a separate program, so one adapter process holds as many connections as you create. |
| **Connector URL** | The address you give Muse for a connection. Its random part only keeps the address private and routes the request to the right connection. It is not the security: every call still needs the connection's token. |

## Quick start

You need a computer with Python 3.12+ and `ssh`; a free [Tailscale](https://tailscale.com) account; and either a Google Cloud project (with the `gcloud` CLI signed in) or a Linux machine you can SSH into.

```bash
pipx install git+https://github.com/sept-usc/muse-mcp-adapter    # or: pip install ...
muse-vm setup
```

(No `pipx`? `python3 -m pip install --user pipx`, or use a virtual environment and `pip install` the same address. To review the code first, or to pin a commit, [clone it](#step-1--get-the-code) instead.)

`muse-vm setup` is a guided run. It checks that everything is in place, asks where the adapter should run, creates it, opens the two Tailscale links you need to click, lets you pick the MCP servers Muse should use, and prints what to give Muse. It asks only for what it cannot work out itself, and it takes 10 to 20 minutes, most of it building.

Then tell Muse: *"Create a connector called GitHub from the API spec at `<the connector address setup printed>`"*, click **Connect**, and paste the token. That's it.

The rest of this page explains each stage, shows how to set everything in one file or run it without prompts, and has a [troubleshooting guide](#troubleshooting).

## Step-by-step setup

```mermaid
flowchart LR
    A["<b>1 Get the code</b><br/>clone and install<br/>muse-vm"] --> B["<b>2 Run setup</b><br/>muse-vm setup"]
    B --> C["<b>3 Answer a few questions</b><br/>where it runs,<br/>which servers"]
    C --> D["<b>4 Click the Tailscale links</b><br/>sign in and approve"]
    D --> E["<b>5 Connect Muse</b><br/>paste the address<br/>and token"]
```

### Step 1 · Get the code

```bash
pipx install git+https://github.com/sept-usc/muse-mcp-adapter
```

**What this does.** It installs the `muse-vm` command. The adapter itself is built later, on the machine, from the code that came with the install: the container recipe, the hash-locked dependency list and the egress guard all travel inside the package, so there is nothing else to download. `muse-vm` records which release the images were built from.

**To review and pin the code first** (recommended for production), clone it and install from your checkout instead:

```bash
git clone https://github.com/sept-usc/muse-mcp-adapter
cd muse-mcp-adapter
git checkout <the tag or commit you have reviewed>
python3 -m venv .venv && .venv/bin/python -m pip install .
```

From a checkout, `muse-vm` builds from that checkout and records the commit. It refuses to deploy a checkout with uncommitted changes (`--allow-dirty` overrides), so every deployment matches a commit you can name again. In the examples below, `muse-vm` is `.venv/bin/muse-vm` in that case.

**Not sure everything is in place?** Run `muse-vm doctor` first. It checks Python, `ssh`, what the images will be built from, and (if you use Google Cloud) that `gcloud` is signed in with valid credentials and a project, and tells you how to fix anything that is missing, before anything is created.

### Step 2 · Run setup

```bash
.venv/bin/muse-vm setup
```

`setup` works through these stages. Each is an ordinary `muse-vm` command, so nothing happens that you could not do by hand.

| Stage | What you do | What happens |
|---|---|---|
| **Where** | Pick *a new Google Cloud VM* or *a Linux machine I already have*. If `gcloud` is signed in, your project is already chosen for you. For your own machine, type its SSH address (`user@host`). | It shows what the choice costs, and asks before creating anything billable. |
| **Check** | Nothing. | The doctor runs against the target: tools, credentials, SSH and `sudo`, a supported Linux, reachability of Docker Hub and PyPI. If something is wrong it says what and how to fix it, and stops before creating anything. |
| **Create** | Click the Tailscale links (next section). | Google Cloud: a network of its own, a shielded VM with no service account and no public IP, SSH only through Identity-Aware Proxy, daily snapshots. Your own machine: only what is missing is installed (an existing Docker is never restarted). Then the images are built from your checkout and the adapter starts. A self-check tests it from outside and inside. |
| **Servers** | Choose from the well-known servers by number or name, or press Enter to skip. Paste a token if a server needs one. | Each server is connected, its tools are discovered, and only the read-only ones are exposed. You get a connector address and token for each. |
| **Save** | Say yes to save your answers. | They are written to `muse-vm.toml` (no secrets in it), so the next run needs no questions. |

**The Tailscale links.** Tailscale gives the adapter its public HTTPS address (`https://<name>.<your-tailnet>.ts.net`) without opening a single port, owning a domain, or giving the machine a public IP. There is nothing to prepare in the Tailscale console: during **Create**, `muse-vm` opens up to two links in your browser. The first signs the machine in to your Tailscale account. The second, shown only if your tailnet has not used Funnel before, is Tailscale's own one-click approval for HTTPS certificates and Funnel. Click approve in each and the run carries on by itself. If your tailnet requires device approval, approve the new device under Machines.

**Which servers?** `muse-vm presets` lists them: Microsoft Learn (no sign-in), GitHub (a token), Linear and Atlassian (you approve access in your browser). Any other MCP server works too: `muse-vm add-server myserver --endpoint https://…`.

**Tokens.** A token is typed at a hidden prompt on your computer and stored in a root-only folder on the machine. It never appears in a command line, a log or a database. For GitHub, create a [fine-grained token](https://github.com/settings/personal-access-tokens/new) limited to the repositories Muse should use; read access is enough to start.

**You will know it worked when** `setup` ends with `The adapter is running at https://…`, eleven green checks during Create, and a connector address for each server.

### Step 3 · Connect Muse and try it

1. Open a **new side chat** in Muse (not your main chat) and send: `Create a connector called GitHub from the API spec at <the connector address>`.
2. Muse reads the API description and shows a **Connect** card. Click **Connect** and accept Muse's consent dialog.
3. Paste the **token** when Muse asks. (Or, if Muse offers it, sign in with the **connection code**.)
4. Muse says the connector is connected. Try it:
   - *"List the open pull requests in my repository `<owner>/<repo>`."* (a read)
   - *"Create an issue titled 'test' in that repository."* (a write; only if you allowed writes, below)

**You will know it worked when** Muse answers with real data and `muse-vm logs` shows the matching `POST …/operations/<name> 200` lines.

### Letting Muse change things

Writes are never exposed by default. To let Muse use specific write tools, publish a new version:

```bash
.venv/bin/muse-vm expose github --allow issue_write --allow add_issue_comment --allow create_pull_request
```

It adds exactly those tools; everything already published keeps its settings, and the connector address and token do not change. Use `--deny TOOL` to withdraw one. Muse asks you before it calls a write, and the adapter runs each write once: if it cannot tell whether a write happened, it marks it `UNKNOWN` and never retries it blindly. See [Stay in control](#stay-in-control-your-policy).

## Settings file and automation

Everything `setup` asks can also be written down once in `muse-vm.toml` in the folder you run it from (or passed with `--config`). Every key is optional. A person at a terminal is asked for what is missing; a script gets an error naming it. Unknown keys are rejected, so a typo never silently does nothing.

```toml
name = "prod"                    # deployment name (default: muse)
cloud = "gcp"                    # or: ssh = "deploy@203.0.113.7"
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

## Doing it by hand

`setup` is a front end to these commands, which stay available for scripts and for finer control. All options are listed in the [deployment reference](docs/deployment.md#commands).

```bash
.venv/bin/muse-vm up --cloud gcp --project my-project        # or: --ssh deploy@203.0.113.7
.venv/bin/muse-vm add-server github                          # a well-known server needs nothing else
.venv/bin/muse-vm add-server tracker --endpoint https://tracker.example.com/mcp --credential env_token
.venv/bin/muse-vm expose github --allow issue_write
```

`up` takes many more options (`--zone`, `--machine-type`, `--budget-usd`, `--idle-stop-hours`, `--egress-allow`, `--ssh-opt`, `--image`, `--keep-on-failure`, `--no-browser`) and, if you prefer not to click a Tailscale sign-in link, a Tailscale auth key in `TS_AUTHKEY`: a single-use, untagged key that expires in a day, generated under **Settings, Keys** in the Tailscale console. The key is removed from the machine as soon as the device has joined.

## Troubleshooting

Start with the commands that explain most problems:

```bash
.venv/bin/muse-vm doctor        # before creating anything: what is missing, and how to fix it
.venv/bin/muse-vm selfcheck     # after: each line says what passed or failed
.venv/bin/muse-vm logs          # the adapter's log; also: logs tailscale, logs guard
```

Add `-v` to any `muse-vm` command to print every command it runs. If `up` fails it removes what it created; add `--keep-on-failure` to keep the machine for inspection.

### While creating the deployment (`muse-vm setup` or `muse-vm up`)

| What you see | Why | What to do |
|---|---|---|
| `gcloud credentials … have expired` | `gcloud` still lists your account but its stored sign-in has expired | `gcloud auth login`, then run again |
| `Python 3.12 or newer`, `ssh` or `git` is marked ✗ | A tool is missing on your computer | Install it; `muse-vm doctor` says which |
| `your tailnet does not allow HTTPS and Funnel yet` | Your Tailscale network has not used Funnel before | Open the approval link it prints (it also opens in your browser) and approve. It keeps waiting, so you can do it while it runs. If it printed steps instead of a link: enable HTTPS certificates under **DNS** in the Tailscale console, and add `"nodeAttrs": [{"target": ["autogroup:member"], "attr": ["funnel"]}]` to the access controls |
| It prints a Tailscale sign-in link and then times out | The link expired before it was opened (they last a few minutes) | Use an auth key (`TS_AUTHKEY`), or open the newest link as soon as it appears |
| `this tailnet requires device approval` | Your tailnet approves new devices | Approve the device under **Machines** in the Tailscale console |
| `That does not look like a Tailscale auth key` | The key was pasted with extra text, or is not a `tskey-` key | Generate a new one and save it without spaces or prefixes |
| The address does not resolve, or `up` waits a long time at the self-check | A brand-new Funnel name can take up to ten minutes to reach public DNS | Wait. `muse-vm selfcheck` checks again, and uses public DNS so a computer that is itself in your tailnet is not misled |
| The address ends in `-1` or `-2` | An older device with the same name is still in your tailnet | Harmless. Delete the stale offline device under **Machines**. `muse-vm down` signs devices out, so this happens only after an interrupted run |
| `Cannot run commands on … with sudo -n` | SSH key login or passwordless `sudo` is missing on the machine | Fix the login (`ssh user@host sudo -n true` must work), then run `up` again |
| Google Cloud: it waits for SSH for several minutes | A new VM is still booting and installing Docker, or IAP access is not granted | Give it up to 10 minutes. It retries dropped connections itself. If it never connects, check that your account has the IAP tunnel and OS Login roles |
| `A network named mma-… already exists` | A previous run left something behind | `muse-vm down --name <name>`, or choose another `--name` |
| `setup` says the deployment already exists | You ran it again | That is fine: it keeps the deployment and only adds the servers you ask for |
| The image build fails or hangs | The machine cannot reach Docker Hub or PyPI | Allow outbound HTTPS from the machine, or build the images elsewhere and use `--image` and `--guard-image` ([reference](docs/deployment.md#how-the-images-are-built)) |
| `The checkout … has uncommitted changes` | You edited files that are deployed | Commit or stash them, so the deployment matches a commit |
| The self-check fails on `/health` but the checks inside the machine pass | The public address is not reachable from here yet, or Tailscale is disconnected | `muse-vm logs tailscale`; check the device is online in the Tailscale console |

### While connecting a server (`muse-vm add-server`)

| What you see | Why | What to do |
|---|---|---|
| `The server offers no such tool` | A tool name in `--allow` or `--deny` is misspelled | The message lists the tools the server offers |
| It refuses a private or `http://` address | By design: the adapter only calls public HTTPS servers | Use the server's public HTTPS address. A server on a private network needs `--egress-allow` and a deliberate policy decision |
| The server returns an authentication error | The token is wrong, expired, or lacks permission | `muse-vm secret set NAME` again with a correct token. For GitHub, check the token's repository access and permissions |
| An OAuth server shows "reconnect" | The sign-in expired | Open the link it prints and approve again |

### While connecting or using Muse

| What you see | Why | What to do |
|---|---|---|
| Muse says it cannot create the connector from that address | Muse accepts only ordinary public hostnames, not ones that encode an IP (`…sslip.io`) | Use the `*.ts.net` address `muse-vm up` printed |
| Muse says 401 or "token invalid" | The token was rotated, belongs to a different connector, or has a typo | `muse-vm rotate-token SERVER` and paste the new token |
| Muse says the server is unavailable | The machine is stopped, or Tailscale is disconnected | `muse-vm status`; `muse-vm start` |
| Muse cannot use a tool you expected | It is not published, or is a write | `muse-vm expose SERVER --allow TOOL` |
| A write shows `UNKNOWN` | The call may or may not have happened | Check the other system, then settle it with the adapter's `reconcile` command ([usage guide](docs/usage.md#1-command-line-tool)). It is never retried automatically |
| Muse reports success but nothing changed | An identical write within 30 seconds of an earlier one is treated as a retry and replays the first result | The response says `idempotentReplay`. Ask again after a moment, or send an `Idempotency-Key` |

### While it runs

| What you see | Why | What to do |
|---|---|---|
| The adapter stopped | The machine rebooted, or a container crashed | Both recover by themselves, usually within a minute. `muse-vm status` shows the state |
| The disk is filling up | Logs and backups | Container logs rotate at 50 MB; delete old backups you no longer need |
| You lost the backup passphrase | Backups are encrypted with it and cannot be recovered | Make a new backup with a new passphrase while the machine is healthy |
| After a restore, a server stops working | Server tokens are not part of a backup | `muse-vm secret set NAME` again |

### Still stuck?

Collect `muse-vm status`, `muse-vm selfcheck` and `muse-vm logs` (they contain no secrets), then see [Running on a VM](docs/operations.md) for more, or open an issue with that output.

## Day to day

| Task | Command |
|---|---|
| Health, containers, connected servers | `muse-vm status`, `muse-vm selfcheck`, `muse-vm servers` |
| Add another MCP server | `muse-vm add-server <name>` (see `muse-vm presets`); it gets its own connector and token |
| Change what Muse can use | `muse-vm expose SERVER --allow TOOL` / `--deny TOOL` |
| Logs | `muse-vm logs` (adapter), `muse-vm logs tailscale` |
| New version | `pipx upgrade muse-mcp-adapter` (or `git pull` in your checkout, and review what changed), then `muse-vm upgrade`: the address, connections and data stay |
| Backup and restore | `muse-vm backup --out FILE` (encrypted with a passphrase you choose), `muse-vm restore FILE` |
| A leaked token | `muse-vm rotate-token SERVER` |
| Pause to save money | `muse-vm stop`, `muse-vm start` (on Google Cloud this powers the VM off) |
| Remove everything | `muse-vm down` (signs the device out of Tailscale and deletes what was created) |

Reference for everything above: [deployment reference](docs/deployment.md) (what is installed, every command), [running on a VM](docs/operations.md), [identity and authentication](docs/authentication.md) (every credential and how it is protected) and [network exposure](docs/networking.md) (what is reachable from where).

## Stay in control: your policy

`policy-template` drafts a policy from what the server offers. It starts closed: `--expose-reads` pre-approves everything read-only (tools the server marks read-only, plus its resources and prompts), and writes always start switched off. You edit the file, and only entries with `"allowed": true` are exposed.

| You decide | Field | Default |
| --- | --- | --- |
| Is it exposed to Muse? | `allowed` | No |
| Read, write, or sensitive write? | `classification` | Write, unless the server marks the tool read-only and you accept that |
| What Muse is told about it | `description` | The server's text, pinned at publish so the server cannot change it later |
| The name Muse sees | `publicOperationId` | Derived from the tool's name |
| How long and how large | `timeoutSeconds`, `maxResultBytes` | 30 seconds, 256 KB |
| Which resources and arguments | `resource` block (`allowedUriSchemes`, `argumentPatterns`, ...) | Strict |

A single entry, taken from a real Microsoft Learn policy:

```json
{
  "primitiveKey": "tool:microsoft_docs_search",
  "allowed": true,
  "publicOperationId": "microsoft_docs_search",
  "classification": "read",
  "description": "Search official Microsoft/Azure documentation ...",
  "timeoutSeconds": 30,
  "maxResultBytes": 262144
}
```

The adapter enforces this on every request:

```mermaid
sequenceDiagram
    autonumber
    participant M as Muse
    participant A as Adapter
    participant S as MCP server
    M->>A: call an operation, with the connection's token
    Note over A: Who is calling? The token identifies one connection
    Note over A: Is it allowed? The operation is in your approved policy
    Note over A: Is it valid? The request matches the published schema
    Note over A: Run before? A repeated write is never run twice
    A->>S: call the server over MCP, with this connection's credential
    S-->>A: the answer
    Note over A: Check the answer: type, size, schema. Content is only data
    Note over A: Record: audit entry and operation state
    A-->>M: the result, or a clear reason it was refused
```

If the server changes later, run `discover-remote` again and then `drift` to see what was added, removed, or altered. Muse keeps seeing the published catalog until you review the change and publish a new version, and `rollback` returns to the previous one. See the [policy reference](docs/usage.md#2-policy-file) for every field.

## What Muse sees

Every connection exposes the same small, fixed API under its own URL:

| Request | Purpose |
| --- | --- |
| `GET /c/<slug>/openapi.json` | The API description Muse reads when you add the connector. It lists only the operations you approved. |
| `POST /c/<slug>/operations/<name>` | Calls one approved tool, resource, or prompt. |
| `GET /c/<slug>/executions/<id>` | Looks at one earlier call: its status, progress, and log messages. |
| `POST /c/<slug>/executions/<id>/resume` | Answers a question the server asked in the middle of a call. |

The routes never change. What the server offers is reflected in the generated description, which lists the operation names and their inputs. Every request carries the connection's token, and a request without a valid one is refused.

### Responses

| Status | Meaning |
| --- | --- |
| `200` | The operation completed. |
| `400` | The request did not match the operation's input schema. |
| `401` | The token is missing, invalid, or revoked, or the linked account must sign in again (the error then includes a fresh sign-in link). |
| `403` | The connection may not use this operation, or the connector is disabled. |
| `404` | The operation or execution does not exist, or is not in the published catalog. |
| `409` | The operation needs an answer, is still running, or its outcome is unknown and must not be retried blindly. |
| `413` | The request body is too large. |
| `429` | Too many requests. |
| `502` | The MCP server failed, timed out, or returned something outside your limits. The message says which. |

### How MCP behaviour shows up through the API

| In MCP | Through the adapter |
| --- | --- |
| A tool that changes data | The operation is marked `x-requires-confirmation`, so Muse confirms with you before calling it. |
| A repeated write | An `Idempotency-Key` (Muse's, or one derived from the request) makes a repeat return the stored result, flagged `idempotentReplay`, instead of running again. |
| A write whose outcome is unclear | `409` with `UNKNOWN_OUTCOME`. It is not retried until you check the system and run `reconcile`. |
| The server asks a question mid-call | `409` with `INPUT_REQUIRED`, the question, and how to answer it. Muse asks you, then calls `.../resume` with your answer. |
| Progress and log messages | Returned with the result. |
| Images, audio, linked and embedded content | Returned as typed content blocks. |
| A result over your size limit, or a call over your time limit | `502` with a message that names the limit, so it is not mistaken for an outage. |
| A tool error | `502` with the server's message. |

---

*Everything below is reference material for when you need the detail.*

## How the adapter exposes an OpenAPI description

Muse learns what it can call from an OpenAPI description, so the adapter produces one for every connection. You never write it. It is generated from what the MCP server offers, filtered by your policy.

```text
discover        the adapter asks the MCP server what it offers and stores the answer
policy          you choose what is exposed
publish         the approved subset becomes the live, versioned catalog
Muse asks       the adapter compiles the OpenAPI description from that catalog, right then
```

The description is served at the connector URL (`/c/<slug>/openapi.json`). It is built from the approved catalog on every request, so it only ever lists approved operations, and it changes only when you publish a new version. It contains no secrets and no MCP endpoint.

### How MCP maps to OpenAPI

Every approved tool, resource, resource template, or prompt becomes one operation: `POST /c/<slug>/operations/<name>`.

| MCP | Becomes | Request body |
| --- | --- | --- |
| **Tool** | An operation | The tool's own input schema |
| **Resource** | A read operation that names the resource | None |
| **Resource template** such as `issues://{project}/{id}` | A read operation | One required field per template variable |
| **Prompt** | A read operation that returns the prompt's messages | One field per prompt argument |

### What each operation contains

| In the description | Where it comes from |
| --- | --- |
| `operationId` and path | The name you chose in your policy (derived from the tool's name by default) |
| `description` | The text pinned in your policy, plus a note on whether it reads or changes data |
| Request body schema | The MCP tool's input schema, translated to OpenAPI 3.1 |
| `x-muse-classification` | Your classification: `read`, `write`, or `sensitive_write` |
| `x-requires-confirmation` and an `Idempotency-Key` header | Added to every write, so Muse confirms with you first and a retry never runs twice |
| `responses` | A fixed set, listed below |
| `security` | Both ways to authenticate, described under authentication |

A real excerpt, generated from Microsoft Learn's `microsoft_code_sample_search` tool:

```json
{
  "/operations/microsoft_code_sample_search": {
    "post": {
      "operationId": "microsoft_code_sample_search",
      "summary": "Microsoft Code Sample Search",
      "description": "Search for code snippets in official Microsoft Learn documentation. ... Read-only: retrieves information and does not change anything.",
      "x-muse-classification": "read",
      "requestBody": {
        "content": {"application/json": {"schema": {
          "type": "object",
          "properties": {
            "query": {"type": "string", "description": "a descriptive query, SDK name, method name ..."},
            "language": {"type": ["string", "null"], "description": "Optional programming language filter ..."}
          },
          "required": ["query"]
        }}}
      },
      "responses": {"200": {"...": "..."}, "400": {"...": "..."}, "401": {"...": "..."}, "409": {"...": "..."}, "502": {"...": "..."}}
    }
  }
}
```

Every operation answers with the same result shape: the content the server returned (text, images, audio, links, or structured data), the operation's status, and any progress and log messages the server sent while working.

## How authentication works

There are two separate sign-ins, and they never mix. Muse never sees the credential for the MCP server, and the server never sees Muse's token.

**Muse to the adapter**: proves Muse may use this connection.

```mermaid
flowchart LR
    A1["<b>1 You run link</b><br/>the adapter generates<br/>a URL, a token and a code"] --> A2["<b>2 The adapter keeps hashes</b><br/>of the token and the code,<br/>never the values"] --> A3["<b>3 You give Muse the token</b><br/>paste it into Muse,<br/>or sign in with the code"] --> A4["<b>4 Every call carries it</b><br/>the URL picks the connection,<br/>the token proves the caller"]

    classDef key stroke:#4f8fd6,stroke-width:2px;
    class A2,A4 key;
```

**Adapter to the MCP server**: proves who you are to the server. Neither side's credential is ever sent to the other.

```mermaid
flowchart LR
    Adapter["<b>Adapter</b><br/>holds the credential for<br/>this one connection"]
    Adapter --> O1["<b>None</b><br/>public servers,<br/>such as Microsoft Learn"]
    Adapter --> O2["<b>A token</b><br/>read from a root-only file,<br/>never saved in the database"]
    Adapter --> O3["<b>Your own sign-in (OAuth)</b><br/>approved once; the tokens are<br/>kept encrypted and refreshed"]
    O1 --> Server["<b>MCP server</b>"]
    O2 --> Server
    O3 --> Server

    classDef key stroke:#4f8fd6,stroke-width:2px;
    class O3 key;
```

### Muse to the adapter

1. You run `link`. The adapter **generates** a connector URL, a token, and a connection code, prints them once, and keeps only a hash of the token and of the code.
2. You give Muse the token, in one of two ways. **Paste the token** into the secure field Muse shows when it adds the connector, or **sign in**: Muse opens a page, you enter the connection code, and Muse receives short-lived tokens that it refreshes itself. The adapter offers both in its API description, and Muse picks one.
3. From then on every request carries `Authorization: Bearer <token>`. The adapter uses the connector URL to find the connection, hashes the token, and compares it with that connection's stored hash. A token for one connection is refused by every other, and a request without a valid token is refused with a 401.

The random part of the connector URL only selects the connection and keeps its API description unlisted. The token is what authenticates. If a token or code leaks, `rotate-token` ends every session for that connection, and `unlink` removes the connection.

**How Muse learns which authentication to use.** The API description declares both options in its `security` section, and Muse picks one:

```json
"security": [{"oauth2": ["connector"]}, {"bearerAuth": []}]
```

- `bearerAuth` is a plain bearer token. Muse asks you for the value.
- `oauth2` is an authorization-code flow with PKCE, served by the adapter itself. The description lists its authorize, token, and refresh addresses. The adapter acts as the authorization server for Muse: it registers Muse as a client, asks for the connection code on a consent page, then issues access tokens (valid for an hour) and refresh tokens that rotate on every use.

A request without a token also gets a `401` with a `WWW-Authenticate` header that points to the adapter's standard OAuth discovery document, so a client that does not read the description can still find out how to sign in.

### The adapter to the MCP server

You choose this per connection when you run `link --credential ...`:

| `--credential` | Use it when | How it works |
| --- | --- | --- |
| `none` | The server is public, such as Microsoft Learn. | Nothing is sent. |
| `env_token` | The server takes an API key or token. | The adapter reads it when it needs it, from the environment variable you name with `--token-env` or, on a VM, from a file in the root-only secrets folder (`muse-vm secret set`). It is never written to the database. |
| `oauth` | The server signs users in with OAuth, as Linear and Atlassian do. | `link` prints a one-time sign-in link. You open it and approve the adapter at the server's own sign-in page. The server sends your browser back to the adapter, which exchanges the result for tokens, stores them encrypted, and refreshes them itself. Muse is not part of that redirect and never sees the tokens. If they ever stop working, the adapter tells Muse to reconnect and includes a fresh sign-in link. |

## Run it by hand (development)


You do steps 1 to 5 once per server. Step 6 happens in Muse's chat.

Requires Python 3.12+.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Muse needs to reach the adapter over HTTPS. For a trial on your own machine, a tunnel such as `ngrok http 8080` gives you a public address. Put it in `serve.json`:

```json
{"publicUrl": "https://<your-public-address>"}
```

### Try it with a public server

Microsoft Learn's MCP server needs no sign-in and only reads documentation, so it is a safe first server.

```bash
adapter=".venv/bin/muse-mcp-adapter --database adapter.db --config serve.json"

$adapter server-register --server-id learn --display-name "Microsoft Learn" \
  --endpoint https://learn.microsoft.com/api/mcp --auth-profile none
$adapter discover-remote --server-id learn
$adapter policy-template --server-id learn --expose-reads > policies.json
$adapter publish  --server-id learn --catalog-version 1.0.0 --policies policies.json
$adapter activate --server-id learn --catalog-version 1.0.0
$adapter link --server-id learn --label "Microsoft Learn" --credential none
$adapter serve
```

`link` prints the connector URL, a token, a connection code, and the sentence to tell Muse. The token and code are shown once.

In Muse, ask it to create a connector from that URL, then paste the token when it asks (or sign in with the connection code). Muse reads the public API description, and from then on can use the server's tools.

### A server that needs a token

```bash
export PROJECT_SYSTEM_TOKEN=...   # the server's API key, kept in your environment

$adapter server-register --server-id project-system --display-name "Project System" \
  --endpoint https://mcp.example.com/mcp --auth-profile bearer_token
$adapter discover-remote --server-id project-system --token-env PROJECT_SYSTEM_TOKEN
$adapter policy-template --server-id project-system --expose-reads > policies.json
$adapter publish  --server-id project-system --catalog-version 1.0.0 --policies policies.json
$adapter activate --server-id project-system --catalog-version 1.0.0
$adapter link --server-id project-system --label "Project System" \
  --credential env_token --token-env PROJECT_SYSTEM_TOKEN
```

### A server that signs you in with OAuth

Create the connection first, sign in through the link it prints, then discover with that connection's credential:

```bash
$adapter server-register --server-id linear --display-name Linear \
  --endpoint https://mcp.linear.app/mcp --auth-profile oauth
$adapter link --server-id linear --label Linear --credential oauth     # prints a sign-in link
# open the link in your browser and approve access at the server's page
$adapter discover-remote --server-id linear --connection-id <id printed by link>
$adapter policy-template --server-id linear --expose-reads > policies.json
$adapter publish  --server-id linear --catalog-version 1.0.0 --policies policies.json
$adapter activate --server-id linear --catalog-version 1.0.0
```

The sign-in link and the provider's redirect both go through the adapter's public address, so `publicUrl` must be reachable from your browser. If a provider does not support dynamic client registration, add its client id under `oauthClients` in `serve.json` (see the [usage guide](docs/usage.md#3-running-the-service)).

Want to try it without Muse or a real provider? The [usage guide](docs/usage.md#try-it-locally) walks through a sample MCP server and a script that behaves like Muse.

## Reference provider packs

Provider packs hold endpoint and safety settings for common servers. They add no provider-specific code and never authorize discovered tools automatically.

| Pack | Endpoint | Safe starting point |
| --- | --- | --- |
| [Linear](https://linear.app/docs/mcp) | `https://mcp.linear.app/mcp` | Start from the documented read-only endpoint; classify and approve writes explicitly |
| [Atlassian](https://atlassian.github.io/atlassian-mcp-server/) | `https://mcp.atlassian.com/v2/mcp` | Use least-privilege scopes; classify and approve writes explicitly |
| [Supabase](https://supabase.com/docs/guides/ai-tools/mcp) | `https://mcp.supabase.com/mcp` | Require `project_ref`; `read_only=true` by default |

These are starting points, not a claim that everything a provider offers is safe to publish. Run discovery, review it, and write your own policy for the account you control.

## Scope

**Supported:** remote MCP servers over Streamable HTTP; tools, resources, resource templates, and prompts; progress and log messages, questions asked mid-call, and image, audio, and linked content; authentication to MCP servers with no credential, a static token, or OAuth.

**Not included:** local `stdio` servers, MCP Apps rendering, a hosted marketplace, or billing. Each deployment serves one owner.

## Documentation

- [Contributing](CONTRIBUTING.md) and [security policy](SECURITY.md)
- [Usage guide](docs/usage.md): configuration, policy reference, API, and operations
- [Deploying on a VM](docs/deployment.md): one command to host the adapter on Google Cloud or any Linux machine
- [Running on a VM](docs/operations.md): prerequisites, how it runs, and troubleshooting
- [Identity and authentication](docs/authentication.md): every identity and credential, and how each is protected
- [Network exposure](docs/networking.md): what is public, what is private, and what the adapter may reach
- [Security model](docs/security.md): controls and guarantees
- [Validation guide](docs/validation.md): how parity with a direct MCP connection is tested, and the [Atlas validation server](docs/validation-server.md)
- [System design](docs/design.md)
- [Control plane](docs/control-plane.md) and [data plane](docs/data-plane.md)

## Development

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'
git config core.hooksPath .githooks     # run the full gate before every commit
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to contribute. Nothing is committed unless the whole gate passes. `scripts/verify.sh` runs everything CI runs, locally and first: workflow syntax and a check that every GitHub Action version exists; lint, type-check and the full test suite on Linux with Python 3.12, and again in your own virtualenv; a build of both container images with a start-up check; a vulnerability scan of the images; a check that every link in the documentation resolves; and a render of every Mermaid diagram. It needs Docker running, and it fails rather than skipping a step when Docker is missing. Diagrams are written in Mermaid (a fenced `mermaid` block), not as images, so they render on GitHub, diff in a pull request and cannot go stale as a separate file. For quick feedback while you work, `scripts/verify.sh --quick` runs lint, types and tests on your machine only; it is not the commit gate.

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 SEPT Lab, University of Southern California.
