# Muse MCP Adapter documentation

The adapter lets Muse use your MCP (Model Context Protocol) servers, such as GitHub, Linear or Atlassian, as a Custom Connector, with only the access you approve. These pages explain how to set it up, how it works and why it is safe. The [project README](../README.md) is the short version.

## Start here

| I want to | Page |
| --- | --- |
| Connect GitHub to Muse with a token limited to one repository | [Connect GitHub to Muse](connect-github-to-muse.md) |
| Add MCP servers by chatting with Muse and approving on my own page | [Set up by chat](chat-setup.md) |
| Install the adapter on a Google Cloud VM or a Linux machine | [Getting started](getting-started.md) |
| Understand what it is and how a call travels | [How it works](how-it-works.md) |
| See what it adds over connecting each server by hand, and where it stops | [What it adds](what-it-adds.md) |

## Use and operate

| Page | What it covers |
| --- | --- |
| [Day to day](day-to-day.md) | Add a server, change what Muse can use, upgrade, back up, rotate a token, pause, remove |
| [Your policy](policy.md) | Exactly what Muse may use: operations, classifications, limits, end times |
| [Troubleshooting](troubleshooting.md) | What to do when a step fails |
| [Running on a VM](operations.md) | Looking after the machine |
| [Test it yourself](test-locally.md) | Rehearse the whole flow on your computer, then as a customer would |
| [Settings file and automation](automation.md) | `muse-vm.toml`, Terraform, non-interactive runs |
| [Cloud Shell tutorial](cloudshell-tutorial.md) | The guided path used by the Open in Cloud Shell button |

## Security

| Page | What it covers |
| --- | --- |
| [Security model](security.md) | Trust boundaries and every control |
| [Identity and authentication](authentication.md) | Every credential: where it comes from, where it lives, what a leak costs |
| [Network exposure](networking.md) | What is public, what is private, what is blocked outbound |
| [Security policy](../SECURITY.md) | How to report a vulnerability |

## Reference

| Page | What it covers |
| --- | --- |
| [Deployment reference](deployment.md) | Every `muse-vm` command and what is installed on the machine |
| [Usage guide](usage.md) | Adapter commands, the policy file and configuration |
| [What Muse sees](api.md) | The REST API and OpenAPI description Muse reads |
| [Run it by hand](run-by-hand.md) | Without `muse-vm`, and the provider packs |

## Design and validation

| Page | What it covers |
| --- | --- |
| [System design](design.md) | Purpose, decisions, architecture, security model |
| [Control plane](control-plane.md) | Register, discover, review, compile, publish, and setup by chat |
| [Data plane](data-plane.md) | How a published catalog is executed at runtime |
| [Validation guide](validation.md) | How parity with a direct MCP connection is tested |
| [Validation server](validation-server.md) | The purpose-built MCP server used to test every feature |
| [MCP parity report](../reports/mcp-parity.md) | The generated results |

## Contributing

[Contributing](../CONTRIBUTING.md) explains how to set up, the gate every change must pass, and how a pull request is merged.
