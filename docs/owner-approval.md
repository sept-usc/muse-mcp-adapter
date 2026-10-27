# Owner approval: nothing risky happens without you

By default the adapter treats the call itself as the confirmation: Muse asks you, then calls. That works while the agent behaves. If a message the agent reads carries a hidden instruction, the agent can call a write without ever asking you, and the adapter runs it.

**Owner approval** closes that gap. In `owner` mode the adapter decides, from rules you set, which writes must wait for you. A held write does not run until you have looked at the exact action on a page only you can approve from.

It is a different thing from asking in the chat. The agent cannot approve its own action, and a hidden instruction cannot talk the adapter out of holding it.

## How it works

1. The agent calls a write, for example `mail_send`.
2. The adapter checks its rules. If the action must wait, nothing is sent and the agent is told so, in plain words, with a one-time link.
3. The agent shows you the link. You open it and see **exactly what will happen**: the tool, every argument as it will be sent, and why you are being asked.
4. You type your **approval passcode** and choose Approve or Decline. The passcode is typed here and nowhere else, so it is never in the agent's context.
5. You tell the agent you decided. It calls the same operation again with the same arguments, and the adapter runs it, once.

An approval covers only that tool, that connection and those exact arguments. Change one character of the message and you are asked again. It is spent by one run, and it lapses after 15 minutes. A declined action stays declined, and trying it again under a new idempotency key does not get around it.

The link is visible to the agent, so it only opens the page. Deciding needs the passcode. Five wrong passcodes pause approvals for 15 minutes. A link is replaced with a fresh one each time the agent asks again while waiting, and the old one stops working.

## Turning it on

Set the passcode once, on the machine the adapter runs on. It is asked for twice and never shown, and it is stored only as a salted hash:

```bash
muse-mcp-adapter --database adapter.db owner-passcode
```

Then set `"approvalMode": "owner"` in the service configuration. The service refuses to start in this mode without a passcode.

## Deciding which writes wait: `ownerControls`

Each tool's policy may carry an `ownerControls` object (see the [policy reference](usage.md#2-policy-file)). With none, **every write waits for you**, and destructive tools (`sensitive_write`) always do.

| Setting | Meaning |
| --- | --- |
| `approval` | `always` holds every call. `risk` holds a call only when a signal below fires. `never` lets it run. `never` is refused for a `sensitive_write`. |
| `recipients` | Argument paths that say where something is sent, such as `["to"]` or `["attendees"]`. A value you have not accepted before is a signal. |
| `knownRecipients` | Patterns that count as already accepted, such as `["*@household.example"]`. |
| `untrusted` | On a **read**: what it returns was written by other people. A write soon after such a read is a signal, because that is how a hidden instruction reaches an action. |
| `untrustedWindowSeconds` | How long after such a read a write counts as following it (default 1800). |
| `arguments` | Hard limits: for an argument path, the patterns the value must match in full. A value outside them is **refused at once**, not held. |

Example: sending mail is held when it goes to someone new or follows reading someone else's mail, and can only ever go to your own domain:

```json
{
  "primitiveKey": "tool:mail_send",
  "allowed": true,
  "publicOperationId": "mail_send",
  "classification": "write",
  "maxResultBytes": 262144,
  "ownerControls": {
    "approval": "risk",
    "recipients": ["to"],
    "knownRecipients": ["*@household.example"],
    "arguments": {"to": [".+@household\\.example"]}
  }
}
```

and the mail-reading tool that makes it risky:

```json
{
  "primitiveKey": "tool:mail_read",
  "allowed": true,
  "publicOperationId": "mail_read",
  "classification": "read",
  "maxResultBytes": 262144,
  "ownerControls": {"untrusted": true}
}
```

The decision is deterministic. No model is involved, and nothing the agent says can change it. Hard limits in `arguments` apply in every approval mode.

On the approval page, tick "Do not ask again" and the recipients of that action are remembered for that connection.

## Being told without the agent

Set up email and the adapter tells you itself, in its own message, when it holds something, so a hidden or forgotten action does not just sit there. The message names the tool and why it was held and carries a link made for you alone (the agent never sees it and it keeps working when the agent asks again). It does not repeat the arguments; the page shows them. In the service configuration:

```json
"notifications": {
  "email": {
    "host": "smtp.example.com", "port": 587, "from": "adapter@example.com",
    "to": "you@example.com", "usernameEnv": "SMTP_USER", "passwordEnv": "SMTP_PASS"
  }
}
```

The mail credentials are read from the named environment variables. A message that cannot be sent never delays or fails the agent's call.

## From the command line

On the adapter's machine, with no page needed:

```bash
muse-mcp-adapter --database adapter.db approvals
muse-mcp-adapter --database adapter.db approval-decide --request-id <id> --approve --remember
muse-mcp-adapter --database adapter.db approval-decide --request-id <id> --decline
```

`approvals` shows each waiting action with its exact arguments. `--all` includes the decided ones.

## What it does not do

* **A held action the agent never mentions is only as visible as your email.** If the agent hides it, nothing is sent, which is safe. With email notifications set up, you are told anyway; without them you are not.
* **It is only as good as the person approving.** An owner who approves without reading approves an attack too. The [trust lab](trust-lab.md) measures this with an owner who reads and one who does not.
* **It looks at who and what is sent, not yet at what is inside.** An agent that pastes more about a person than it should into an approved message still gets that message through. Withholding sensitive values is a separate step.
* **Reads are not held.** Data that is read reaches the agent. Holding writes decides what the agent can *do* with it.
