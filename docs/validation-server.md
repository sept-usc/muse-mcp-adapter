# Atlas: the MCP Server Used for Validation

Atlas is a deliberately complete MCP server. It offers every kind of MCP primitive, can authenticate three ways, and can be told to misbehave on command. It exists so the adapter can be checked against a server whose correct behaviour is known, and so any MCP client can be pointed at it to compare a connector with the real thing.

## Running it

```bash
PYTHONPATH=src:examples .venv/bin/python -m validation_server [options]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--port` | 9101 | Where it listens (`http://127.0.0.1:<port>/mcp`). |
| `--auth none\|bearer\|oauth` | `none` | How callers authenticate. |
| `--token` | `atlas-secret-token` | The bearer token for `--auth bearer`. |
| `--access-ttl` | 3600 | Access token lifetime in seconds for `--auth oauth`. |
| `--stateless` | off | Stateless HTTP. Sampling and questions to the user cannot work without sessions, which is itself something to test. |

In `oauth` mode Atlas runs its own authorization server (registration, PKCE, refresh) with scopes `issues:read`, `issues:write` and more, so scope handling can be exercised.

## What it offers

### Tools (23)

Reads, which carry `readOnlyHint`:

| Tool | Exercises |
| --- | --- |
| `get_issue`, `list_issues` | Structured output, optional parameters, enums, arrays. |
| `search_knowledge` | Nested results; a description the `v2` profile changes. |
| `whoami` | Which credential the server saw (credential isolation). |
| `render_chart` | Text, image and audio in one result. |
| `monthly_report` | Text plus an embedded resource. |
| `related_documents` | Resource links. |
| `echo_unicode` | Emoji, right-to-left text, escapes. |
| `injection_probe` | Text that tries to instruct the model. |
| `payload` | A result of a requested size. |
| `slow` | A call that takes a requested time. |
| `flaky` | Fails the first N calls for a key. |
| `progress_task` | Progress and log notifications. |
| `ask_client_model` | Sampling. |
| `list_client_roots` | Roots. |
| `bad_output` | A result that breaks its own declared schema. |

Writes, which are the safety cases:

| Tool | Exercises |
| --- | --- |
| `create_issue` | A write that creates something new on every call (the clearest duplicate detector). |
| `update_issue`, `add_comment` | Ordinary writes. |
| `delete_issue` | A destructive write. |
| `send_notification` | An external, irreversible effect. |
| `schedule_review` | A write that asks the user a question mid-call. |
| `archive_project` | A write that asks for confirmation. |

### Resources, templates and prompts

- **Resources (7):** markdown, plain text, JSON, PNG, opaque binary, a very large text, and one whose content changes on every read.
- **Templates (3):** `kb://articles/{slug}`, `issues://{project}/{issue_id}`, `files://{path}`, for awkward-value tests.
- **Prompts (4):** with arguments and defaults, with several roles and an embedded resource, with no arguments, and with unicode.

### Profiles

`v1` is the baseline. `v2` changes the server the way a real upgrade might: a new required input on `get_issue`, `list_issues` losing its read-only hint, a new description on `search_knowledge`, `echo_unicode` removed and `archive_issue` added. It is used to prove that upstream changes are detected and never reach Muse unreviewed.

## The ledger

Every side effect Atlas performs is appended to a ledger: a sequence number, the tool, the details of what was done, and the time. Responses can be lost, delayed or misreported; the ledger cannot. Tests assert on it to prove a write happened once, not twice, or not at all.

## Control API

Available on the same port; used by tests and handy by hand.

| Route | Effect |
| --- | --- |
| `GET /_control/state` | Current issues and settings. |
| `GET /_control/ledger` | Every side effect so far. |
| `POST /_control/reset` | Back to a clean state. |
| `POST /_control/faults` | Set faults per tool (below). |
| `POST /_control/profile` | `{"profile": "v1"\|"v2"}`. |
| `POST /_control/redirect` | `{"to": "<url>"}` answers every MCP request with a redirect; `{"to": null}` stops. |
| `POST /_control/oauth/expire` | Expire all access tokens (OAuth mode). |
| `POST /_control/oauth/revoke` | Revoke refresh tokens (OAuth mode). |
| `GET /health` | Liveness. |

Faults, set per tool name:

| Fault | Behaviour |
| --- | --- |
| `fail_first: N` | The first N calls fail, then succeed. |
| `delay_seconds: S` | Wait before answering. |
| `drop_before_effect` | Close the connection before doing the work. Nothing happened. |
| `drop_after_effect` | Do the work, then close the connection. The effect is in the ledger but the caller never hears. |
| `drop_times: N` | Apply a drop fault to the next N calls only. |

The two drop faults are the heart of write-safety testing: from the caller's side they look identical, but only one of them changed the world, and the ledger says which.

## Using it with any MCP client

Because it is a standard MCP server, any client can talk to it, including the official MCP Inspector and conformance tooling. Point the client at `http://127.0.0.1:<port>/mcp` (with the bearer token or OAuth sign-in if you chose one), and compare its results with the connector's using [the checklist in the validation guide](validation.md#trying-it-with-muse).
