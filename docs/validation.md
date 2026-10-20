# Validating the Adapter

The adapter exists so that an MCP server behaves the same through Muse as it does when you talk to it directly. This guide describes how that claim is tested, what counts as passing, and how to repeat the checks yourself.

- [The question being answered](#the-question-being-answered)
- [Setup](#setup)
- [How results are judged](#how-results-are-judged)
- [What is tested](#what-is-tested)
- [Running the checks](#running-the-checks)
- [Reading the evidence](#reading-the-evidence)
- [Exit criteria](#exit-criteria)
- [Known differences from a direct connection](#known-differences-from-a-direct-connection)
- [Trying it with Muse](#trying-it-with-muse)
- [Troubleshooting](#troubleshooting)

## The question being answered

Muse adds services as OpenAPI-described connectors; MCP servers speak MCP. The adapter sits between them, so every MCP feature has to be carried across, translated, deliberately restricted, or declared out of scope. The validation answers three questions:

1. **Parity**: for every MCP feature, does the connector give Muse the same information and results as a direct MCP client gets? Where it differs, is the difference intended and visible?
2. **Safety**: do writes run exactly once, fail clearly, and never get repeated when the outcome is uncertain? Can hostile input or a hostile server make the adapter do something the owner did not allow?
3. **Operability**: does it keep working when servers fail, change, or slow down, and what does it cost?

## Setup

Everything runs locally with no network access beyond loopback.

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

The checks use **Atlas**, a purpose-built MCP server that offers every kind of MCP primitive plus controls for injecting failures (see [validation-server.md](validation-server.md)). Each test starts its own Atlas on a free port, publishes a policy that exposes everything it offers, links a connection, and then talks to the connector the way Muse does: over HTTP with a bearer token.

```text
test ──direct MCP client──────────────▶ Atlas      (the baseline)
test ──HTTP, as Muse──▶ connector ────▶ Atlas      (what Muse experiences)
                                         │
                       ledger of real side effects ◀┘   (ground truth for writes)
```

Atlas keeps a **ledger** of every side effect it actually performed. That is the key to judging writes: the ledger shows what really happened, regardless of what any response claimed.

## How results are judged

Three kinds of oracle are used, and every test relies on at least one.

| Oracle | What it is | Used for |
| --- | --- | --- |
| **Differential** | Make the same request directly and through the connector; compare. | Parity of tools, resources, prompts, content types, schemas. |
| **Ground truth** | Atlas's ledger and state, which the connector cannot influence. | Write safety: how many times did the effect actually happen? |
| **Specification** | The MCP and OpenAPI specifications, and the connector's own published contract. | Schema validity, error handling, authentication challenges. |

Each parity comparison is given one verdict:

| Verdict | Meaning |
| --- | --- |
| `SAME` | The caller gets the same thing as from the server directly. |
| `TRANSLATED` | The same information arrives in the shape REST requires (for example a tool error becomes an HTTP error, progress arrives with the operation's status). Nothing is lost. |
| `POLICY` | The owner's limits deliberately change the outcome (size limits, blocked URI values). |
| `NOT_APPLICABLE` | A property of the MCP transport itself that has no meaning for a request/response API. |
| `UNSUPPORTED` | An MCP feature the connector does not carry. **This must stay at zero.** |

## What is tested

| Area | What is checked | Suite |
| --- | --- | --- |
| Tools | Listing, names, input and output schemas, annotations, descriptions; calls with text, structured, image, audio, embedded and linked content; mixed ordering; unicode; nested arguments; errors; unknown tools; invalid input; results that break their own schema; progress, logs, questions to the user, and sampling. | `test_parity_tools.py` |
| Resources | Listing, text and binary reads, changing content, templates with awkward values (`..`, `%2e%2e`, spaces, unicode, NUL), size limits. | `test_parity_resources_prompts.py` |
| Prompts | Listing, arguments and defaults, roles, embedded resources, unicode. | `test_parity_resources_prompts.py` |
| Write safety | A write runs exactly once across retries; a dropped connection before the effect is retryable; a dropped connection after the effect is `UNKNOWN` and never repeated; owner reconciliation settles it; concurrent duplicates collapse to one; keys are scoped per connection; operations never stay `RUNNING`. | `test_write_safety.py` |
| Resilience | A server that stops and returns; write and read timeouts; per-server concurrency limits; continued service after failures; servers that cannot hold sessions. | `test_resilience.py` |
| Change and drift | Each kind of upstream change is detected (new required input, annotation change, new description, removed or added tools); Muse's view does not change until the owner publishes; pinned descriptions keep injected text out; publishing enforces a stricter schema; rollback restores the earlier contract. | `test_drift_and_change.py` |
| Authentication | Linked credential reaches the server and no other; rejected credentials are reported as such; missing credentials fail before any request; callers without a valid token are refused with a discovery challenge; tokens do not work on other connectors or after rotation; one connection cannot read another's operations. | `test_auth_matrix.py`, plus the OAuth suites below |
| Hostile input | Instructions inside results stay data; redirects are not followed; secrets are absent from the published description and the database; malformed, deeply nested, oversized and mistyped bodies are client errors; path tricks and wrong methods find nothing; unlisted tools cannot be called. | `test_security_negative.py` |
| Discovery at scale | Many pages of listings; a server that repeats a pagination cursor. | `test_discovery_scale.py` |
| Cost | Added latency over a direct call, at p50 and p95. | `test_performance.py` |
| OAuth and sign-in | Muse signing in to the adapter (PKCE, codes, refresh rotation, reuse detection); the owner signing in to the MCP provider; silent refresh; reconnect prompts; isolation between connections. | `tests/e2e/test_muse_oauth.py`, `tests/e2e/test_oauth_flow.py` |
| Real network | Real sockets, DNS pinning, redirects, an operator walkthrough from registration to a working call. | `tests/e2e/test_real_network.py`, `tests/e2e/test_cli.py` |
| Unit and component | Policy validation, catalog compilation, schema generation, persistence, content handling. | `tests/unit`, `tests/integration` |

A final **parity gate** (`test_zz_parity_gate.py`) fails if any scenario recorded `UNSUPPORTED`.

### Mapping to the standard MCP conformance scenarios

The MCP project publishes a conformance suite that exercises servers as an MCP client. Its scenarios are used as the checklist for the differential tests: each parity row names the scenario it corresponds to (for example `tools-call-image`, `resources-read-binary`, `prompts-get-with-args`, `tools-call-with-progress`, `server-initialize`). Scenarios about the MCP transport itself (initialize, ping, SSE streams, session lifecycle, DNS rebinding, subscriptions, completion) are `NOT_APPLICABLE` to a REST connector and are listed as such rather than skipped silently.

## Running the checks

Everything:

```bash
.venv/bin/pytest
```

Only the validation program:

```bash
.venv/bin/pytest -m validation
```

Including the timing measurements, which are not part of the default run:

```bash
.venv/bin/pytest -m "validation and performance"
```

A single area, for example write safety:

```bash
.venv/bin/pytest tests/validation/test_write_safety.py -v
```

Static checks that belong to the same release gate:

```bash
.venv/bin/ruff check src tests
.venv/bin/pyright
```

To explore by hand, start Atlas and a connector, then use any MCP client against Atlas and any HTTP client against the connector:

```bash
PYTHONPATH=src:examples .venv/bin/python -m validation_server --auth bearer --port 9101
```

## Reading the evidence

Running the parity suites writes two reports to `reports/` (ignored by git):

- `reports/mcp-parity.md`: one row per scenario with the verdict, what the server returned directly, what the connector returned, and a note. Verdict counts are at the top.
- `reports/performance.md`: direct versus connector latency, with overhead at p50 and p95.

Review the `TRANSLATED`, `POLICY` and `NOT_APPLICABLE` rows when you change behaviour: each is a deliberate decision that should still be true.

## Exit criteria

A release is acceptable when all of the following hold:

1. The full test run passes, including lint and type checks.
2. The parity gate passes: zero `UNSUPPORTED` scenarios.
3. Every write-safety scenario passes, and in each, the ledger shows at most one effect per logical write.
4. No secret (server credential, connection token, provider token) appears in the published API description, the database, an error body, or a log.
5. The added latency stays within the budget in `test_performance.py` (the p95 overhead per call).
6. Every difference in the parity report is one listed below or explained in its note.

## Known differences from a direct connection

These are the places where a connector is not byte-for-byte what a direct MCP client sees, and why.

| Area | Direct MCP | Through the connector | Why |
| --- | --- | --- | --- |
| Tool errors | `isError` result | HTTP 502 with the server's message | REST reports failure by status code. |
| Invalid arguments | The server rejects them | The adapter rejects them (HTTP 400) before the server is contacted | Bad input from a model never reaches your server. |
| Unknown tool | `isError` result | HTTP 404 | Same. |
| Progress and logs | Streamed live | Returned with the result, and kept with the operation's events | A request/response API cannot push. |
| Questions to the user (elicitation) | Asked mid-call | HTTP 409 `INPUT_REQUIRED` with the question, answered by calling resume | Same; the user still answers once. |
| Resources and templates | `resources/list`, `read` | One operation per resource or template, naming its URI | There is no list call in OpenAPI. |
| Annotations | Advisory hints | The owner's policy classification, with confirmation hints | Hints from a server are untrusted. |
| Descriptions | The server's text | The owner's pinned text plus a classification note | A server cannot change what Muse is told. |
| Large results | Whatever the server sends | Limited by the owner's maximum sizes | Protects Muse and the adapter. |
| Template values | Passed through | Dot segments and control characters are refused | Prevents path tricks. |
| Subscriptions, completion, sampling | Available if offered | Not available | They need a live channel the REST contract does not have. |
| Tool list changes | Pushed as `list_changed` | Noticed by drift detection; takes effect when the owner publishes | The contract Muse sees is deliberately stable. |

Everything else (names, schemas, output schemas, content types and their order, resource contents, prompt messages) is the same.

## Trying it with Muse

The local suites cover the adapter completely on their own. A session with Muse confirms what only Muse can: how it reads the description, how its sign-in screen behaves, and how it presents confirmations.

1. Start Atlas (with a bearer token) and an HTTPS tunnel that reaches the adapter's port (any tunnel that gives a public `https://` address works).
2. Register, discover, publish and link Atlas, then serve it at the tunnel address:

   ```bash
   PYTHONPATH=src:examples .venv/bin/python -m validation_server --auth bearer --port 9101 &
   export ATLAS_TOKEN=atlas-secret-token
   adapter=".venv/bin/muse-mcp-adapter --database atlas.db --config serve.json --allow-http --allow-private-network"
   $adapter server-register --server-id atlas --display-name Atlas \
     --endpoint http://127.0.0.1:9101/mcp --auth-profile bearer
   $adapter discover-remote --server-id atlas --token-env ATLAS_TOKEN
   $adapter policy-template --server-id atlas --expose-reads > policies.json
   #  edit policies.json: set "allowed": true on the writes you want to try
   $adapter publish  --server-id atlas --catalog-version 1.0.0 --policies policies.json
   $adapter activate --server-id atlas --catalog-version 1.0.0
   $adapter link --server-id atlas --label "Muse trial" --credential env_token --token-env ATLAS_TOKEN
   $adapter serve
   ```

   `serve.json` sets `publicUrl` to the tunnel address (see the [usage guide](usage.md#3-running-the-service)). To skip the setup for a first look, `examples/prepare_sample_connector.py` does the same for the smaller sample server.

3. In Muse, ask it to add a custom connector from `https://<your-tunnel>/c/<slug>/openapi.json`, signing in with the connection code, or giving it the connection token.
4. Walk through the checklist:

   | Try | Expect |
   | --- | --- |
   | "Look up ISSUE-1" | A normal answer from a read. |
   | "Create an issue called X" | Muse asks you to confirm, then exactly one issue appears in Atlas's ledger (`/_control/ledger`). |
   | Ask for the same write again | No second effect (idempotency). |
   | "Schedule a review for ISSUE-1" | Muse relays the server's question to you and carries your answer back. |
   | "Show the logo" | An image comes back as an image. |
   | Switch Atlas to its `v2` profile and ask again | Muse's connector does not change until you publish; the owner sees the drift first. |
   | Rotate the token, then use Muse | It fails clearly and asks you to reconnect. |

5. Compare with the same requests made directly against Atlas using any MCP client. Differences should match the table above.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Every call returns 403 `CREDENTIAL_RESOLUTION_FAILED` | The environment variable named by the connection is not set where `serve` runs. |
| 502 `DOWNSTREAM_AUTH_REJECTED` | The server refused the stored credential; replace it or sign in again. |
| 503 `DOWNSTREAM_UNAVAILABLE` | The MCP server could not be reached and nothing was sent. Safe to repeat after the `Retry-After` delay. |
| 401 with a `resource_metadata` challenge | The caller's token is missing, revoked, or for another connector. |
| 409 `UNKNOWN_OUTCOME` | A write may or may not have happened; check the system, then `reconcile`. |
| A tool Muse could use is missing | It is not in the published policy, or has not been republished since discovery. |
| Parity suite writes no report | Run the parity files, not a filtered subset that skips them. |
