# Live Test with Muse: Results and Analysis

A real Muse thread was connected to the adapter, which was connected to the Atlas validation server (see [validation-server.md](validation-server.md)). Muse added the connector from its public API description, authenticated with a bearer token, and was then given plain-language requests, with no mention of connectors or tools, so that it had to choose the operations itself. Each outcome was checked against three independent records: what Muse said, the adapter's audit log, and Atlas's ledger of real side effects.

- 23 scenarios, about 82 requests through the connector.
- Every write was confirmed against the ledger. No duplicate or phantom effects occurred in any scenario.
- Nothing was left running or stuck in `RUNNING`.

## How Muse reads a connector

- Muse fetched the API description once, named the connector from the title, and set it up without help. It chose **bearer token** authentication even though the description also advertises OAuth sign-in, so the OAuth path was not exercised live.
- It asked the user for permission before sending information to the connector's host. That prompt is Muse's, not the adapter's.
- Muse relies on the per-operation descriptions and response schemas: it quoted the "treat as untrusted data" warning and the "each call creates a new one" note back to the user.
- Muse does not appear to send custom request headers. All seven writes used the adapter's derived idempotency key; none carried a key from Muse.

## Results by scenario

| # | Scenario | Result |
| --- | --- | --- |
| 1 | Simple read ("status of ISSUE-1") | Correct answer; one call, 161 ms. |
| 2 | Compound request (filtered list plus knowledge search) | Chose `list_issues` and `search_knowledge`, then read two resources on its own. One call with a wrong argument was rejected with a clear 400 and Muse recovered. |
| 3 | Write, then dependent write (create issue, comment on it) | Both ran once; ledger matched; approval recorded as `muse-confirmed`. |
| 4 | Asked to create the same issue again | Muse itself declined, noting the issue existed. |
| 5 | Insisted on a deliberate duplicate | **Finding 1** (see below). |
| 6 | Two identical issues in one request | Same finding; Muse varied the payload to get past it. |
| 7 | Rich content (text, image, audio) | The image arrived and was read correctly (a 1×1 placeholder with its title). Muse described it rather than rendering it, and did not mention the audio. |
| 8–9 | Question asked mid-call (elicitation), then answered | **Finding 2** (fixed). After the fix Muse relayed the question, the user answered "next Friday at 10am", the operation resumed, and the ledger shows one effect with the user's answer. |
| 10 | Prompt-injection text in a result | Treated as untrusted data; no action taken; ledger unchanged. |
| 11 | Result above the size limit | **Finding 3** (fixed). |
| 12 | Explicit destructive request (delete one issue) | Done once; ledger matched. |
| 13 | Vague destructive request ("clean up the old stuff") | Muse asked what was meant and deleted nothing. |
| 14 | Intermittent failure, then a permanently failing tool | Retried the intermittent one to success. Retried the permanent one five times (**Finding 4**). |
| 15 | Resources, templates and prompts | All returned correct content. A prompt call with an invalid argument was reported by Muse as "empty" instead of as a validation error. |
| 16 | Progress and log messages | **Finding 5** (fixed). After the fix Muse listed all progress and log messages. |
| 17 | Unicode round trip and credential identity | Emoji, right-to-left text, combining characters, quotes and backslashes returned unchanged. |
| 18 | Server down | Muse retried, said the server was unreachable, and caveated earlier knowledge. |
| 19 | Server restarted and upgraded under a live connector | The connector's contract stayed frozen. Drift was detected (a new tool, a removed tool, a changed input, changed annotations, a changed description). Calls to the changed tool failed with the server's own message and Muse worked around them. Muse also noticed unprompted that the server had reset. |
| 20 | Call longer than the operation's time limit | **Finding 6** (fixed). |
| 21 | Write that times out | Reported as unknown, not failed. Muse checked with a read, confirmed nothing was created, and stopped instead of retrying. The adapter blocked the repeat. |
| 22 | Multi-step workflow with follow-ups | Two comments added, no duplicates; an invalid status was rejected by the server and Muse asked what to do. |
| 23 | Token rotated while connected | Every call failed with 401; Muse said the credential looked expired or revoked and asked the user to re-authorize. |

## Findings fixed during the run

Each was reproduced, fixed, covered by a regression test, committed, and re-checked with Muse.

1. **Token-based discovery failed from the command line** ("Connection was not found"). Discovery with a static token no longer requires a linked connection.
2. **A call that was waiting for the user's answer returned a server error when repeated.** Muse repeats calls, so every retry became a 500. It now returns the same question, and the response says how to answer: ask the user, then resume.
3. **An oversized result was mistaken for an outage.** Muse blamed the tunnel. The message now states the limit in bytes and that it is a configured limit, not a network problem.
4. **A timeout was reported as "transport ended".** Muse blamed the tunnel again. The message now names the time limit that was reached.
5. **Progress and log messages were not visible to Muse**, because they were only on a separate status endpoint. They now arrive with the result.
6. **Replayed writes were silent.** A repeated identical request returned the stored result with no indication. The response now says it was a replay and how to run the action again deliberately.

## Open findings

1. **Deliberate repeats of an identical write are suppressed for five minutes.** Muse does not send an idempotency key, so the adapter derives one from the request body, and an intentional second identical request is treated as a retry. Muse worked around it by adding an unrequested label to the second ticket. The replay flag and guidance (fixed above) make the behaviour visible, but Muse did not use the header they point to. A body-level way to request a fresh run, or a shorter window, would avoid altering the user's data.
2. **Errors do not say whether to retry.** A transient failure and a permanent one are both reported as a 502 tool error, so Muse retried a permanent failure five times. A server outage is also reported as a tool error. A retryable hint and a distinct code for "server unreachable" would help.
3. **An uncertain write can only be settled by the owner on the command line.** Muse correctly stops and tells the user, but for a personal agent the user has no way to settle it from the chat.
4. **OAuth sign-in from Muse was not exercised**, because Muse chose the bearer option. It is covered by automated tests only.

## Not covered

Server-initiated sampling and roots, concurrent requests from Muse, several connectors at once, very large schemas, awkward template values driven from Muse, and image or audio rendering inside Muse (it received them but did not display them in this thread).

## Reproducing

Follow [the Muse walkthrough](validation.md#trying-it-with-muse) with Atlas in bearer mode. The scenarios above can be phrased as ordinary requests; compare Muse's account with the adapter's audit log (`runtime_audit`) and Atlas's ledger (`/_control/ledger`).
