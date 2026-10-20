# What Muse sees and how the API works

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
| `503` | The MCP server could not be reached, so nothing was sent. Safe to repeat after the `Retry-After` delay. |

Every error body carries `retryable` (true when repeating the same call can succeed) and `nextStep` (what to do instead when it cannot). Retryable errors also send a `Retry-After` header.

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
