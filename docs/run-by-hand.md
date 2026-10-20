# Run it by hand


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

## Try it with a public server

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

## A server that needs a token

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

## A server that signs you in with OAuth

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

The sign-in link and the provider's redirect both go through the adapter's public address, so `publicUrl` must be reachable from your browser. If a provider does not support dynamic client registration, add its client id under `oauthClients` in `serve.json` (see the [usage guide](usage.md#3-running-the-service)).

Want to try it without Muse or a real provider? The [usage guide](usage.md#try-it-locally) walks through a sample MCP server and a script that behaves like Muse.


## Reference provider packs

Provider packs hold endpoint and safety settings for common servers. They add no provider-specific code and never authorize discovered tools automatically.

| Pack | Endpoint | Safe starting point |
| --- | --- | --- |
| [Linear](https://linear.app/docs/mcp) | `https://mcp.linear.app/mcp` | Start from the documented read-only endpoint; classify and approve writes explicitly |
| [Atlassian](https://atlassian.github.io/atlassian-mcp-server/) | `https://mcp.atlassian.com/v2/mcp` | Use least-privilege scopes; classify and approve writes explicitly |
| [Supabase](https://supabase.com/docs/guides/ai-tools/mcp) | `https://mcp.supabase.com/mcp` | Require `project_ref`; `read_only=true` by default |

These are starting points, not a claim that everything a provider offers is safe to publish. Run discovery, review it, and write your own policy for the account you control.
