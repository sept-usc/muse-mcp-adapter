# Sealed values: control what reaches the agent

An agent that reads your mail and contacts holds all of it for as long as it runs. Anything in its context can be repeated, pasted into a message, or sent to a stranger by a hidden instruction. Owner approval ([owner-approval.md](owner-approval.md)) decides what the agent may *do*. Sealed values decide what it gets to *know*.

For each tool, you say field by field what the agent sees. The adapter applies that to every result on the way out, and to every call on the way back in.

## The four rungs

| Rung | What the agent gets |
| --- | --- |
| `full` | The value, as it is. For things the agent needs to answer you, such as a phone number you asked for. |
| `handle` | An opaque token, `[contact.email:k3f9a2bq]`. The adapter keeps the value. |
| `withhold` | Nothing. The field is not in the result. For health notes, ID numbers, birthdays. |
| `redact` | The text, with card, national-ID and bank-account numbers blanked out: `[redacted:card]`. For free text written by other people, such as an email body. |

No model decides what is sensitive. You do, in the policy, and the adapter enforces it where the agent cannot argue with it.

## What a handle is for

A handle stands in for a value the agent has no business reading but may need to *use*. The usual case is an email address: to reply to Dana, the agent does not need to know her address, only to name her.

```
agent reads:   "from_email": "[contact.email:k3f9a2bq]"
agent sends:   mail_send {"to": "[contact.email:k3f9a2bq]", ...}
adapter sends: mail_send {"to": "dana.whitfield@family.example", ...}
```

The policy says where a handle may be filled in (`slots`). A handle anywhere else is **refused**, and the agent is told where it may go:

> `[contact.email:k3f9a2bq]` is a sealed value and cannot be used in 'body'. It can only be used in: to. Remove it and use only what the owner asked you to share.

So an agent that pastes everything it has read into a message gets an error, not a leak. A handle that is made up, edited, from another connection or expired is refused too. Handles are tied to one connection, last 24 hours, and are forgotten when you unlink the connection.

Properties worth knowing:

* **The same value always gets the same handle**, so the agent can compare and repeat it.
* **Values are encrypted at rest**, with a key kept in an owner-only file beside the database.
* **Your approval page and your hard limits see the real value.** When an approval is needed, the page shows `dana.whitfield@family.example`, not the handle. A limit like "only to my own domain" is checked against the real address.
* **A tool that repeats a value back is sealed again.** If the server's receipt says `"to": "dana.whitfield@family.example"`, the agent receives the handle.
* **Muse is told a field may be a handle.** In the published connector description, the argument that accepts handles has its `format` and length limits removed, so a field typed as an email address does not make Muse refuse a handle before the call. The adapter checks the real value against the original schema after filling it in.

## Writing the policy

Add a `release` block to a tool's policy (see the [policy reference](usage.md#2-policy-file)).

```json
{
  "primitiveKey": "tool:contacts_search",
  "allowed": true,
  "publicOperationId": "contacts_search",
  "classification": "read",
  "maxResultBytes": 262144,
  "release": {
    "fields": [
      {"path": "$.contacts[*].email", "label": "contact.email", "rung": "handle"},
      {"path": "$.contacts[*].address", "label": "contact.address", "rung": "handle"},
      {"path": "$.contacts[*].notes", "label": "contact.notes", "rung": "withhold"},
      {"path": "$.contacts[*].phone", "label": "contact.phone", "rung": "full"}
    ]
  }
}
```

```json
{
  "primitiveKey": "tool:mail_send",
  "allowed": true,
  "publicOperationId": "mail_send",
  "classification": "write",
  "maxResultBytes": 262144,
  "release": {"slots": {"to": ["contact.email"]}}
}
```

| Setting | Meaning |
| --- | --- |
| `fields` | A list of `{path, label, rung}`. `path` is `$` followed by `.name` and `[*]` steps. `label` names what the value is (`contact.email`, two or more lowercase words joined by dots). |
| `slots` | For arguments of this tool, the labels whose handles may be filled in there. A nested argument uses a dotted name (`meta.who`). |
| `patterns` | What `redact` blanks out: any of `card` (checked by the card number checksum), `ssn`, `iban`. Default: all three. |
| `text` | Rules for servers that answer in prose rather than fields. See [Servers that answer in prose](#servers-that-answer-in-prose). |
| `reveal` | Labels you may choose to show the agent, one value at a time. See [Letting the agent see one value](#letting-the-agent-see-one-value-reveal). |

The same value can be shown fully by one tool and sealed by another. A lookup of one person may show a phone number in full, while a bulk export of everyone's seals it. That is how you keep a convenient answer without letting one call hand over the whole book.

## Servers that answer in prose

Many servers return one block of formatted text instead of fields: a contact card as lines, a message as headers and a body. There is no path to point a field rule at. A `text` rule finds the value inside the text instead:

```json
{
  "primitiveKey": "tool:get_contact",
  "allowed": true,
  "publicOperationId": "get_contact",
  "classification": "read",
  "maxResultBytes": 262144,
  "release": {
    "text": [
      {"find": "email", "label": "contact.email", "rung": "handle", "keep": ["me@example.com"]},
      {"find": "phone", "label": "contact.phone", "rung": "handle"},
      {"find": "regex", "label": "contact.private", "rung": "withhold",
       "pattern": "(?m)^(?:Notes|Address|Birthday): .*$"}
    ]
  }
}
```

* `find` is `email`, `phone`, or `regex` with your own `pattern` (up to 200 characters), such as a `Notes:` line.
* `rung` is `handle` or `withhold`. A withheld match becomes `[withheld]`.
* `keep` lists values that stay readable whatever the rule, such as your own address.
* The rule applies to every string in the result, structured or plain text, and to every sender, recipient and body in it. The same address gets the same handle everywhere, so a sender shown in a header can be replied to by its handle in a `to` slot.

A text rule finds what is shaped like an address or a number, or what your pattern matches. It cannot know that a sentence names a diagnosis. For that, keep the sensitive field somewhere a rule can reach, or withhold the whole line it sits on.

## Letting the agent see one value: reveal

A sealed value is useless when you actually want it read out: "what is Dana's number?" Instead of leaving the value open everywhere, you can make some labels **revealable**. The value stays a handle until the agent asks to see that one handle and you approve that one request.

```json
{
  "primitiveKey": "tool:search_contacts",
  "allowed": true,
  "publicOperationId": "search_contacts",
  "classification": "read",
  "maxResultBytes": 262144,
  "release": {
    "text": [{"find": "phone", "label": "contact.phone", "rung": "handle"}],
    "reveal": ["contact.phone"]
  }
}
```

With `reveal` set on any tool of a server, the connector publishes one more operation, `reveal_value`, which takes a `handle`:

1. The agent calls `reveal_value` with the handle it was given. The adapter answers `409 APPROVAL_REQUIRED` with a one-time link, and tells the agent to hand it to you.
2. You open the link **in your own browser**, not one the agent controls. The page shows the label and **the value itself**, and asks for your passcode. You choose *Show it* or *Keep it sealed*.
3. If you approve, the agent calls again with the same handle and is shown the value, once. Asking again needs a new approval. If you decline, it stays declined.

Only labels listed in `reveal` can be asked for. Any other handle is refused with `HANDLE_NOT_ALLOWED_HERE`, a made-up handle with `HANDLE_UNKNOWN`. The approval is for this handle on this connection, lasts 15 minutes, and is spent by one reveal. The record counts each reveal by label and handle, never the value. The same approval mechanism, passcode and lockout as [owner approval](owner-approval.md) apply, and reveal needs `approvalMode: owner`.

Reveal does not help against a fooled agent that tricks you into approving. Read the page: it shows exactly what you are about to show.

## Check that your rules match

A path that does not fit what the server really returns protects nothing, and nothing says so. The adapter notices: when a rule matches no field in a result, it records an `unmatched` entry. Look for them after the first real calls:

```bash
muse-mcp-adapter --database adapter.db activity
```

## The record

Every sealing, withholding, redaction, fill-in and refusal is recorded with its label and its handle, never its value. Each entry carries the hash of the one before it, so an entry removed or changed later breaks the chain.

```bash
muse-mcp-adapter --database adapter.db activity --since 2026-10-26T00:00:00Z
muse-mcp-adapter --database adapter.db verify-ledger
```

`activity` is a digest: how many of each kind, per label, and whether the record is intact, plus how many actions are waiting for you. `verify-ledger` exits with an error and names the first entry that does not fit if the record was changed.

## What it does not do

* **It governs only what passes through the adapter.** Data the agent reaches some other way (a built-in connector, a browser, what you type in the chat, its own memory) is outside it.
* **Free text is only partly covered.** Card, national-ID and bank-account numbers are found by pattern, and `text` rules find addresses, phone numbers and whatever your own patterns match. Names, and health details written into a message body or a calendar title, are not found, so treat third-party text as untrusted and keep the sensitive fields where a rule can reach them.
* **A sealed address in a message body can still be sent to.** Sealing the addresses in an incoming message does not stop an agent from using one as a recipient; the handle works in a `to` slot. What decides whether that message goes is [owner approval](owner-approval.md).
* **A `full` field is gone the moment it is read.** Use it only for what the agent needs to answer you.
* **It does not by itself stop a message from being sent.** An agent can still send what it knows to someone. Sealing means what it knows holds less; owner approval decides whether the message goes.
