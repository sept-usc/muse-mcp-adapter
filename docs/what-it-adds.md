# What the adapter adds

An MCP server can already be added to Muse as a Custom Connector, so access is not what the adapter offers. Muse also provides its own safeguards: it keeps credentials away from the agent, confirms consequential actions with you, and keeps an activity history. The adapter does not replace any of that.

What it adds is a layer you own, between Muse and each MCP server, that a connector built one server at a time does not give you. Each row below states what the adapter does, where it is tested, and where it stops.

| What it adds | What it does | Tested by |
| --- | --- | --- |
| **A frozen, versioned contract** | Publishing compiles an immutable catalog: the exact operations, descriptions, schemas, classifications and limits Muse may use. A server's description is pinned when you publish, so the server cannot change what Muse is told. A later change at the server does not reach Muse until you publish it, and `rollback` returns to the previous version. | Catalog and rollback tests; the *Change and drift* suite in the [validation guide](validation.md) |
| **Drift you can see** | `drift` compares the live server with the active catalog and reports each difference by kind (a tool added, removed, or changed in its input schema, output schema, annotations, required scopes, result content or lifecycle) with a severity. | `test_drift_detector_classifies_schema_and_authorization_changes`, and the same suite |
| **A policy you own** | For each operation you decide: is it exposed at all, is it a read, a write or a sensitive write, its timeout, the largest result accepted, the result types accepted, and the downstream scopes required. Anything you did not list does not exist for Muse. Destructive tools (`destructiveHint`, or a word such as delete or merge in the name) are drafted as sensitive writes. For resources and templates you also limit URI schemes, hosts, MIME types and argument patterns. | Policy and catalog tests; the *Hostile input* suite |
| **Writes that run once** | A repeated write is replayed, not run again: the client's `Idempotency-Key` is honoured, and without one an identical write is treated as a retry while the first runs or for 30 seconds after it. Every write is recorded. If the adapter cannot tell whether a write happened (a failure after the request was sent), it becomes `UNKNOWN` and further retries are blocked until you check the system and run `reconcile`. A failure before anything was sent is retryable and says so (`503`, `Retry-After`). | The *Write safety* and *Resilience* suites |
| **The whole of MCP, tested** | Tools, resources, resource templates and prompts; structured, image, audio, embedded and linked results; progress and log messages; and questions the server asks mid-call (`409 INPUT_REQUIRED`, then `resume`) are carried into one REST contract. Every MCP feature is checked against a direct MCP client, and a final gate fails if any is recorded as unsupported. | The parity suites and `test_zz_parity_gate.py` |
| **Credentials that stay with you** | Each connection has its own credential: none, a token stored in a root-only file, or your own OAuth sign-in with tokens encrypted at rest. Muse holds one revocable token per connector and never sees the server's credential. A token works on its own connector only. `rotate-token` ends every session for a connection. | The *Authentication* suite; checked live with two connectors on one adapter |
| **A safe place to run** | The adapter runs unprivileged on a read-only filesystem. An egress guard blocks the cloud metadata server and private networks, so a compromised adapter cannot use the machine's cloud identity. Nothing on the machine accepts an inbound connection. A self-check tests all of this from outside and inside before setup reports success. | `scripts/check_stack.py` in the gate; the setup self-check, run on every install |
| **Custody and audit** | You host it. Every request and every administrative action is recorded on your machine, with secrets redacted, and backups are encrypted. | The *Hostile input* suite (secrets are absent from the database and the description); backup and restore tests |

## What it does not do (yet)

These limits are real, and the page should change when they do.

- **Policy works per operation.** It cannot yet decide by the value of an argument (for example "only project X"), by who is asking, or by how many records a call touches. Resources and templates have argument-level limits; tools do not.
- **Drift reports what changed, not whether it is safe.** It does not judge backward compatibility, estimate impact, or replay tests against a changed server.
- **Settling an unknown write is manual.** You check the other system, then run `reconcile` with the adapter. There is no automatic read-after-write check and no way to settle it from chat.
- **Read-only labels are trusted by default.** `add-server` exposes the tools a server itself marks read-only, and prints what it exposed. A server that mislabels a write would be exposed. `--no-reads` reviews every tool first.
- **Signed approvals are not usable.** The mode exists in the code, but the HTTP interface cannot carry the evidence it needs. Muse's own confirmation is the approval today.
- **One owner per deployment.** No teams, roles or single sign-on.
- **Built for Muse.** The contract is plain REST with an OpenAPI description, but it has been tested only with Muse. Local `stdio` servers and MCP Apps are not supported.
- **Muse's own safeguards still apply, and are separate.** The policy governs what passes through the adapter. It does not limit Muse's other tools, such as its own browser.
